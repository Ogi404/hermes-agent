"""Approval-bound held-source eligibility; never admission or worker execution."""
from __future__ import annotations

import copy
from pathlib import Path

from plugins.fleet_lifecycle.controller import (
    Halt, Ledger, Native, canonical, digest, require, token,
)
from plugins.fleet_lifecycle.intake import _read_source, prepare_held_scope


def _identity(policy, board, task_id):
    token(board, r'[a-z0-9][a-z0-9_-]{0,79}')
    token(task_id, r't_[0-9a-f]{8}')
    home = Path(policy['installation']['profiles']['director']['home'])
    return home / 'fleet-eligibility', digest(canonical({'board': board, 'task_id': task_id}))


def _held(conn, task_id, expected=None):
    from hermes_cli.kanban_contract_guard import (
        _extract_contract, fleet_contract_planning_guard_reason,
    )
    row = conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    require(row is not None, 'eligibility_identity_changed')
    row = dict(row)
    require(expected is None or row == expected, 'eligibility_identity_changed')
    require(fleet_contract_planning_guard_reason(row) is None, 'eligibility_not_held')
    contract, error = _extract_contract(row['body'])
    require(error is None and contract['agent_ready'] is False and contract['labels'] == [],
            'eligibility_not_held')
    require(not conn.execute('SELECT 1 FROM task_runs WHERE task_id=?', (task_id,)).fetchone(),
            'eligibility_owned')
    require(not conn.execute('SELECT 1 FROM task_events WHERE task_id=? AND kind=?',
                             (task_id, 'fleet_controller_admitted')).fetchone(), 'eligibility_owned')
    return row, contract


def _gate(scope, plan, row):
    from hermes_cli import kanban_db as kb, kanban_db_notify as notify
    native = Native(plan)
    proof = 'held-eligibility:v1:' + digest(canonical(scope))
    key = 'fleet-eligibility:' + plan.data['board'] + ':' + row['id']
    with native.connect(True) as conn:
        with kb.write_txn(conn):
            _held(conn, row['id'], row)
            require(not conn.execute('SELECT 1 FROM tasks WHERE idempotency_key=?', (key,)).fetchone(),
                    'eligibility_interrupted')
            gate_id = kb.create_task(
                conn, title='Source eligibility review: ' + row['title'],
                body=('Existing held source readiness only; same native task remains blocked. '
                      'Independent scope review and authenticated Slack approval required. '
                      'No staging, admission, worker, product edits or execution. Proof: ' + proof),
                created_by='trusted-controller', workspace_kind='scratch',
                initial_status='blocked', idempotency_key=key,
            )
            route = plan.data['slack']
            notify.add_notify_sub(conn, task_id=gate_id, platform='slack', chat_id=route['channel'],
                                  thread_id=route['thread'], chat_type='channel',
                                  notifier_profile=route['profile'], delivery_mode='notify',
                                  delivery_metadata={'slack_team_id': route['team']}, allow_nested=True)
            gate = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (gate_id,)).fetchone())
    return gate_id, Native.frozen(gate)


def prepare_eligibility(policy, task_id, proposal, *, board, runtime_factory=None):
    """Freeze reviewed scope and a parked approval carrier; no remote writes."""
    from plugins.fleet_lifecycle.controller import Plan
    policy, proposal = copy.deepcopy(policy), copy.deepcopy(proposal)
    root, key = _identity(policy, board, task_id)
    held_scope = prepare_held_scope(policy, task_id, proposal, board=board,
                                   runtime_factory=runtime_factory)
    plan = Plan(held_scope['plan'], held_scope['plan_hash'])
    row = _read_source(board, task_id)
    with Native(plan).connect() as conn:
        row, contract = _held(conn, task_id, row)
    scope = {'proposal': proposal, 'schema': 1, 'policy_sha256': digest(canonical(policy)),
             'held_scope': held_scope, 'native_row': row, 'contract': contract}
    proof = 'held-eligibility:v1:' + digest(canonical(scope))
    with Ledger(root) as ledger:
        require(ledger is not None, 'eligibility_busy')
        prior = ledger.load(key)
        if prior is not None:
            require(prior['scope'] == scope, 'eligibility_scope_changed')
            require(prior['stage'] == 'awaiting_approval' and prior['intent'] is None,
                    'eligibility_already_attempted')
            return prior['result']
        state = {'scope': scope, 'stage': 'preparing', 'intent': 'create_gate'}
        ledger.save(key, state)
        gate_id, gate_frozen = _gate(scope, plan, row)
        result = {'stage': 'awaiting_eligibility_review', 'board': board, 'task_id': task_id,
                  'gate_task_id': gate_id, 'source_sha': plan.data['source_sha'],
                  'held_plan_hash': plan.digest, 'scope_sha256': digest(canonical(scope)),
                  'proof': proof, 'execution_authorized': False}
        state.update(stage='awaiting_approval', intent=None, gate_frozen=gate_frozen, result=result)
        ledger.save(key, state)
        return result


def eligibility_command(args):
    from hermes_constants import get_hermes_home
    from plugins.fleet_lifecycle.controller import read_regular, safe_path, strict_json
    home = get_hermes_home()
    policy = strict_json(read_regular(safe_path(home / 'fleet-lifecycle-policy.json',
                                                private=True), 128 * 1024))
    require(policy['installation']['profiles']['director']['home'] == str(home), 'identity_drift')
    if args.action == 'prepare-eligibility':
        proposal = strict_json(read_regular(Path(args.proposal), 64 * 1024))
        result = prepare_eligibility(policy, args.task, proposal, board=args.board)
    else:
        result = _publish_with_transport(policy, home, args)
    print(canonical(result).decode())
    return 0


def _approved(conn, state, policy):
    from hermes_cli.kanban_human_gate import latest_gate
    result = state['result']
    gate_row = conn.execute('SELECT * FROM tasks WHERE id=?', (result['gate_task_id'],)).fetchone()
    require(gate_row is not None and Native.frozen(gate_row) == state['gate_frozen']
            and gate_row['status'] == 'blocked' and all(gate_row[k] is None for k in
                ('claim_lock', 'current_run_id', 'worker_pid')), 'eligibility_gate_changed')
    require(not conn.execute('SELECT 1 FROM task_runs WHERE task_id=?',
                             (gate_row['id'],)).fetchone(), 'eligibility_gate_changed')
    gate = latest_gate(conn, gate_row['id'], 'push')
    require(gate is not None and isinstance(gate.get('decision'), dict), 'eligibility_not_approved')
    request, decision = gate['request'], gate['decision']
    event = gate['request_event_id']
    require(type(event) is int and event > 0, 'eligibility_not_approved')
    require(request.get('stage') == 'push' and request.get('sha') == result['source_sha']
            and request.get('tests') == result['proof']
            and request.get('reviewer_verdict') == 'APPROVE', 'eligibility_not_approved')
    expected = {'actor': policy['approval']['actor'], 'platform': 'slack', 'decision': 'approve',
                'stage': 'push', 'sha': result['source_sha'], 'request_event_id': event}
    require(all(decision.get(k) == v for k, v in expected.items()), 'eligibility_not_approved')
    require(not conn.execute('SELECT 1 FROM task_events WHERE task_id=? AND kind=?',
                             (gate_row['id'], 'fleet_held_eligibility_consumed')).fetchone(),
            'eligibility_already_attempted')
    return event


def _source_before(detail, contract, normalize_source):
    require(isinstance(detail, dict) and len(canonical(detail)) <= 96 * 1024,
            'eligibility_source_changed')
    require(detail.get('labels') == [] and not detail.get('assigneeId')
            and not detail.get('delegateId'), 'eligibility_source_changed')
    normalized = normalize_source(copy.deepcopy(detail))
    expected = {k: v for k, v in contract.items() if k != 'fetched_at'}
    require(isinstance(normalized, dict)
            and {k: v for k, v in normalized.items() if k != 'fetched_at'} == expected,
            'eligibility_source_changed')
    for target, field in {'id': 'issue_id', 'uuid': 'linear_id', 'title': 'title',
                           'description': 'description', 'status': 'status', 'url': 'url'}.items():
        require(detail.get(target) == contract[field], 'eligibility_source_changed')


def _ready_contract(before, after, contract):
    import datetime
    from hermes_cli.kanban_contract_guard import _contract_hash
    require(isinstance(after, dict) and len(canonical(after)) <= 96 * 1024
            and after.get('labels') == ['agent-ready'], 'eligibility_source_changed')
    require({k: v for k, v in before.items() if k not in ('labels', 'updatedAt')}
            == {k: v for k, v in after.items() if k not in ('labels', 'updatedAt')},
            'eligibility_source_changed')
    stamp = after.get('updatedAt')
    require(isinstance(stamp, str) and bool(stamp), 'eligibility_source_changed')
    try:
        parsed = datetime.datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    except ValueError:
        raise Halt('eligibility_source_changed') from None
    require(parsed.tzinfo is not None, 'eligibility_source_changed')
    # Every producer-normalized source input except these two observed fields
    # is proven identical to the authenticated pre-write detail above. Preserve
    # its complete validated schema/roles/relations; hash with the native owner.
    ready = copy.deepcopy(contract)
    ready.update(labels=after['labels'], agent_ready=True, source_updated_at=stamp,
                 fetched_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
    ready['contract_sha256'] = _contract_hash(ready)
    return ready


def _refresh_native(native, state, ready):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_contract_guard import fleet_contract_guard_reason
    row = state['scope']['native_row']
    body = ("Frozen Linear contract. This snapshot is the worker's only scope authority.\n\n"
            '<!-- FLEET_CONTRACT_SNAPSHOT_V1 BEGIN -->\n```json\n'
            + canonical(ready).decode() + '\n```\n<!-- FLEET_CONTRACT_SNAPSHOT_V1 END -->\n')
    require(len(body.encode()) <= 7800, 'eligibility_snapshot_invalid')
    prospective = {**row, 'body': body}
    require(fleet_contract_guard_reason(prospective) is None, 'eligibility_snapshot_invalid')
    with native.connect(True) as conn:
        with kb.write_txn(conn):
            _held(conn, row['id'], row)
            # Native non-lifecycle body refresh: CAS under the owning write
            # transaction. No status/ownership field is written. edit_task owns
            # its own non-nestable transaction, so it cannot provide this CAS.
            changed = conn.execute('UPDATE tasks SET body=? WHERE id=? AND body=? AND status=?',
                                   (body, row['id'], row['body'], row['status']))
            require(changed.rowcount == 1, 'eligibility_identity_changed')
            kb._append_event(conn, row['id'], 'edited', {'fields': ['body']})
            actual = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (row['id'],)).fetchone())
            require(actual == prospective and fleet_contract_guard_reason(actual) is None,
                    'eligibility_identity_changed')
        # This documented native observer seam runs only AFTER the outer commit.
        kb.notify_task_updated(conn, row['id'], ['body'], board=native.p['board'])
    return ready['contract_sha256']


def publish_eligibility(policy, task_id, *, board, read_issue, update_issue,
                        normalize_source, runtime_factory=None):
    """Spend one authenticated scope approval, one label write, same blocked row.

    An acknowledged or uncertain write is NEVER automatically replayed. Failure
    retains a durable halted record; no worker, worktree or admission is created.
    """
    from plugins.fleet_lifecycle.controller import Plan
    from hermes_cli import kanban_db as kb
    root, key = _identity(policy, board, task_id)
    with Ledger(root) as ledger:
        require(ledger is not None, 'eligibility_busy')
        state = ledger.load(key)
        require(state is not None, 'eligibility_not_prepared')
        require(state['stage'] == 'awaiting_approval' and state['intent'] is None,
                'eligibility_already_attempted')
        scope = state['scope']
        require(digest(canonical(scope)) == state['result']['scope_sha256']
                and state['result']['proof'] == 'held-eligibility:v1:' + digest(canonical(scope))
                and digest(canonical(policy)) == scope['policy_sha256'], 'eligibility_scope_changed')
        held_scope = scope['held_scope']
        require(held_scope['plan_hash'] == digest(canonical(held_scope['plan'])),
                'eligibility_scope_changed')
        plan = Plan(held_scope['plan'], held_scope['plan_hash'])
        native = Native(plan)
        with native.connect() as conn:
            _held(conn, task_id, scope['native_row'])
            approval = _approved(conn, state, policy)
        # Reuse the real no-effect source/preview/install validation. A moved
        # remote source or changed proposal invalidates the old human approval.
        fresh = prepare_held_scope(policy, task_id, scope['proposal'], board=board,
                                   runtime_factory=runtime_factory)
        require(fresh == held_scope, 'eligibility_scope_changed')
        contract = scope['contract']
        before = read_issue(contract['issue_id'])
        _source_before(before, contract, normalize_source)
        state.update(stage='publishing', intent='reserve_approval', source_updates=0)
        ledger.save(key, state)
        try:
            with native.connect(True) as conn:
                with kb.write_txn(conn):
                    _held(conn, task_id, scope['native_row'])
                    require(_approved(conn, state, policy) == approval, 'eligibility_not_approved')
                    kb._append_event(conn, state['result']['gate_task_id'],
                                     'fleet_held_eligibility_consumed',
                                     {'request_event_id': approval, 'task_id': task_id,
                                      'scope_sha256': state['result']['scope_sha256']})
            state.update(intent='source_update', source_updates=1, request_event_id=approval)
            ledger.save(key, state)
            reply = update_issue(contract['linear_id'])
            require(isinstance(reply, dict) and reply.get('id') in
                    (contract['issue_id'], contract['linear_id']), 'eligibility_update_uncertain')
            after = read_issue(contract['issue_id'])
            ready = _ready_contract(before, after, contract)
            final = read_issue(contract['issue_id'])
            require(final == after, 'eligibility_source_changed')
            state['intent'] = 'native_body_refresh'
            ledger.save(key, state)
            contract_hash = _refresh_native(native, state, ready)
            result = {'stage': 'eligible_native_still_blocked', 'board': board, 'task_id': task_id,
                      'contract_hash': contract_hash, 'request_event_id': approval,
                      'execution_authorized': False, 'fresh_staging_required': True}
            state.update(stage='eligible', intent=None, publication=result)
            ledger.save(key, state)
            return result
        except Exception as exc:
            state.update(stage='halted', error_type=type(exc).__name__, error_detail=str(exc)[:160])
            ledger.save(key, state)
            raise Halt('eligibility_publication_stopped') from exc


def _publish_with_transport(policy, home, args):
    from tools.mcp_tool_discovery import discover_mcp_tools
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    from plugins.fleet_lifecycle import brief
    from plugins.fleet_lifecycle.held_brief import normalize_installed_source
    from plugins.fleet_lifecycle.intake import project_policy
    from hermes_cli.kanban_contract_guard import _extract_contract, fleet_contract_planning_guard_reason

    row = _read_source(args.board, args.task)
    require(fleet_contract_planning_guard_reason(row) is None, 'eligibility_not_held')
    contract, error = _extract_contract(row['body'])
    require(error is None, 'eligibility_not_held')
    selected = project_policy(policy, contract['repository'], board=args.board)
    slug = selected['slug']
    try:
        discover_mcp_tools(allowed_mcp_names=['linear'])
        return publish_eligibility(
            policy, args.task, board=args.board,
            read_issue=lambda identifier: brief._linear_call('get_issue', {
                'id': identifier, 'includeRelations': True,
                'includeCustomerNeeds': False, 'includeReleases': False}),
            update_issue=lambda uuid: brief._linear_call('save_issue', {
                'id': uuid, 'labels': ['agent-ready']}),
            normalize_source=lambda issue: normalize_installed_source(
                home, policy, slug, args.board, issue))
    finally:
        try:
            shutdown_mcp_servers()
        except Exception as exc:
            # Publication's ledger remains authoritative. Never retry the write
            # or print a false CLI completion when transport cleanup fails.
            raise Halt('eligibility_transport_cleanup_failed') from exc

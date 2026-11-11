"""Build task-specific plans from a deployment-owned installation policy.

Native identity stays authoritative. No task, worktree, gate or worker is changed
by the plan factory; staging owns those distinct transitions.
"""
from __future__ import annotations

import copy
from pathlib import Path

from plugins.fleet_lifecycle.controller import (
    Plan, absolute, canonical, digest, fields, require, sha, token,
)

INSTALLATION_FIELDS = 'core source_sha resources profiles native state_root slack commit'
PROJECT_FIELDS = 'slug repository board base source_branch verification'


def project_policy(policy, slug, *, board):
    """Validate the deployment-owned allowlist without a scratch/default fallback."""
    fields(policy, 'schema installation approval projects')
    require(type(policy['schema']) is int and policy['schema'] == 1, 'invalid_plan')
    fields(policy['installation'], INSTALLATION_FIELDS)
    fields(policy['approval'], 'actor stage')
    token(policy['approval']['actor'], r'U[A-Z0-9]{1,40}')
    require(policy['approval']['stage'] == 'push', 'invalid_plan')
    projects = policy['projects']
    require(isinstance(projects, list) and 0 < len(projects) <= 32, 'invalid_plan')
    names = []
    for project in projects:
        fields(project, PROJECT_FIELDS)
        token(project['slug'], r'[a-z0-9][a-z0-9_-]{0,79}')
        token(project['board'], r'[a-z0-9][a-z0-9_-]{0,79}')
        absolute(project['repository'])
        sha(project['base'], 40)
        token(project['source_branch'], r'[A-Za-z0-9][A-Za-z0-9_/-]{0,159}')
        require(project['verification'] is None or isinstance(project['verification'], dict), 'invalid_plan')
        names.append(project['slug'])
    require(len(names) == len(set(names)), 'invalid_plan')
    matches = [p for p in projects if p['slug'] == slug and p['board'] == board]
    require(len(matches) == 1, 'identity_drift')
    return matches[0]


def task_plan(policy, row, proposal, gate_task_id, *, board):
    """Derive a complete scoped draft; no per-task platform template is supplied."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_contract_guard import _extract_contract, fleet_contract_guard_reason
    from hermes_cli.kanban_fleet_project_registry import fleet_project_record

    required = {'id', 'project_id', 'workspace_kind', 'workspace_path', 'branch_name',
                'body', 'created_by', 'idempotency_key', 'status', 'claim_lock',
                'current_run_id', 'worker_pid', 'workflow_template_id', 'current_step_key'}
    require(hasattr(row, 'keys') and required <= set(row.keys()), 'identity_drift')
    require(row['created_by'] == 'fleet-linear-reconciler'
            and isinstance(row['idempotency_key'], str)
            and row['idempotency_key'].startswith('linear:'), 'identity_drift')
    require(fleet_contract_guard_reason(row) is None, 'identity_drift')
    require(row['status'] in {'blocked', 'triage'} and all(row[k] is None for k in
            ('claim_lock', 'current_run_id', 'worker_pid', 'workflow_template_id', 'current_step_key')),
            'handoff_invalid')
    contract, error = _extract_contract(row['body'])
    require(error is None and contract['schema_version'] == 2
            and contract['task_lane'] == 'implementation' and contract['execution_mode'] == 'implement',
            'identity_drift')
    project = project_policy(policy, contract['repository'], board=board)
    require(fleet_project_record(row['project_id']) == (project['slug'], project['repository']),
            'identity_drift')
    expected = str(Path(project['repository']) / '.worktrees' / row['id'])
    require(row['workspace_kind'] == 'worktree' and row['workspace_path'] == expected
            and row['branch_name'].startswith(project['slug'] + '/'), 'identity_drift')
    description = contract['description']
    require(isinstance(description, str) and bool(description.strip()), 'invalid_plan')
    raw = description.encode('utf-8')
    require(len(raw) <= 64 * 1024, 'invalid_plan')
    fields(proposal, 'candidate_paths test_files context')
    paths = proposal['candidate_paths']
    require(isinstance(paths, list) and 0 < len(paths) <= 24, 'invalid_plan')
    plan = copy.deepcopy(policy['installation'])
    plan.update(schema=2, mutation=None, board=board,
                database=str(kb.kanban_db_path(board=board)), task_id=row['id'],
                project_id=row['project_id'], workspace=expected, branch=row['branch_name'],
                base=project['base'], contract_hash=contract['contract_sha256'],
                brief_sha256=digest(raw),
                gate={'task_id':gate_task_id, 'stage':policy['approval']['stage'],
                      'actor':policy['approval']['actor'], 'source_sha':plan['source_sha']},
                candidate_paths=copy.deepcopy(paths), test_files=copy.deepcopy(proposal['test_files']),
                context=copy.deepcopy(proposal['context']),
                changes=[{'path':p, 'base_sha256':None} for p in paths])
    if project['verification'] is not None:
        plan['verification'] = copy.deepcopy(project['verification'])
    Plan.validate(plan)
    for role,lane in [('director','orchestration'), ('builder','implementation'), ('reviewer','review')]:
        require(plan['profiles'][role]['name'] in contract[lane + '_profiles'], 'identity_drift')
    return Plan(plan, digest(canonical(plan)))


def _read_source(board, task_id):
    import sqlite3
    from contextlib import closing
    from hermes_cli import kanban_db as kb
    from plugins.fleet_lifecycle.controller import safe_path
    path = safe_path(kb.kanban_db_path(board=board))
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        require(row is not None, 'identity_drift')
        return dict(row)


def _prepare_source_subscription(plan, frozen):
    """Fill only missing team metadata from the deployment-owned route."""
    from hermes_cli import kanban_db_notify as notify
    from plugins.fleet_lifecycle.controller import Native
    native = Native(plan)
    route = plan.data['slack']
    with native.connect(True) as conn:
        row = conn.execute('SELECT * FROM tasks WHERE id=?', (plan.data['task_id'],)).fetchone()
        require(row is not None and Native.frozen(row) == frozen, 'identity_drift')
        require(row['status'] in {'blocked', 'triage'} and all(row[k] is None for k in
                ('claim_lock', 'current_run_id', 'worker_pid', 'workflow_template_id', 'current_step_key')),
                'handoff_invalid')
        rows = [s for s in notify.list_notify_subs(conn, plan.data['task_id'])
                if s['platform'] == 'slack' and s['chat_id'] == route['channel']
                and (s.get('thread_id') or '') == route['thread']]
        require(len(rows) <= 1, 'identity_drift')
        metadata = {}
        if rows:
            existing = rows[0]
            require(existing.get('notifier_profile') == route['profile']
                    and existing.get('delivery_mode') == 'notify', 'identity_drift')
            metadata = existing.get('delivery_metadata') or {}
            require(isinstance(metadata, dict)
                    and metadata.get('slack_team_id') in (None, route['team']), 'identity_drift')
            if metadata.get('slack_team_id') == route['team']:
                return
        metadata = {**metadata, 'slack_team_id':route['team']}
        notify.add_notify_sub(conn, task_id=plan.data['task_id'], platform='slack',
                              chat_id=route['channel'], thread_id=route['thread'],
                              notifier_profile=route['profile'], delivery_mode='notify',
                              chat_type=None if rows else 'channel', delivery_metadata=metadata)
        native.subscription(conn)


def _check_source(plan, frozen):
    from plugins.fleet_lifecycle.controller import Native
    native = Native(plan)
    with native.connect() as conn:
        row = native.task(conn)
        require(Native.frozen(row) == frozen, 'identity_drift')


def _scope_gate(plan, frozen):
    from hermes_cli import kanban_db as kb, kanban_db_notify as notify
    from plugins.fleet_lifecycle.controller import Native
    native = Native(plan)
    key = 'fleet-scope:' + plan.data['board'] + ':' + plan.data['task_id']
    with native.connect(True) as conn:
        row = native.task(conn)
        require(Native.frozen(row) == frozen, 'identity_drift')
        require(not conn.execute('SELECT 1 FROM tasks WHERE idempotency_key=?', (key,)).fetchone(),
                'staging_interrupted')
        gate = kb.create_task(conn, title='Scope review required: ' + row['title'],
                              body='Prepared implementation scope awaits independent review. No worker, approval or execution is authorised by this card.',
                              assignee=plan.data['profiles']['director']['name'],
                              created_by='trusted-controller', workspace_kind='scratch',
                              initial_status='blocked', idempotency_key=key)
        route = plan.data['slack']
        notify.add_notify_sub(conn, task_id=gate, platform='slack', chat_id=route['channel'],
                              thread_id=route['thread'], chat_type='channel',
                              notifier_profile=route['profile'], delivery_mode='notify',
                              delivery_metadata={'slack_team_id':route['team']})
        return gate


def _publish_plan(plan, path):
    import os
    from plugins.fleet_lifecycle.controller import safe_path
    safe_path(path.parent.parent, directory=True)
    if not path.parent.exists():
        path.parent.mkdir(mode=0o700)
    safe_path(path.parent, directory=True, private=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(canonical(plan.data))
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    require(Plan.load(path).digest == plan.digest, 'identity_drift')


def _stage_new(policy, row, proposal, draft, runtime, source, target, path, ledger, key, state, runtime_factory):
    from plugins.fleet_lifecycle.controller import Halt, Native
    from plugins.fleet_lifecycle.planning import prepare_plan
    try:
        state['intent'] = 'source_subscription'
        ledger.save(key, state)
        _prepare_source_subscription(draft, state['frozen'])
        _check_source(draft, state['frozen'])
        state['intent'] = 'create_scope_gate'
        ledger.save(key, state)
        gate_id = _scope_gate(draft, state['frozen'])
        state.update(gate_task_id=gate_id, intent='create_worktree')
        ledger.save(key, state)
        source.call('worktree', 'add', '-b', draft.data['branch'], str(target), draft.data['base'],
                    child_umask=0o022)
        state['intent'] = 'prepare_exact_plan'
        ledger.save(key, state)
        template = task_plan(policy, row, proposal, gate_id, board=draft.data['board'])
        owned = runtime_factory(template)
        owned.identity()
        prepared = prepare_plan(template, proposal, owned.git)
        _check_source(prepared, state['frozen'])
        state['intent'] = 'publish_plan'
        ledger.save(key, state)
        _publish_plan(prepared, path)
        owned.identity()
        _check_source(prepared, state['frozen'])
        result = {'stage':'awaiting_scope_review', 'task_id':row['id'],
                  'gate_task_id':gate_id, 'plan_path':str(path), 'plan_hash':prepared.digest}
        state.update(stage='awaiting_scope_review', intent=None, result=result)
        ledger.save(key, state)
        return result
    except Exception as exc:
        state.update(stage='halted', error_type=type(exc).__name__, error_detail=str(exc))
        ledger.save(key, state)
        raise Halt('staging_failed') from exc


def stage_task(policy, task_id, proposal, *, board, runtime_factory=None):
    """Own staging once; uncertain effects halt rather than being adopted or retried."""
    import os
    from plugins.fleet_lifecycle.controller import Ledger, Native, safe_path
    from plugins.fleet_lifecycle.runtime import Git, Runtime
    runtime_factory = runtime_factory or Runtime
    policy, proposal = copy.deepcopy(policy), copy.deepcopy(proposal)
    token(task_id, r't_[0-9a-f]{8}')
    row = _read_source(board, task_id)
    # An unpublished syntax-check ID only; the actual gate comes from the kernel.
    temporary_gate = 't_00000000' if task_id != 't_00000000' else 't_ffffffff'
    fields(proposal, 'candidate_paths test_files context')
    draft = task_plan(policy, row, proposal, temporary_gate, board=board)
    from plugins.fleet_lifecycle.planning import prepare_plan
    runtime = runtime_factory(draft)
    runtime.principal()
    runtime.installation_identity()
    frozen = Native.frozen(row)
    require(Native.frozen(_read_source(board, task_id)) == frozen, 'identity_drift')
    project = project_policy(policy, row['branch_name'].split('/', 1)[0], board=board)
    home = Path(draft.data['profiles']['director']['home'])
    key = digest(canonical({'board':board, 'task_id':task_id}))
    binding = {'policy_hash':digest(canonical(policy)), 'proposal_hash':digest(canonical(proposal)),
               'frozen':frozen}
    with Ledger(home / 'fleet-task-staging') as ledger:
        require(ledger is not None, 'staging_busy')
        existing = ledger.load(key)
        if existing is not None:
            require(all(existing.get(k) == v for k,v in binding.items()), 'identity_drift')
            require(existing.get('stage') == 'awaiting_scope_review' and existing.get('intent') is None,
                    'staging_interrupted')
            saved = Plan.load(Path(existing['result']['plan_path']))
            owned = runtime_factory(saved)
            owned.identity()
            require(prepare_plan(saved, proposal, owned.git).digest == saved.digest, 'identity_drift')
            _check_source(saved, frozen)
            require(saved.digest == existing['result']['plan_hash'], 'identity_drift')
            return existing['result']
        source = Git(runtime.git_binary, project['repository'], runtime.env)
        safe_path(Path(project['repository']), directory=True)
        source.guard(project['source_branch'], project['base'])
        target = Path(draft.data['workspace'])
        path = home / 'fleet-lifecycle-plans' / (task_id + '.json')
        require(not os.path.lexists(target) and not os.path.lexists(path), 'identity_drift')
        if target.parent.exists():
            safe_path(target.parent, directory=True)
        if path.parent.exists():
            safe_path(path.parent, directory=True, private=True)
        require(not source.call('show-ref', '--verify', 'refs/heads/' + draft.data['branch'], ok=False).strip(),
                'identity_drift')
        state = {**binding, 'stage':'preparing', 'intent':None,
                 'workspace':str(target), 'branch':draft.data['branch']}
        ledger.save(key, state)
        return _stage_new(policy, row, proposal, draft, runtime, source, target, path,
                          ledger, key, state, runtime_factory)


def stage_command(args):
    from hermes_constants import get_hermes_home
    from plugins.fleet_lifecycle.controller import read_regular, safe_path, strict_json
    # Deployment-owned profile policy, never a caller-supplied authority file.
    path = get_hermes_home() / 'fleet-lifecycle-policy.json'
    safe_path(path, private=True)
    policy = strict_json(read_regular(path, 128 * 1024))
    proposal = strict_json(read_regular(Path(args.proposal), 64 * 1024))
    result = stage_task(policy, args.task, proposal, board=args.board)
    print(canonical(result).decode())
    return 0

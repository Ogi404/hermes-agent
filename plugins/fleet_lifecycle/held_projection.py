"""Trusted producer projection of a validated held snapshot; never readiness.

The source-facing caller must authenticate the capture, read the current source
and bind the manifest. This native transaction accepts no caller execution recipe
and never creates a workspace, gate, claim, worker or eligibility transition.
"""
from __future__ import annotations

from pathlib import Path

from plugins.fleet_lifecycle.controller import canonical, fields, require, token
from plugins.fleet_lifecycle.intake import project_policy


def project_held_contract(policy, contract, project_id, *, board, conn):
    from hermes_cli import kanban_db as kb, kanban_db_notify as notify
    from hermes_cli.kanban_contract_guard import (
        _extract_contract, fleet_contract_planning_guard_reason,
    )
    from hermes_cli.kanban_fleet_project_registry import fleet_project_record

    require(isinstance(contract, dict), 'held_snapshot_invalid')
    token(contract.get('linear_id'), r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
    token(contract.get('issue_id'), r'[A-Z][A-Z0-9]*-[1-9][0-9]*')
    project = project_policy(policy, contract.get('repository'), board=board)
    require(fleet_project_record(project_id) == (project['slug'], project['repository']), 'identity_drift')
    require(contract.get('schema_version') == 2 and contract.get('agent_ready') is False
            and contract.get('labels') == [] and contract.get('blocked') is False
            and contract.get('redactions') == [] and contract.get('task_lane') == 'implementation'
            and contract.get('execution_mode') == 'implement', 'held_snapshot_invalid')
    for role, lane in [('director', 'orchestration'), ('builder', 'implementation'), ('reviewer', 'review')]:
        require(contract.get(lane + '_profiles') == [policy['installation']['profiles'][role]['name']],
                'identity_drift')
    title = contract['issue_id'] + ': ' + contract['title']
    body = ('Frozen Linear contract. This snapshot is the worker\'s only scope authority.\n\n'
            '<!-- FLEET_CONTRACT_SNAPSHOT_V1 BEGIN -->\n```json\n'
            + canonical(contract).decode() + '\n```\n<!-- FLEET_CONTRACT_SNAPSHOT_V1 END -->\n')
    require(len(body.encode()) <= 7800, 'held_snapshot_invalid')
    key = 'linear:' + contract['linear_id']
    route = policy['installation']['slack']
    expected = {'created_by': 'fleet-linear-reconciler', 'idempotency_key': key,
                'project_id': project_id, 'workspace_kind': 'worktree', 'title': title,
                'assignee': policy['installation']['profiles']['builder']['name'],
                'status': 'blocked', 'claim_lock': None, 'current_run_id': None, 'worker_pid': None,
                'workflow_template_id': None, 'current_step_key': None,
                'model_override': None, 'provider_override': None, 'max_retries': 1}

    def validate(row):
        require(all(row[k] == v for k, v in expected.items()), 'identity_drift')
        require(row['workspace_path'] == str(Path(project['repository']) / '.worktrees' / row['id'])
                and row['branch_name'].startswith(project['slug'] + '/'), 'identity_drift')
        current, error = _extract_contract(row['body'])
        require(error is None and current == contract, 'identity_drift')
        require(fleet_contract_planning_guard_reason(row) is None, 'held_snapshot_invalid')
        require(not conn.execute('SELECT 1 FROM task_runs WHERE task_id=?', (row['id'],)).fetchone(),
                'handoff_invalid')

    # Validate the complete snapshot before create. A planning-only guard never
    # makes this prospective or eventual row eligible for the dispatcher.
    prospective = {**expected, 'id': 't_00000000', 'body': body,
                   'workspace_path': str(Path(project['repository']) / '.worktrees' / 't_00000000'),
                   'branch_name': project['slug'] + '/held-planning'}
    require(fleet_contract_planning_guard_reason(prospective) is None, 'held_snapshot_invalid')
    with kb.write_txn(conn):
        existing = conn.execute('SELECT * FROM tasks WHERE idempotency_key=?', (key,)).fetchall()
        require(len(existing) <= 1, 'identity_drift')
        if existing:
            row = existing[0]
            validate(row)
        else:
            tid = kb.create_task(conn, title=title, body=body, assignee=expected['assignee'],
                                 created_by=expected['created_by'], workspace_kind='worktree',
                                 project_id=project_id, board=board, idempotency_key=key,
                                 initial_status='blocked', max_retries=1, skills=['fleet-build'])
            row = conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
            validate(row)
            notify.add_notify_sub(conn, task_id=tid, platform='slack', chat_id=route['channel'],
                                  thread_id=route['thread'], chat_type='channel',
                                  notifier_profile=route['profile'], delivery_mode='notify',
                                  delivery_metadata={'slack_team_id': route['team']}, allow_nested=True)
        subscriptions = [s for s in notify.list_notify_subs(conn, row['id'])
                         if s['platform'] == 'slack' and s['chat_id'] == route['channel']
                         and (s.get('thread_id') or '') == route['thread']
                         and s['notifier_profile'] == route['profile'] and s['delivery_mode'] == 'notify'
                         and (s.get('delivery_metadata') or {}).get('slack_team_id') == route['team']]
        require(len(subscriptions) == 1, 'identity_drift')
        result = {'stage': 'held_native_projected', 'task_id': row['id'],
                  'contract_hash': contract['contract_sha256'], 'execution_authorized': False}
    return result

"""Ordinary task handoff: real native records with explicit external boundaries."""
import json

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_notify as notify
from plugins.fleet_lifecycle.controller import Halt, canonical
from tests.hermes_cli.conftest import all_assignees_spawnable
from tests.hermes_cli.test_kanban_fleet_contract_guard import (
    fleet_home, _role_contract, _create_fleet_task,
)
from tests.plugins.test_fleet_lifecycle_controller import native_lifecycle
from tests.plugins.test_fleet_task_intake import source
from tests.plugins.test_fleet_task_staging import staging


def test_implementation_waits_for_scope_without_stranding_valid_sibling(fleet_home, all_assignees_spawnable):
    _home, pid = fleet_home
    calls = []
    with kbc.connect() as conn:
        held = _create_fleet_task(conn, pid, _role_contract(execution_mode='implement'))
        sibling = kb.create_task(conn, title='Ordinary sibling', assignee='builder')
        result = dispatch.dispatch_once(conn, max_spawn=1,
            spawn_fn=lambda task, *_args, **_kwargs: calls.append(task.id))
        assert calls == [sibling]
        assert result.contract_guarded == [(held, 'controller_scope_not_admitted')]
        row = kb.get_task(conn, held)
        assert row.status == 'blocked' and row.current_run_id is None and row.worker_pid is None
        runs = conn.execute('SELECT * FROM task_runs WHERE task_id=?', (held,)).fetchall()
        assert all(run['ended_at'] is not None and run['worker_pid'] is None for run in runs)
        assert not conn.execute('SELECT 1 FROM task_events WHERE task_id=? AND kind IN (?,?)',
                                (held, 'claimed', 'spawned')).fetchone()


def test_admission_publishes_full_plan_once_without_dispatch(native_lifecycle):
    controller, _backend, tid, _request = native_lifecycle
    state = controller.admit()
    assert state['stage'] == 'waiting_source'
    with kbc.connect() as conn:
        comments = kb.list_comments(conn, tid)
        bound = [c for c in comments if c.author == 'trusted-controller'
                 and 'FLEET_ADMITTED_PLAN_V1' in c.body]
        assert len(bound) == 1, 'Builder received only a host plan path/hash, not the complete admitted plan'
        body = bound[0].body
        assert controller.plan.digest in body
        payload = body.split('```json\n', 1)[1].split('\n```', 1)[0]
        assert json.loads(payload) == controller.plan.data
        assert canonical(controller.plan.data).decode() in body
        assert 'kanban_show' in body and 'comments' in body
        assert not conn.execute('SELECT 1 FROM task_runs WHERE task_id=?', (tid,)).fetchone()
        before = [(c.id, c.body) for c in comments]
    assert controller.admit()['stage'] == 'waiting_source'
    with kbc.connect() as conn:
        assert [(c.id, c.body) for c in kb.list_comments(conn, tid)] == before
        assert kb.get_task(conn, tid).status == 'blocked'


def test_context_publication_failure_rolls_back_native_admission(native_lifecycle, monkeypatch):
    controller, _backend, tid, _request = native_lifecycle
    def fail(*_args, **_kwargs):
        raise RuntimeError('fixture context publication failure')
    monkeypatch.setattr(kb, 'add_comment', fail)
    assert controller.admit()['stage'] == 'halted'
    with kbc.connect() as conn:
        row = kb.get_task(conn, tid)
        assert row.workflow_template_id is None and row.current_step_key is None
        assert not conn.execute('SELECT 1 FROM task_events WHERE task_id=? AND kind=?',
                                (tid, 'fleet_controller_admitted')).fetchone()
        assert kb.list_comments(conn, tid) == []
        assert not conn.execute('SELECT 1 FROM task_runs WHERE task_id=?', (tid,)).fetchone()


def test_staging_completes_missing_source_team_metadata_from_trusted_policy(staging):
    from plugins.fleet_lifecycle.intake import stage_task
    policy, row, proposal, _git, runtime, _calls = staging
    with kbc.connect() as conn:
        conn.execute('UPDATE kanban_notify_subs SET delivery_metadata=NULL WHERE task_id=?', (row['id'],))
        conn.commit()
    result = stage_task(policy, row['id'], proposal, board='default', runtime_factory=runtime)
    assert result['stage'] == 'awaiting_scope_review'
    with kbc.connect() as conn:
        subs = notify.list_notify_subs(conn, row['id'])
        assert len(subs) == 1 and subs[0]['delivery_metadata']['slack_team_id'] == 'TFIXTURE'
        assert kb.get_task(conn, row['id']).status == 'blocked'
        assert not conn.execute('SELECT 1 FROM task_runs').fetchone()


def test_staging_never_replaces_an_existing_foreign_team(staging):
    from plugins.fleet_lifecycle.intake import stage_task
    policy, row, proposal, _git, runtime, _calls = staging
    with kbc.connect() as conn:
        notify.add_notify_sub(conn, task_id=row['id'], platform='slack', chat_id='CFIXTURE',
                              notifier_profile='default', delivery_mode='notify',
                              delivery_metadata={'slack_team_id':'TOTHER'})
        before = notify.list_notify_subs(conn, row['id'])
        tasks = conn.execute('SELECT count(*) FROM tasks').fetchone()[0]
    with pytest.raises(Halt):
        stage_task(policy, row['id'], proposal, board='default', runtime_factory=runtime)
    with kbc.connect() as conn:
        assert notify.list_notify_subs(conn, row['id']) == before
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == tasks
        assert not conn.execute('SELECT 1 FROM task_runs').fetchone()

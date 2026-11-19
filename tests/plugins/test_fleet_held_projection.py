"""Trusted held snapshot projection: real native transactions, no live sources."""
import copy
import importlib.util
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli.kanban_contract_guard import _extract_contract, _contract_hash, fleet_contract_guard_reason
from tests.hermes_cli.test_kanban_fleet_contract_guard import fleet_home
from tests.plugins.test_fleet_task_intake import source
from plugins.fleet_lifecycle.controller import Halt


def projection():
    assert importlib.util.find_spec('plugins.fleet_lifecycle.held_projection') is not None,'missing reusable held snapshot to born-blocked native projection'
    from plugins.fleet_lifecycle.held_projection import project_held_contract
    return project_held_contract


@pytest.fixture
def held_source(source):
    policy,row,_scope,_gate=source
    c,error=_extract_contract(row['body']);assert error is None
    c.update(issue_id='FIXTURE-9',linear_id='44444444-4444-4444-8444-444444444444',title='A fresh held fixture',agent_ready=False,labels=[],status='Backlog')
    c['contract_sha256']=_contract_hash(c)
    return policy,c,row['project_id']


def test_projection_creates_exact_born_blocked_native_identity_without_eligibility_or_workspace(held_source):
    policy,c,pid=held_source;before=copy.deepcopy((policy,c))
    with kbc.connect() as conn:
        result=projection()(policy,c,pid,board='default',conn=conn)
        row=dict(conn.execute('SELECT * FROM tasks WHERE id=?',(result['task_id'],)).fetchone())
        assert row['status']=='blocked' and row['project_id']==pid
        assert row['created_by']=='fleet-linear-reconciler' and row['idempotency_key']=='linear:'+c['linear_id']
        frozen,error=_extract_contract(row['body']);assert error is None and frozen==c
        assert row['title']==c['issue_id']+': '+c['title']
        assert row['assignee']=='builder' and row['max_retries']==1
        assert row['workspace_path']==str(Path(policy['projects'][0]['repository'])/'.worktrees'/row['id'])
        assert not Path(row['workspace_path']).exists()
        assert fleet_contract_guard_reason(row)=='snapshot_not_agent_ready'
        assert all(row[k] is None for k in ['claim_lock','current_run_id','worker_pid','workflow_template_id','current_step_key'])
        assert not conn.execute('SELECT 1 FROM task_runs').fetchone()
        events=[__import__('json').loads(x['payload']) for x in conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind='created'",(row['id'],))]
        assert len(events)==1 and events[0]['status']=='blocked'
        count=conn.execute('SELECT COUNT(*) FROM task_events').fetchone()[0]
        assert projection()(policy,c,pid,board='default',conn=conn)==result
        assert conn.execute('SELECT COUNT(*) FROM task_events').fetchone()[0]==count
    assert (policy,c)==before and result['execution_authorized'] is False


@pytest.mark.parametrize('case',['ready','hash','wrong_project','wrong_board','wrong_role','manual_identity','redacted','blocked'])
def test_invalid_source_rejects_before_any_native_creation(held_source,case):
    policy,c,pid=held_source
    if case=='ready':c['agent_ready']=True;c['labels']=['agent-ready']
    elif case=='hash':c['contract_sha256']='f'*64
    elif case=='wrong_project':pid='p_other'
    elif case=='wrong_board':policy['projects'][0]['board']='other'
    elif case=='wrong_role':c['implementation_profiles']=['director']
    elif case=='manual_identity':c['linear_id']='invalid'
    elif case=='redacted':c['redactions']=['secret']
    else:c['blocked']=True
    if case!='hash':c['contract_sha256']=_contract_hash(c)
    with kbc.connect() as conn:
        before=[tuple(x) for x in conn.execute('SELECT * FROM task_events ORDER BY id')]
        with pytest.raises(Halt):projection()(policy,c,pid,board='default',conn=conn)
        assert [tuple(x) for x in conn.execute('SELECT * FROM task_events ORDER BY id')]==before


def test_existing_wrong_scope_or_claim_is_not_adopted(held_source):
    policy,c,pid=held_source
    with kbc.connect() as conn:
        result=projection()(policy,c,pid,board='default',conn=conn)
        conn.execute("UPDATE tasks SET title='foreign scope' WHERE id=?",(result['task_id'],));conn.commit()
        before=conn.execute('SELECT COUNT(*) FROM task_events').fetchone()[0]
        with pytest.raises(Halt):projection()(policy,c,pid,board='default',conn=conn)
        assert conn.execute('SELECT COUNT(*) FROM task_events').fetchone()[0]==before


def test_postcreate_validation_failure_rolls_back_task_and_events(held_source,monkeypatch):
    project=projection()
    from plugins.fleet_lifecycle import held_projection
    policy,c,pid=held_source
    original=kb.create_task
    def corrupt(conn,**kw):
        tid=original(conn,**kw);conn.execute("UPDATE tasks SET status='ready' WHERE id=?",(tid,));return tid
    monkeypatch.setattr(kb,'create_task',corrupt)
    with kbc.connect() as conn:
        before=[tuple(x) for x in conn.execute('SELECT * FROM tasks ORDER BY id')]
        with pytest.raises(Halt):project(policy,c,pid,board='default',conn=conn)
        assert [tuple(x) for x in conn.execute('SELECT * FROM tasks ORDER BY id')]==before
        assert not conn.execute('SELECT 1 FROM task_runs').fetchone()

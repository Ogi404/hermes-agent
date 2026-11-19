"""Held scope preparation reads exact source but grants no admission/dispatch."""
import argparse
import copy
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_contract_guard import _extract_contract, _contract_hash, fleet_contract_guard_reason
from tests.hermes_cli.test_kanban_fleet_contract_guard import fleet_home
from tests.plugins.test_fleet_task_intake import source
from tests.plugins.test_fleet_task_staging import staging
from plugins.fleet_lifecycle.controller import Halt, Native, Plan, setup_cli


def consumer():
    from plugins.fleet_lifecycle import intake
    fn=getattr(intake,'prepare_held_scope',None)
    assert callable(fn),'missing executable held planning consumer; ordinary stage-task must stay strict'
    return fn


@pytest.fixture
def held_staging(staging):
    policy,row,proposal,git,runtime,calls=staging
    c,error=_extract_contract(row['body']);assert error is None
    c.update(agent_ready=False,labels=[],status='Backlog');c['contract_sha256']=_contract_hash(c)
    body='<!-- FLEET_CONTRACT_SNAPSHOT_V1 BEGIN -->\n```json\n'+json.dumps(c)+'\n```\n<!-- FLEET_CONTRACT_SNAPSHOT_V1 END -->'
    with kbc.connect() as conn:
        conn.execute('UPDATE tasks SET body=? WHERE id=?',(body,row['id']));conn.commit()
        row=dict(conn.execute('SELECT * FROM tasks WHERE id=?',(row['id'],)).fetchone())
    return policy,row,proposal,git,runtime,calls


def test_held_scope_reads_base_preimages_without_worktree_gate_plan_or_readiness_mutation(held_staging):
    policy,row,proposal,git,runtime,calls=held_staging;before=copy.deepcopy((policy,proposal))
    with kbc.connect() as conn:events=[tuple(x) for x in conn.execute('SELECT * FROM task_events ORDER BY id')]
    result=consumer()(policy,row['id'],proposal,board='default',runtime_factory=runtime)
    assert result['stage']=='held_scope_prepared_not_admitted' and result['execution_authorized'] is False
    assert result['requires_eligibility_refresh'] is True
    plan=Plan(result['plan'],result['plan_hash']);Plan.validate(plan.data)
    assert plan.data['task_id']==row['id'] and plan.data['base']==policy['projects'][0]['base']
    assert plan.data['changes'][0]['base_sha256'] is not None
    assert not Path(row['workspace_path']).exists()
    assert not (Path(policy['installation']['profiles']['director']['home'])/'fleet-lifecycle-plans').exists()
    with kbc.connect() as conn:
        assert [tuple(x) for x in conn.execute('SELECT * FROM task_events ORDER BY id')]==events
        actual=dict(conn.execute('SELECT * FROM tasks WHERE id=?',(row['id'],)).fetchone());assert actual==row
        assert fleet_contract_guard_reason(actual)=='snapshot_not_agent_ready'
        with pytest.raises(Halt):Native(plan).task(conn)
    assert (policy,proposal)==before


@pytest.mark.parametrize('case',['occupied_workspace','source_dirty','claimed','scope_override'])
def test_held_preparation_rejects_drift_without_any_native_or_plan_side_effect(held_staging,case):
    policy,row,proposal,git,runtime,calls=held_staging
    if case=='occupied_workspace':Path(row['workspace_path']).mkdir(parents=True)
    elif case=='source_dirty':(git.workspace/'example.py').write_text('changed')
    elif case=='claimed':
        with kbc.connect() as conn:conn.execute('UPDATE tasks SET claim_lock=? WHERE id=?',('other',row['id']));conn.commit()
    else:proposal['native']={'model':'other'}
    with kbc.connect() as conn:events=[tuple(x) for x in conn.execute('SELECT * FROM task_events ORDER BY id')]
    with pytest.raises(Halt):consumer()(policy,row['id'],proposal,board='default',runtime_factory=runtime)
    with kbc.connect() as conn:assert [tuple(x) for x in conn.execute('SELECT * FROM task_events ORDER BY id')]==events


def test_cli_has_a_distinct_held_planning_command_without_admit_or_ready_flags(tmp_path):
    p=argparse.ArgumentParser();setup_cli(p)
    try:a=p.parse_args(['plan-held-task','--board','default','--task','t_12345678','--proposal',str(tmp_path/'proposal.json')])
    except SystemExit:pytest.fail('missing native held planning command')
    assert a.action=='plan-held-task' and not any(hasattr(a,k) for k in ['ready','admit','plan'])

"""Held planning validates full scope without granting dispatch eligibility."""
import copy
import json

import pytest

from hermes_cli import kanban_contract_guard as guard
from tests.hermes_cli.test_kanban_fleet_contract_guard import fleet_home
from tests.plugins.test_fleet_task_intake import source


def held_row(source):
    _policy,row,_proposal,_gate=source
    row=copy.deepcopy(row)
    contract,error=guard._extract_contract(row['body'])
    assert error is None
    contract['agent_ready']=False
    contract['labels']=[]
    contract['contract_sha256']=guard._contract_hash(contract)
    row['body']='Frozen Linear contract.\n\n<!-- FLEET_CONTRACT_SNAPSHOT_V1 BEGIN -->\n```json\n'+json.dumps(contract,sort_keys=True)+'\n```\n<!-- FLEET_CONTRACT_SNAPSHOT_V1 END -->\n'
    return row,contract


def planning_guard(row):
    fn=getattr(guard,'fleet_contract_planning_guard_reason',None)
    assert callable(fn),'missing distinct held-planning snapshot validator; dispatch guard must not be relaxed'
    return fn(row)


def test_genuine_unready_held_scope_can_be_inspected_but_remains_undispatchable(source):
    row,_contract=held_row(source)
    before=copy.deepcopy(row)
    assert guard.fleet_contract_guard_reason(row)=='snapshot_not_agent_ready'
    assert planning_guard(row) is None
    assert row==before
    assert guard.fleet_contract_guard_reason(row,dispatch_lane='ready')=='snapshot_not_agent_ready'


@pytest.mark.parametrize('case',['hash','title','role','project','provenance','idempotency','blocked','redacted'])
def test_unready_does_not_short_circuit_other_snapshot_guards(source,case):
    row,c=held_row(source)
    if case=='hash':c['contract_sha256']='f'*64
    elif case=='title':row['title']='Copied title'
    elif case=='role':c['implementation_profiles']=['director']
    elif case=='project':row['project_id']='p_missing_project'
    elif case=='provenance':row['created_by']='manual'
    elif case=='idempotency':row['idempotency_key']='linear:other'
    elif case=='blocked':c['blocked']=True
    else:c['redactions']=['secret']
    if case!='hash':c['contract_sha256']=guard._contract_hash(c)
    row['body']='<!-- FLEET_CONTRACT_SNAPSHOT_V1 BEGIN -->\n```json\n'+json.dumps(c)+'\n```\n<!-- FLEET_CONTRACT_SNAPSHOT_V1 END -->'
    assert planning_guard(row) is not None


@pytest.mark.parametrize('field,value',[('status','ready'),('status','running'),('claim_lock','foreign'),('current_run_id',1),('worker_pid',123),('workflow_template_id','fleet-source-review-v1'),('current_step_key','waiting_source')])
def test_held_planning_refuses_live_claims_or_admitted_tasks(source,field,value):
    row,_=held_row(source);row[field]=value
    assert planning_guard(row) is not None


def test_existing_ready_scope_positive_control_remains_valid(source):
    _policy,row,_proposal,_gate=source
    assert guard.fleet_contract_guard_reason(row) is None
    assert planning_guard(row) is None

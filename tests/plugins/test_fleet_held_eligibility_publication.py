"""Actual native gates and transactions, substituted remote source boundary only."""
import copy
from pathlib import Path

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_contract_guard import _extract_contract, fleet_contract_guard_reason
from hermes_cli.kanban_human_gate import request_gate, decide_gate
from tests.hermes_cli.test_kanban_fleet_contract_guard import fleet_home
from tests.plugins.test_fleet_task_intake import source
from tests.plugins.test_fleet_task_staging import staging
from tests.plugins.test_fleet_held_scope_preparation import held_staging
from tests.plugins.test_fleet_held_eligibility_preparation import held, eligibility
from plugins.fleet_lifecycle.controller import Halt


def publication():
 fn=getattr(eligibility(),'publish_eligibility',None)
 assert callable(fn),'missing reusable bounded eligibility publication path'
 return fn


@pytest.fixture
def prepared(held):
 policy,row,proposal,git,runtime,calls=held;module=eligibility()
 result=module.prepare_eligibility(policy,row['id'],proposal,board='default',runtime_factory=runtime)
 contract,error=_extract_contract(row['body']);assert error is None
 detail={k:contract[v] for k,v in {'id':'issue_id','uuid':'linear_id','title':'title','description':'description','status':'status','url':'url','labels':'labels','relations':'relations','updatedAt':'source_updated_at'}.items()}
 detail.update(project='fixture-project',projectId='fixture-project-id',teamId='fixture-team',assigneeId=None,delegateId=None)
 detail=copy.deepcopy(detail);operations=[]
 def read(issue_id):
  assert issue_id==contract['issue_id'];operations.append(('read',issue_id));return copy.deepcopy(detail)
 def update(uuid):
  assert uuid==contract['linear_id'];operations.append(('update',uuid));detail.update(labels=['agent-ready'],updatedAt='2026-11-20T07:00:00Z');return {'id':contract['issue_id']}
 def normalize(raw):
  value=copy.deepcopy(contract)
  for k,v in {'issue_id':'id','linear_id':'uuid','title':'title','description':'description','status':'status','url':'url','labels':'labels','relations':'relations','source_updated_at':'updatedAt'}.items():value[k]=raw[v]
  return value
 return policy,row,proposal,runtime,result,detail,operations,read,update,normalize


def approve(data,*,actor=None,platform='slack',sha=None,proof=None):
 policy,row,proposal,runtime,result,*_=data
 with kbc.connect() as conn:
  eid,_=request_gate(conn,result['gate_task_id'],stage='push',sha=sha or result['source_sha'],tests=proof or result['proof'],reviewer_verdict='APPROVE',requested_by='fixture-controller')
  decide_gate(conn,result['gate_task_id'],stage='push',decision='approve',sha=sha or result['source_sha'],actor=actor or policy['approval']['actor'],platform=platform)
 return eid


def invoke(data):
 policy,row,proposal,runtime,result,detail,operations,read,update,normalize=data
 return publication()(policy,row['id'],board='default',read_issue=read,update_issue=update,normalize_source=normalize,runtime_factory=runtime)


def test_authentic_approval_changes_only_existing_readiness_and_same_blocked_snapshot_once(prepared):
 policy,row,proposal,runtime,result,detail,operations,*_=prepared;before=copy.deepcopy(detail);approve(prepared)
 out=invoke(prepared)
 assert out['stage']=='eligible_native_still_blocked' and out['execution_authorized'] is False and out['fresh_staging_required'] is True
 assert sum(op[0]=='update' for op in operations)==1 and sum(op[0]=='read' for op in operations)<=3
 assert {k:v for k,v in detail.items() if k not in ['labels','updatedAt']}=={k:v for k,v in before.items() if k not in ['labels','updatedAt']}
 with kbc.connect() as conn:
  after=dict(conn.execute('SELECT * FROM tasks WHERE id=?',(row['id'],)).fetchone())
  assert after['status']=='blocked' and fleet_contract_guard_reason(after) is None
  assert all(v==after[k] for k,v in row.items() if k!='body')
  assert not conn.execute('SELECT 1 FROM task_runs').fetchone()
 assert not Path(row['workspace_path']).exists()
 consumed=list(operations)
 with pytest.raises(Halt,match='eligibility_already_attempted'):invoke(prepared)
 assert operations==consumed


@pytest.mark.parametrize('case',['missing','actor','platform','sha','proof','scope'])
def test_invalid_gate_or_scope_causes_zero_remote_writes_and_native_refresh(prepared,case):
 policy,row,proposal,runtime,result,detail,operations,*_=prepared
 if case!='missing':
  kwargs={}
  if case=='actor':kwargs['actor']='UOTHER'
  elif case=='platform':kwargs['platform']='cli'
  elif case=='sha':kwargs['sha']='9'*40
  elif case=='proof':kwargs['proof']='wrong-proof'
  approve(prepared,**kwargs)
 if case=='scope':
  with kbc.connect() as conn:conn.execute('UPDATE tasks SET title=? WHERE id=?',('changed scope',row['id']));conn.commit()
 with kbc.connect() as conn:before=[tuple(x) for x in conn.execute('SELECT * FROM tasks ORDER BY id')]
 with pytest.raises(Halt):invoke(prepared)
 assert not any(x[0]=='update' for x in operations)
 with kbc.connect() as conn:assert [tuple(x) for x in conn.execute('SELECT * FROM tasks ORDER BY id')]==before


def test_changed_remote_scope_rejects_without_publishing_readiness(prepared):
 approve(prepared);prepared[5]['description']='source changed after scope approval'
 with pytest.raises(Halt):invoke(prepared)
 assert not any(x[0]=='update' for x in prepared[6])


def test_uncertain_source_update_is_durable_and_never_replayed(prepared):
 approve(prepared);data=list(prepared);original=data[8]
 def uncertain(uuid):original(uuid);raise OSError('fixture response lost after actual update')
 data[8]=uncertain
 with pytest.raises(Halt,match='eligibility_publication_stopped'):invoke(data)
 assert sum(x[0]=='update' for x in data[6])==1
 with pytest.raises(Halt,match='eligibility_already_attempted'):invoke(data)
 assert sum(x[0]=='update' for x in data[6])==1
 with kbc.connect() as conn:assert conn.execute('SELECT body FROM tasks WHERE id=?',(data[1]['id'],)).fetchone()[0]==data[1]['body']


def test_unrelated_postwrite_change_does_not_publish_a_native_success(prepared):
 approve(prepared);data=list(prepared);original=data[8]
 def drifting(uuid):reply=original(uuid);data[5]['description']='unexpected source edit';return reply
 data[8]=drifting
 with pytest.raises(Halt,match='eligibility_publication_stopped'):invoke(data)
 assert sum(x[0]=='update' for x in data[6])==1
 with kbc.connect() as conn:assert conn.execute('SELECT body FROM tasks WHERE id=?',(data[1]['id'],)).fetchone()[0]==data[1]['body']

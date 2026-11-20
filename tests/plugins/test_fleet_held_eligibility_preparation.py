"""Reusable held-scope eligibility preparation never publishes readiness."""
import argparse
import copy
import importlib.util
import os
from pathlib import Path

import pytest

from hermes_cli import kanban_db_connect as kbc, kanban_db_notify as notify
from tests.hermes_cli.test_kanban_fleet_contract_guard import fleet_home
from tests.plugins.test_fleet_task_intake import source
from tests.plugins.test_fleet_task_staging import staging
from tests.plugins.test_fleet_held_scope_preparation import held_staging
from plugins.fleet_lifecycle.controller import Halt, setup_cli


def eligibility():
 assert importlib.util.find_spec('plugins.fleet_lifecycle.eligibility') is not None,'missing reusable approval-to-source-eligibility consumer'
 from plugins.fleet_lifecycle import eligibility as module
 return module


@pytest.fixture
def held(held_staging):
 policy,row,proposal,git,runtime,calls=held_staging
 home=Path(policy['installation']['profiles']['director']['home']);home.mkdir(parents=True,exist_ok=True)
 for path in [home,*home.parents]:
  st=path.stat()
  if st.st_uid==os.getuid() and not st.st_mode&0o1000:os.chmod(path,0o700)
 # The production role snapshot deliberately has project_slug="app" while
 # repository="ensological". Project routing is repository-authoritative.
 from hermes_cli.kanban_contract_guard import _extract_contract, _contract_hash
 from plugins.fleet_lifecycle.controller import canonical
 contract,error=_extract_contract(row['body']);assert error is None
 contract['project_slug']='app';contract['contract_sha256']=_contract_hash(contract)
 body='<!-- FLEET_CONTRACT_SNAPSHOT_V1 BEGIN -->\n```json\n'+canonical(contract).decode()+'\n```\n<!-- FLEET_CONTRACT_SNAPSHOT_V1 END -->'
 with kbc.connect() as conn:
  conn.execute('UPDATE tasks SET body=? WHERE id=?',(body,row['id']));conn.commit()
  row=dict(conn.execute('SELECT * FROM tasks WHERE id=?',(row['id'],)).fetchone())
 assert contract['project_slug']!=contract['repository']
 return policy,row,proposal,git,runtime,calls


def test_prepare_is_reusable_scope_bound_and_idempotent_without_source_or_worker_effects(held):
 policy,row,proposal,git,runtime,calls=held;before=copy.deepcopy((policy,proposal))
 module=eligibility()
 result=module.prepare_eligibility(policy,row['id'],proposal,board='default',runtime_factory=runtime)
 assert result['stage']=='awaiting_eligibility_review' and result['execution_authorized'] is False
 assert result['source_sha']==policy['installation']['source_sha']
 assert result['proof'].startswith('held-eligibility:v1:')
 assert result['task_id']==row['id'] and result['gate_task_id']!=row['id']
 with kbc.connect() as conn:
  gate=dict(conn.execute('SELECT * FROM tasks WHERE id=?',(result['gate_task_id'],)).fetchone())
  assert gate['status']=='blocked' and gate['workspace_kind']=='scratch'
  assert not conn.execute('SELECT 1 FROM task_runs').fetchone()
  assert dict(conn.execute('SELECT * FROM tasks WHERE id=?',(row['id'],)).fetchone())==row
  subs=notify.list_notify_subs(conn,gate['id']);assert len(subs)==1 and subs[0]['delivery_metadata']['slack_team_id']==policy['installation']['slack']['team']
  events=conn.execute('SELECT COUNT(*) FROM task_events').fetchone()[0]
 assert module.prepare_eligibility(policy,row['id'],proposal,board='default',runtime_factory=runtime)==result
 with kbc.connect() as conn:assert conn.execute('SELECT COUNT(*) FROM task_events').fetchone()[0]==events
 assert not Path(row['workspace_path']).exists() and (policy,proposal)==before


def test_changed_scope_cannot_reuse_the_pending_eligibility_gate(held):
 policy,row,proposal,git,runtime,calls=held;module=eligibility()
 module.prepare_eligibility(policy,row['id'],proposal,board='default',runtime_factory=runtime)
 changed=copy.deepcopy(proposal);changed['context']=[]
 with kbc.connect() as conn:before=[tuple(r) for r in conn.execute('SELECT * FROM tasks ORDER BY id')]
 with pytest.raises(Halt):module.prepare_eligibility(policy,row['id'],changed,board='default',runtime_factory=runtime)
 with kbc.connect() as conn:assert [tuple(r) for r in conn.execute('SELECT * FROM tasks ORDER BY id')]==before


def test_cli_exposes_separate_prepare_and_publish_not_arbitrary_source_write_fields():
 parser=argparse.ArgumentParser();setup_cli(parser)
 try:
  prepare=parser.parse_args(['prepare-eligibility','--board','default','--task','t_12345678','--proposal','/fixture/proposal.json'])
  publish=parser.parse_args(['publish-eligibility','--board','default','--task','t_12345678'])
 except SystemExit:pytest.fail('missing reusable eligibility CLI stages')
 assert prepare.action=='prepare-eligibility' and publish.action=='publish-eligibility'
 assert not any(hasattr(publish,k) for k in ['issue','labels','description','scope','policy','plan','approve','gate'])

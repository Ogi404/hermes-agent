"""Real eligibility CLI transport wiring with native disposable task state."""
import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli.test_kanban_fleet_contract_guard import fleet_home
from tests.plugins.test_fleet_task_intake import source
from tests.plugins.test_fleet_task_staging import staging
from tests.plugins.test_fleet_held_scope_preparation import held_staging
from tests.plugins.test_fleet_held_eligibility_preparation import held, eligibility
from tests.plugins.test_fleet_held_eligibility_publication import prepared, approve
from plugins.fleet_lifecycle.controller import Halt


@pytest.mark.parametrize('cleanup_failure',[False,True])
def test_native_cli_binds_fixed_readiness_payload_and_never_runs_staging(prepared,monkeypatch,capsys,cleanup_failure):
 import hermes_constants
 from tools import mcp_tool_discovery,mcp_tool_lifecycle
 from plugins.fleet_lifecycle import brief,held_brief
 module=eligibility();policy,row,proposal,runtime,result,detail,operations,read,update,normalize=prepared
 approve(prepared)
 home=Path(policy['installation']['profiles']['director']['home'])
 path=home/'fleet-lifecycle-policy.json';path.write_text(json.dumps(policy));os.chmod(path,0o600)
 monkeypatch.setattr(hermes_constants,'get_hermes_home',lambda:home)
 monkeypatch.setattr(mcp_tool_discovery,'discover_mcp_tools',lambda **kw:[])
 shutdown=[]
 def cleanup():
  shutdown.append(True)
  if cleanup_failure:raise OSError('fixture cleanup failed')
 monkeypatch.setattr(mcp_tool_lifecycle,'shutdown_mcp_servers',cleanup)
 monkeypatch.setattr(held_brief,'normalize_installed_source',lambda h,p,s,b,d:normalize(d))
 rpc=[]
 def call(name,args):
  rpc.append((name,copy.deepcopy(args)))
  if name=='get_issue':
   assert args=={'id':detail['id'],'includeRelations':True,'includeCustomerNeeds':False,'includeReleases':False}
   return read(args['id'])
  assert name=='save_issue' and args=={'id':detail['uuid'],'labels':['agent-ready']}
  return update(args['id'])
 monkeypatch.setattr(brief,'_linear_call',call)
 original=module.publish_eligibility
 def publication(*args,**kwargs):return original(*args,**kwargs,runtime_factory=runtime)
 monkeypatch.setattr(module,'publish_eligibility',publication)
 args=SimpleNamespace(action='publish-eligibility',board='default',task=row['id'])
 if cleanup_failure:
  with pytest.raises(Halt,match='eligibility_transport_cleanup_failed'):module.eligibility_command(args)
  assert not capsys.readouterr().out
 else:
  assert module.eligibility_command(args)==0
  out=json.loads(capsys.readouterr().out);assert out['stage']=='eligible_native_still_blocked' and out['execution_authorized'] is False
 assert shutdown==[True] and sum(n=='save_issue' for n,_ in rpc)==1
 assert not Path(row['workspace_path']).exists()
 with kbc.connect() as conn:
  task=dict(conn.execute('SELECT * FROM tasks WHERE id=?',(row['id'],)).fetchone())
  assert task['status']=='blocked' and task['current_run_id'] is None and not conn.execute('SELECT 1 FROM task_runs').fetchone()

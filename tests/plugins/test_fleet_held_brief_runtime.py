"""Exercise the actual credentialless normalizer child and source-facing CLI."""
import copy
import json
import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from tests.hermes_cli.test_kanban_fleet_contract_guard import fleet_home
from tests.plugins.test_fleet_task_intake import source
from tests.plugins.test_fleet_held_brief import inputs
from tests.plugins.test_fleet_held_brief_projection import captured_source
from plugins.fleet_lifecycle.controller import Halt

PRODUCER='''import json,os
from pathlib import Path
def load_route(slug):
 assert not any(k in os.environ for k in ['ANTHROPIC_API_KEY','ANTHROPIC_AUTH_TOKEN','SLACK_BOT_TOKEN','GITHUB_TOKEN'])
 assert json.loads(MANIFEST_FILE.read_text())=={'fixture':True}
 return json.loads(ROUTES_FILE.read_text())
def normalize_relations(value):return value,[]
def build_issue(issue,route,project):return issue['fixture_contract']
def contract_hash(contract):return contract['contract_sha256']
'''


@pytest.fixture
def normalizer_fixture(captured_source):
 bp,policy,message,calls,read_message,linear,normalize,pid,created=captured_source
 home=Path(os.environ['HERMES_HOME'])/'profiles/director';home.mkdir(parents=True,exist_ok=True)
 policy['installation']['profiles']['director']['home']=str(home)
 # Reproduce observed deployed metadata: private state dir,0755script,0600route,0644manifest.
 state=home/'state';state.mkdir(mode=0o700)
 for parent in (home,*home.parents):
  if parent.stat().st_uid==os.getuid() and not parent.stat().st_mode&0o1000:os.chmod(parent,0o700)
 script=state/'fetch-linear-reconcile-snapshot.py';script.write_text(PRODUCER);os.chmod(script,0o755)
 route=state/'linear-reconcile-routes.json';route.write_text(json.dumps({'hermes_project':'hermes-agent','board':'default','linear_project':'fixture'}));os.chmod(route,0o600)
 manifest=state/'fleet-projects.json';manifest.write_text(json.dumps({'fixture':True}));os.chmod(manifest,0o644)
 raw=copy.deepcopy(created);raw.update(project='fixture',updatedAt='2026-11-19T00:00:00Z',relations={'blockedBy':[],'blocks':[],'relatedTo':[],'duplicateOf':None});raw['fixture_contract']=normalize(created)
 return home,policy,raw


def test_actual_normalization_child_accepts_deployed_modes_without_parent_environment_changes(normalizer_fixture,monkeypatch):
 from plugins.fleet_lifecycle.held_brief import normalize_installed_source
 home,policy,raw=normalizer_fixture
 monkeypatch.setenv('ANTHROPIC_API_KEY','SYNTHETIC_NOT_A_REAL_KEY')
 before=dict(os.environ)
 result=normalize_installed_source(home,policy,'hermes-agent','default',raw)
 assert result['issue_id']==raw['id'] and result['description']==raw['description']
 assert result['source_updated_at'] and result['fetched_at']
 assert dict(os.environ)==before


@pytest.mark.parametrize('case',['missing_timestamp','missing_relations','blocked_relations'])
def test_real_child_rejects_missing_or_blocking_source_metadata(normalizer_fixture,case):
 from plugins.fleet_lifecycle.held_brief import normalize_installed_source
 home,policy,raw=normalizer_fixture
 if case=='missing_timestamp':raw.pop('updatedAt')
 elif case=='missing_relations':raw.pop('relations')
 else:raw['relations']['blockedBy']=[{'id':'OTHER-1','title':'Blocked'}]
 with pytest.raises(Halt):normalize_installed_source(home,policy,'hermes-agent','default',raw)


@pytest.mark.parametrize('board_layout',['default','shared'])
def test_source_cli_runs_actual_capture_and_native_transaction_on_disposable_director_registry(captured_source,normalizer_fixture,monkeypatch,capsys,board_layout):
 import hermes_constants
 import dotenv
 from tools import mcp_tool_discovery,mcp_tool_lifecycle
 from plugins.fleet_lifecycle import held_brief
 bp,policy,message,calls,read_message,linear,normalize,pid,created=captured_source
 home,policy,_raw=normalizer_fixture
 root=home.parent.parent
 policy['installation']['slack']['team']=bp['team'];policy['approval']['actor']=bp['owner']
 for name,value in [('fleet-lifecycle-policy.json',policy),('fleet-brief-policy.json',bp)]:
  p=home/name;p.write_text(json.dumps(value));os.chmod(p,0o600)
 original_registry=root/'profiles/director/projects.db'
 # The source fixture may choose another Director home; mirror only the genuine fixture registry.
 from hermes_cli.kanban_fleet_project_registry import fleet_project_record
 record=fleet_project_record(pid);assert record is not None
 fixture_root=Path(os.environ['HERMES_HOME'])
 source_registry=fixture_root/'profiles/director/projects.db'
 if source_registry!=original_registry:
  with sqlite3.connect(source_registry) as src,sqlite3.connect(original_registry) as dst:src.backup(dst)
 os.chmod(original_registry,0o644)
 database=kb.kanban_db_path(board='default')
 assert database==root/'kanban.db' and database.exists()
 if board_layout=='shared':
  destination=root/'kanban/boards/fixture-held/kanban.db';destination.parent.mkdir(parents=True)
  with sqlite3.connect(database) as src,sqlite3.connect(destination) as dst:src.backup(dst)
  os.chmod(root/'kanban',0o775);os.chmod(root/'kanban/boards',0o775);os.chmod(destination.parent,0o700)
  database=destination
  policy['projects'][0]['board']='fixture-held'
  (home/'fleet-lifecycle-policy.json').write_text(json.dumps(policy))
  assert kb.kanban_db_path(board='fixture-held')==database
 os.chmod(database,0o600)
 monkeypatch.setattr(hermes_constants,'get_hermes_home',lambda:home)
 monkeypatch.setattr(hermes_constants,'get_default_hermes_root',lambda **kw:root)
 monkeypatch.setattr(dotenv,'dotenv_values',lambda p:{'SLACK_BOT_TOKEN':'fixture-only'})
 class Slack:
  def auth_test(self):return {'team_id':bp['team']}
  def conversations_history(self,**kw):
   assert kw['oldest']==kw['latest']==message['ts'] and kw['inclusive'] is True
   return {'messages':[message]}
 monkeypatch.setitem(sys.modules,'slack_sdk',SimpleNamespace(WebClient=lambda **kw:Slack()))
 monkeypatch.setattr(mcp_tool_discovery,'discover_mcp_tools',lambda **kw:[])
 monkeypatch.setattr(mcp_tool_lifecycle,'shutdown_mcp_servers',lambda:None)
 monkeypatch.setattr(held_brief.brief,'_linear_call',linear)
 monkeypatch.setattr(held_brief,'normalize_installed_source',lambda h,p,s,b,i:normalize(i))
 assert held_brief.project_command(SimpleNamespace(project='hermes-agent',message_ts=message['ts']))==0
 result=json.loads(capsys.readouterr().out);assert result['stage']=='held_native_projected'
 with sqlite3.connect(database) as conn:
  row=conn.execute('SELECT status,current_run_id,worker_pid FROM tasks WHERE id=?',(result['task_id'],)).fetchone()
  assert row==('blocked',None,None)
 assert [n for n,_ in calls].count('save_issue')==1


@pytest.mark.parametrize('case',['valid','outside_root','symlink','writable_root','readable_database','writable_database'])
def test_private_root_native_database_boundary(normalizer_fixture,monkeypatch,case):
 from plugins.fleet_lifecycle import held_brief
 home,policy,raw=normalizer_fixture;root=home.parent.parent
 database=root/'kanban/boards/fixture-boundary/kanban.db';database.parent.mkdir(parents=True)
 database.write_bytes(b'fixture-only; path validation does not open sqlite')
 for path in [root/'kanban',root/'kanban/boards']:os.chmod(path,0o775)
 os.chmod(root,0o700);os.chmod(database.parent,0o700);os.chmod(database,0o600)
 if case=='outside_root':
  other=root.parent/'outside-board.db';other.write_bytes(b'foreign');os.chmod(other,0o600);database=other
 elif case=='symlink':
  target=database.with_name('real.db');database.rename(target);database.symlink_to(target)
 elif case=='writable_root':os.chmod(root,0o775)
 elif case=='readable_database':os.chmod(database,0o644)
 elif case=='writable_database':os.chmod(database,0o660)
 if case=='outside_root':monkeypatch.setattr(kb,'kanban_db_path',lambda **kw:database)
 fn=getattr(held_brief,'private_board_database',None)
 assert callable(fn),'missing native database private-root boundary'
 before=database.lstat()
 if case=='valid':assert fn(root,'fixture-boundary')==database
 else:
  with pytest.raises(Halt):fn(root,'fixture-boundary')
 after=database.lstat();assert (before.st_mode,before.st_ino,before.st_uid,before.st_gid)==(after.st_mode,after.st_ino,after.st_uid,after.st_gid)

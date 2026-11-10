"""Workspace and plan staging: native board + real disposable Git, no inference."""
import argparse
import copy
import importlib.util
import json
import os
import shutil
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_human_gate as gates
from plugins.fleet_lifecycle.controller import Halt, Plan, setup_cli
from plugins.fleet_lifecycle.runtime import Git
from tests.hermes_cli.test_kanban_fleet_contract_guard import fleet_home
from tests.plugins.test_fleet_task_intake import source


def stage_module():
    from plugins.fleet_lifecycle import intake
    assert callable(getattr(intake,'stage_task',None)), 'no automatic workspace/plan staging owner'
    return intake


@pytest.fixture
def staging(source, tmp_path):
    policy,row,proposal,_gate=source
    env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null',
         'GIT_CONFIG_COUNT':'0','GIT_TERMINAL_PROMPT':'0','GIT_OPTIONAL_LOCKS':'0','GIT_NO_LAZY_FETCH':'1',
         'GIT_NO_REPLACE_OBJECTS':'1','GIT_ATTR_NOSYSTEM':'1'}
    binary=shutil.which('git');assert binary
    root=Path(policy['projects'][0]['repository']);root.mkdir()
    os.chmod(root.parent, 0o700)
    git=Git(binary,root,env)
    git.call('init','--initial-branch=main')
    git.call('config','user.name','Fixture')
    git.call('config','user.email','fixture@example.invalid')
    (root/'example.py').write_bytes(b'VALUE = 1\n')
    git.call('add','--','example.py');git.call('commit','-m','Disposable staging base')
    policy['projects'][0]['base']=git.call('rev-parse','HEAD').decode().strip()
    os.chmod(Path(policy['installation']['profiles']['director']['home']).parent, 0o700)
    os.chmod(Path(policy['installation']['profiles']['director']['home']).parent.parent, 0o700)
    for profile in policy['installation']['profiles'].values():
        Path(profile['home']).mkdir(parents=True,exist_ok=True,mode=0o700)
    stage=Path(policy['installation']['profiles']['director']['home'])/'fleet-task-staging'
    stage.mkdir(parents=True,exist_ok=True,mode=0o700)
    _chain=stage
    while _chain != Path('/'):
        try:
            if _chain.stat().st_uid == os.getuid(): os.chmod(_chain, 0o700)
        except OSError: pass
        _chain = _chain.parent
    calls=[]
    class RuntimeFixture:
        def __init__(self,plan):
            self.p=plan.data;self.git_binary=binary;self.env=env
            self.git=Git(binary,plan.data['workspace'],env)
        def principal(self):calls.append('principal')
        def installation_identity(self):calls.append('installation_identity')
        def identity(self):
            calls.append('identity')
            self.git.guard(self.p['branch'],self.p['base'])
        # The actual Runtime requires live identity pins after source staging;
        # this fixture is an explicit boundary double, not a proxy for that policy.
        def prepare_exact(self,template,proposal):
            calls.append('prepare_exact')
            from plugins.fleet_lifecycle.planning import prepare_plan
            return prepare_plan(template,proposal,self.git)
    return policy,row,proposal,git,RuntimeFixture,calls


def test_stage_materialises_exact_worktree_and_complete_plan_without_dispatch_or_gate_approval(staging):
    policy,row,proposal,git,runtime,calls=staging
    parent_mask=os.umask(0o077)
    try:
        result=stage_module().stage_task(policy,row['id'],proposal,board='default',runtime_factory=runtime)
        observed=os.umask(0o077)
        assert observed==0o077,'staging must not mutate a shared parent umask'
    finally:os.umask(parent_mask)
    assert result['stage']=='awaiting_scope_review'
    plan=Plan.load(Path(result['plan_path']))
    assert plan.digest==result['plan_hash'] and plan.data['task_id']==row['id']
    assert plan.data['changes'][0]['base_sha256'] is not None
    assert (Path(plan.data['workspace'])/'example.py').read_bytes()==b'VALUE = 1\n'
    assert (Path(plan.data['workspace'])/'example.py').stat().st_mode&0o777==0o644
    assert Path(result['plan_path']).stat().st_mode&0o777==0o600
    assert git.call('rev-parse','HEAD').decode().strip()==policy['projects'][0]['base']
    with kbc.connect() as c:
        task=kb.get_task(c,row['id']);gate=kb.get_task(c,result['gate_task_id'])
        assert task.status=='blocked' and task.current_run_id is None and task.worker_pid is None
        assert gate.status=='blocked' and gate.workspace_kind=='scratch'
        assert gates.latest_gate(c,gate.id,'push') is None,'unreviewed plan cannot forge APPROVE/request gate'
        assert not c.execute('SELECT 1 FROM task_runs').fetchone()
    assert calls[0]=='principal' and 'installation_identity' in calls and 'identity' in calls
    original=Path(result['plan_path']).read_bytes()
    again=stage_module().stage_task(policy,row['id'],proposal,board='default',runtime_factory=runtime)
    assert again==result and Path(result['plan_path']).read_bytes()==original
    with kbc.connect() as c:
        assert c.execute("SELECT count(*) FROM tasks WHERE created_by='trusted-controller'").fetchone()[0]==1


@pytest.mark.parametrize('case',['occupied','existing_branch','drifted_base','bad_scope'])
def test_stage_preflight_refuses_before_gate_or_worktree_side_effects(staging,case):
    policy,row,proposal,git,runtime,_calls=staging
    target=Path(row['workspace_path'])
    if case=='occupied':target.mkdir(parents=True)
    elif case=='existing_branch':git.call('branch',row['branch_name'])
    elif case=='drifted_base':policy['projects'][0]['base']='0'*40
    else:proposal['candidate_paths']=['../outside.py']
    with kbc.connect() as c:before=c.execute('SELECT count(*) FROM tasks').fetchone()[0]
    with pytest.raises(Halt):stage_module().stage_task(policy,row['id'],proposal,board='default',runtime_factory=runtime)
    with kbc.connect() as c:
        assert c.execute('SELECT count(*) FROM tasks').fetchone()[0]==before
        assert not c.execute('SELECT 1 FROM task_runs').fetchone()
    if case!='occupied':assert not target.exists()


def test_stage_failure_is_preserved_and_not_replayed(staging,monkeypatch):
    policy,row,proposal,git,runtime,_calls=staging
    module=stage_module();real=Git.call;count=[]
    def fail_create(self,*args,**kwargs):
        if args[:2]==('worktree','add'):
            count.append(True);raise Halt('fixture_worktree_failure')
        return real(self,*args,**kwargs)
    monkeypatch.setattr(Git,'call',fail_create)
    for _ in range(2):
        with pytest.raises(Halt):module.stage_task(policy,row['id'],proposal,board='default',runtime_factory=runtime)
    assert len(count)==1,'uncertain staging must not retry worktree creation'
    with kbc.connect() as c:
        assert not c.execute('SELECT 1 FROM task_runs').fetchone()
        assert kb.get_task(c,row['id']).status=='blocked'


def test_stage_task_cli_needs_no_complete_plan_template(tmp_path):
    parser=argparse.ArgumentParser();setup_cli(parser)
    choices=next(a for a in parser._actions if isinstance(a,argparse._SubParsersAction)).choices
    assert 'stage-task' in choices,'normal task still requires a hand-built template'
    args=parser.parse_args(['stage-task','--board','default','--task','t_12345678',
                           '--proposal',str(tmp_path/'scope.json')])
    assert args.task=='t_12345678' and not hasattr(args,'plan')

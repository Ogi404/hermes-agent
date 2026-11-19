"""Current source selection through real disposable staging and a read-only GH boundary."""
import copy
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
from hermes_cli import kanban_db_connect as kbc
from plugins.fleet_lifecycle.controller import Halt, Plan
from plugins.fleet_lifecycle import intake
from tests.hermes_cli.test_kanban_fleet_contract_guard import fleet_home
from tests.plugins.test_fleet_task_intake import source
from tests.plugins.test_fleet_task_staging import staging


@pytest.fixture
def moving_source(staging,tmp_path,monkeypatch):
    policy,row,proposal,git,runtime,calls = staging
    old = git.call('rev-parse','HEAD').decode().strip()
    git.call('checkout','-b','upstream-fixture')
    (git.workspace/'example.py').write_text('VALUE = 2\n')
    git.call('add','example.py');git.call('commit','-m','New upstream fixture')
    new = git.call('rev-parse','HEAD').decode().strip()
    git.call('checkout','main')
    git.call('remote','add','origin','https://github.com/fixture-owner/fixture-repo.git')
    policy['projects'][0]['source_repository'] = 'fixture-owner/fixture-repo'
    metadata = tmp_path/'remote-head.json';metadata.write_text(json.dumps({'ref':'refs/heads/main','object':{'type':'commit','sha':new}}))
    log = tmp_path/'gh-calls.jsonl'
    gh = tmp_path/'readonly-gh'
    gh.write_text('#!'+sys.executable+'\nimport json,sys\nfrom pathlib import Path\np=Path('+repr(str(log))+')\nwith p.open("a") as f:f.write(json.dumps(sys.argv[1:])+"\\n")\nprint(Path('+repr(str(metadata))+').read_text())\n')
    gh.chmod(0o755)
    from hermes_platform import resolver
    monkeypatch.setattr(resolver,'locate_command',lambda name: SimpleNamespace(found=True,command=[str(gh)]) if name=='gh' else None)
    from plugins.fleet_lifecycle import source_selection
    from plugins.fleet_lifecycle import runtime as runtime_module
    native_run = runtime_module.run
    # /var/tmp is intentionally noexec in the canonical container. Run only
    # this disposable fake through the already-mounted managed interpreter.
    def fake_cli_transport(argv, **kwargs):
        assert argv[0] == str(gh)
        return native_run([sys.executable, *argv], **kwargs)
    monkeypatch.setattr(source_selection, "run", fake_cli_transport)
    return policy,row,proposal,git,runtime,old,new,metadata,log


def test_new_task_freezes_verified_current_source_without_moving_existing_checkout(moving_source):
    policy,row,proposal,git,runtime,old,new,metadata,log = moving_source
    before = copy.deepcopy((policy,row,proposal))
    result = intake.stage_task(policy,row['id'],proposal,board='default',runtime_factory=runtime)
    plan = Plan.load(Path(result['plan_path']))
    assert plan.data['base'] == new
    assert (Path(plan.data['workspace'])/'example.py').read_text() == 'VALUE = 2\n'
    assert plan.data['changes'][0]['base_sha256'] == hashlib.sha256(b'VALUE = 2\n').hexdigest()
    assert git.call('rev-parse','HEAD').decode().strip() == old
    assert (git.workspace/'example.py').read_text() == 'VALUE = 1\n'
    assert (policy,row,proposal) == before
    requests=[json.loads(x) for x in log.read_text().splitlines()]
    assert len(requests)==2
    assert all(x==['api','--method','GET','repos/fixture-owner/fixture-repo/git/ref/heads/main'] for x in requests)
    metadata.write_text(json.dumps({'ref':'refs/heads/main','object':{'type':'commit','sha':'f'*40}}))
    assert intake.stage_task(policy,row['id'],proposal,board='default',runtime_factory=runtime)==result
    assert len(log.read_text().splitlines()) == 2, 'idempotent existing plans must stay frozen, not refresh'
    with kbc.connect() as conn:
        task=conn.execute('SELECT * FROM tasks WHERE id=?',(row['id'],)).fetchone()
        assert task['status']=='blocked' and task['worker_pid'] is None
        assert not conn.execute('SELECT 1 FROM task_runs').fetchone()


@pytest.mark.parametrize('case',['wrong_remote','wrong_ref','not_commit','invalid_sha'])
def test_invalid_source_selection_stops_before_scope_gate_or_worktree(moving_source,case):
    policy,row,proposal,git,runtime,old,new,metadata,log = moving_source
    if case=='wrong_remote':git.call('remote','set-url','origin','https://github.com/other/repository.git')
    else:
        m=json.loads(metadata.read_text())
        if case=='wrong_ref':m['ref']='refs/heads/elsewhere'
        elif case=='not_commit':m['object']['type']='tag'
        else:m['object']['sha']='invalid'
        metadata.write_text(json.dumps(m))
    with kbc.connect() as conn:before=tuple(tuple(x) for x in conn.execute('SELECT * FROM task_events ORDER BY id'))
    with pytest.raises(Halt,match='source_'):
        intake.stage_task(policy,row['id'],proposal,board='default',runtime_factory=runtime)
    assert not Path(row['workspace_path']).exists()
    with kbc.connect() as conn:
        assert tuple(tuple(x) for x in conn.execute('SELECT * FROM task_events ORDER BY id'))==before
        assert not conn.execute('SELECT 1 FROM task_runs').fetchone()
    assert git.call('rev-parse','HEAD').decode().strip()==old


def test_missing_upstream_object_is_fetched_without_moving_refs_or_checkout(moving_source,tmp_path,monkeypatch):
    policy,row,proposal,git,runtime,old,new,metadata,log = moving_source
    bare=tmp_path/'transport-fixture.git'
    git.call('-c','protocol.file.allow=always','clone','--bare','--no-hardlinks',str(git.workspace),str(bare))
    objects=Path(git.call('rev-parse','--git-path','objects').decode().strip())
    if not objects.is_absolute(): objects=git.workspace/objects
    (objects/new[:2]/new[2:]).unlink()
    from plugins.fleet_lifecycle.runtime import Git
    original=Git.call;fetches=[]
    def boundary(self,*args,**kwargs):
        if 'fetch' in args:
            fetches.append(args)
            assert args[-2:]==('https://github.com/fixture-owner/fixture-repo.git',new)
            assert '--no-write-fetch-head' in args and '--no-tags' in args
            assert '--no-recurse-submodules' in args
            return original(self,'-c','protocol.file.allow=always','fetch','--no-tags',
                            '--no-write-fetch-head','--no-recurse-submodules',str(bare),new)
        return original(self,*args,**kwargs)
    monkeypatch.setattr(Git,'call',boundary)
    result=intake.stage_task(policy,row['id'],proposal,board='default',runtime_factory=runtime)
    assert Plan.load(Path(result['plan_path'])).data['base']==new
    assert len(fetches)==1
    assert git.call('rev-parse','HEAD').decode().strip()==old
    assert not (git.workspace/'.git/FETCH_HEAD').exists()


@pytest.mark.parametrize('value',[None, [], True])
def test_non_mapping_project_policy_fails_typed_before_work(staging,value):
    policy,row,proposal,git,runtime,calls=staging
    policy['projects'][0]=value
    with pytest.raises(Halt):intake.project_policy(policy,'hermes-agent',board='default')


@pytest.mark.parametrize('value',[None, [], True])
def test_non_mapping_scope_fails_typed_before_work(staging,value):
    policy,row,proposal,git,runtime,calls=staging
    with pytest.raises(Halt):intake.task_plan(policy,row,value,'t_deadbeef',board='default')


def test_source_reader_spawn_failure_is_typed_before_native_effects(moving_source,monkeypatch):
    policy,row,proposal,git,runtime,old,new,metadata,log=moving_source
    from plugins.fleet_lifecycle import source_selection
    def unavailable(*args,**kwargs):raise OSError('reader unavailable')
    monkeypatch.setattr(source_selection,'run',unavailable)
    with pytest.raises(Halt,match='source_reader_unavailable'):
        intake.stage_task(policy,row['id'],proposal,board='default',runtime_factory=runtime)
    assert not Path(row['workspace_path']).exists()

"""Task data reaches the real planner; source/baseline remain immutable and gated."""
import copy
import hashlib
import json
from pathlib import Path
import pytest
from hermes_cli import kanban_db_connect as kbc
from plugins.fleet_lifecycle import intake
from plugins.fleet_lifecycle.controller import Halt, Plan
from tests.hermes_cli.test_kanban_fleet_contract_guard import fleet_home
from tests.plugins.test_fleet_task_intake import source
from tests.plugins.test_fleet_task_staging import staging
from tests.plugins.test_fleet_source_selection import moving_source
from tests.plugins.test_fleet_project_verification import _runtime
from tests.plugins.test_fleet_project_visual_handoff import preview
from tests.plugins.test_fleet_task_visual_expectations import grey_observations


@pytest.fixture
def task_preview(moving_source,tmp_path,monkeypatch):
    policy,row,proposal,git,runtime,old,new,metadata,log=moving_source
    rt,data,_calls=_runtime(tmp_path,monkeypatch)
    spec=preview(rt,data,tmp_path)
    policy['installation']['resources']=copy.deepcopy(data['resources'])
    policy['projects'][0]['verification']=copy.deepcopy(data['verification'])
    git.call('checkout','upstream-fixture')
    path=git.workspace/'src/app/page.tsx';path.parent.mkdir(parents=True)
    path.write_bytes(b'CURRENT_SOURCE\n')
    git.call('add','src/app/page.tsx');git.call('commit','-m','Current navigation fixture')
    head=git.call('rev-parse','HEAD').decode().strip();git.call('checkout','main')
    metadata.write_text(json.dumps({'ref':'refs/heads/main','object':{'type':'commit','sha':head}}))
    proposal.update(candidate_paths=['src/app/page.tsx'],test_files=[],
                    context=[{'path':'src/app/page.tsx','functions':[]}])
    baseline,_candidate,expected=grey_observations()
    baseline['sourceSha256']=hashlib.sha256(b'CURRENT_SOURCE\n').hexdigest()
    reference=Path(policy['installation']['state_root'])/'reference';reference.mkdir(parents=True)
    record=reference/'preview-result.json';record.write_text(json.dumps(baseline))
    proposal['preview']={'baseline':str(record),'baseline_sha256':hashlib.sha256(record.read_bytes()).hexdigest(),
                         'expected_background_rgb':expected}
    return policy,row,proposal,git,runtime,old,head,record


def test_task_preview_freezes_current_base_reference_and_theme_states_without_changing_policy(task_preview):
    policy,row,proposal,git,runtime,old,head,record=task_preview
    before=copy.deepcopy((policy,proposal))
    result=intake.stage_task(policy,row['id'],proposal,board='default',runtime_factory=runtime)
    plan=Plan.load(Path(result['plan_path']))
    assert plan.data['base']==head
    expected=plan.data['verification']['preview']
    assert all(expected[k]==v for k,v in proposal['preview'].items())
    assert expected['runner']==policy['projects'][0]['verification']['preview']['runner']
    assert expected['image']==policy['projects'][0]['verification']['preview']['image']
    assert (policy,proposal)==before
    assert git.call('rev-parse','HEAD').decode().strip()==old
    assert intake.stage_task(policy,row['id'],proposal,board='default',runtime_factory=runtime)==result
    with kbc.connect() as conn:
        task=conn.execute('SELECT * FROM tasks WHERE id=?',(row['id'],)).fetchone()
        assert task['status']=='blocked' and task['worker_pid'] is None
        assert not conn.execute('SELECT 1 FROM task_runs').fetchone()


@pytest.mark.parametrize('case',['wrong_source','wrong_digest','missing_state','runner_override','outside_state_root'])
def test_task_reference_rejected_before_scope_gate_or_worktree(task_preview,case,tmp_path):
    policy,row,proposal,git,runtime,old,head,record=task_preview
    if case in ['wrong_source','missing_state']:
        value=json.loads(record.read_text())
        if case=='wrong_source':value['sourceSha256']='0'*64
        else:value['observations'].pop()
        record.write_text(json.dumps(value));proposal['preview']['baseline_sha256']=hashlib.sha256(record.read_bytes()).hexdigest()
    elif case=='wrong_digest':proposal['preview']['baseline_sha256']='0'*64
    elif case=='runner_override':proposal['preview']['runner']='/arbitrary/executable'
    else:
        outside=tmp_path/'untrusted-preview.json';outside.write_bytes(record.read_bytes())
        proposal['preview']['baseline']=str(outside)
    with kbc.connect() as conn:before=tuple(tuple(x) for x in conn.execute('SELECT * FROM task_events ORDER BY id'))
    with pytest.raises(Halt):intake.stage_task(policy,row['id'],proposal,board='default',runtime_factory=runtime)
    assert not Path(row['workspace_path']).exists()
    with kbc.connect() as conn:
        assert tuple(tuple(x) for x in conn.execute('SELECT * FROM task_events ORDER BY id'))==before
        assert not conn.execute('SELECT 1 FROM task_runs').fetchone()


def test_current_source_mode_rejects_stale_default_reference_without_task_override(task_preview):
    policy,row,proposal,git,runtime,old,head,record=task_preview
    del proposal['preview']
    with kbc.connect() as conn:before=tuple(tuple(x) for x in conn.execute('SELECT * FROM task_events ORDER BY id'))
    with pytest.raises(Halt,match='preview_'):
        intake.stage_task(policy,row['id'],proposal,board='default',runtime_factory=runtime)
    assert not Path(row['workspace_path']).exists()
    with kbc.connect() as conn:
        assert tuple(tuple(x) for x in conn.execute('SELECT * FROM task_events ORDER BY id'))==before


def test_preview_data_cannot_upgrade_a_python_lane(source):
    policy,row,proposal,gate=source
    proposal['preview']={'baseline':str(Path(policy['installation']['state_root'])/'not-created.json'),
                         'baseline_sha256':'0'*64,'expected_background_rgb':[1,2,3]}
    with pytest.raises(Halt):intake.task_plan(policy,row,proposal,gate,board='default')

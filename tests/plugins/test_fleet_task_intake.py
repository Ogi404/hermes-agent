"""Automatic task-to-plan intake; real native fixtures, no live fleet actions."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_notify as notify
from plugins.fleet_lifecycle.controller import Halt, Plan, canonical, digest
from tests.hermes_cli.test_kanban_fleet_contract_guard import fleet_home, _role_contract, _create_fleet_task
from tests.plugins.test_fleet_lifecycle_controller import plan_data

BRIEF = 'Keep instance configuration independent.\nAcceptance: nested values do not leak.\nNon-goals: deployment.'


def intake():
    assert importlib.util.find_spec('plugins.fleet_lifecycle.intake') is not None, 'no native task-to-plan factory'
    from plugins.fleet_lifecycle import intake as module
    return module


@pytest.fixture
def source(fleet_home, tmp_path):
    home, pid = fleet_home
    base = plan_data(tmp_path)
    root = str(tmp_path / 'repo')
    contract = _role_contract(execution_mode='implement', description=BRIEF)
    with kbc.connect() as c:
        tid = _create_fleet_task(c, pid, contract, initial_status='blocked')
        gate = kb.create_task(c, title='Brief approval', assignee='director', initial_status='blocked')
        notify.add_notify_sub(c, task_id=tid, platform='slack', chat_id='CFIXTURE', notifier_profile='default',
                             delivery_mode='notify', delivery_metadata={'slack_team_id':'TFIXTURE'})
        row = dict(c.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
    fixed = {k:copy.deepcopy(base[k]) for k in ['core','source_sha','resources','profiles','native','state_root','slack','commit']}
    policy = {'schema':1, 'installation':fixed, 'approval':{'actor':'UOWNER','stage':'push'},
              'projects':[{'slug':'hermes-agent','repository':root,'board':'default',
                           'base':'a'*40,'source_branch':'main','verification':None}]}
    scope = {'candidate_paths':['example.py','tests/test_example.py'], 'test_files':['tests/test_example.py'],
             'context':[{'path':'example.py','functions':[]}]}
    return policy, row, scope, gate


def test_factory_derives_native_identity_and_brief_without_a_per_task_template(source):
    policy, row, scope, gate = source
    old = copy.deepcopy((policy,row,scope))
    result = intake().task_plan(policy, row, scope, gate, board='default')
    Plan.validate(result.data)
    assert result.data['task_id']==row['id'] and result.data['project_id']==row['project_id']
    assert result.data['workspace']==row['workspace_path'] and result.data['branch']==row['branch_name']
    assert result.data['brief_sha256']==digest(BRIEF.encode())
    assert result.data['gate']=={'task_id':gate,'stage':'push','actor':'UOWNER','source_sha':policy['installation']['source_sha']}
    assert result.digest==digest(canonical(result.data))
    assert result.data['database']==str(kb.kanban_db_path(board='default'))
    assert (policy,row,scope)==old
    with kbc.connect() as c:
        assert not c.execute('SELECT 1 FROM task_runs').fetchone()
        assert kb.get_task(c,row['id']).status=='blocked'


@pytest.mark.parametrize('case',['unknown_project','wrong_root','wrong_board','duplicate_project','scope_override',
                                  'not_held','claimed','workflow_exists','bad_gate','bad_policy','unready_source'])
def test_factory_rejects_scope_identity_or_ownership_drift_without_native_mutation(source, case):
    policy,row,scope,gate=source
    if case=='unknown_project': policy['projects'][0]['slug']='elsewhere'
    elif case=='wrong_root': policy['projects'][0]['repository']+='/different'
    elif case=='wrong_board': policy['projects'][0]['board']='elsewhere'
    elif case=='duplicate_project': policy['projects'].append(copy.deepcopy(policy['projects'][0]))
    elif case=='scope_override': scope['native']={'provider':'unapproved'}
    elif case=='not_held': row['status']='ready'
    elif case=='claimed': row['claim_lock']='foreign'
    elif case=='workflow_exists': row['workflow_template_id']='already-owned'
    elif case=='bad_gate': gate=row['id']
    elif case=='bad_policy': policy['unexpected']=True
    else:
        import re
        old_body=row['body']
        row['body']=re.sub(r'("agent_ready"\s*:\s*)true', r'\1false', old_body, count=1)
        assert row['body']!=old_body
    with kbc.connect() as c:
        before=tuple(tuple(r) for r in c.execute('SELECT * FROM task_events ORDER BY id'))
    with pytest.raises(Halt):intake().task_plan(policy,row,scope,gate,board='default')
    with kbc.connect() as c:
        assert tuple(tuple(r) for r in c.execute('SELECT * FROM task_events ORDER BY id'))==before
        assert not c.execute('SELECT 1 FROM task_runs').fetchone()

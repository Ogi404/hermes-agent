"""Trusted reusable preparation, using disposable Git and explicit native doubles."""
import argparse
import copy
import importlib.util
import json
from types import SimpleNamespace

import pytest

from plugins.fleet_lifecycle.controller import Halt, Native, Plan, canonical, digest, setup_cli, command
from tests.plugins.test_fleet_ordinary_brief import brief_plan
from tests.plugins.test_fleet_verification_packet import blob_repo


def planner():
    name = 'plugins.fleet_lifecycle.planning'
    assert importlib.util.find_spec(name) is not None, 'normal intake has no reusable trusted plan preparer'
    from plugins.fleet_lifecycle.planning import prepare_plan
    return prepare_plan


def proposal():
    return {'candidate_paths': ['example.py', 'tests/test_example.py'],
            'test_files': ['tests/test_example.py'],
            'context': [{'path': 'example.py', 'functions': []}]}


def configured_plan(git):
    (git.workspace / 'example.py').write_bytes(b'VALUE = 1\n')
    git.call('add', '--', 'example.py')
    git.call('commit', '-m', 'Fixture preparation base')
    data = brief_plan(git.workspace.parent)
    data.update(base=git.call('rev-parse', 'HEAD').decode().strip(), branch='fixture', workspace=str(git.workspace))
    return Plan(data, digest(canonical(data)))


def test_preparer_compiles_exact_base_preimages_without_editing_or_authorising(blob_repo):
    git, _ = blob_repo
    template = configured_plan(git)
    scope = proposal()
    before = copy.deepcopy(template.data), copy.deepcopy(scope), git.call('status', '--porcelain')
    result = planner()(template, scope, git)
    Plan.validate(result.data)
    assert result.data['changes'] == [
        {'path': 'example.py', 'base_sha256': digest(b'VALUE = 1\n')},
        {'path': 'tests/test_example.py', 'base_sha256': None},
    ]
    for key in set(template.data) - {'candidate_paths', 'test_files', 'context', 'changes'}:
        assert result.data[key] == template.data[key], key
    assert result.digest == digest(canonical(result.data))
    assert (template.data, scope, git.call('status', '--porcelain')) == before
    assert not (git.workspace / 'tests/test_example.py').exists()
    assert not (git.workspace.parent / 'director/fleet-lifecycle').exists()


@pytest.mark.parametrize('case', ['extra', 'traversal', 'duplicate', 'no_tests', 'wrong_type', 'platform_override'])
def test_preparer_rejects_bad_proposal_before_git_or_native_work(tmp_path, case):
    data = brief_plan(tmp_path)
    template = Plan(data, digest(canonical(data)))
    scope = proposal()
    if case == 'extra':
        scope['command'] = 'do not run'
    elif case == 'traversal':
        scope['candidate_paths'][0] = '../outside.py'
    elif case == 'duplicate':
        scope['candidate_paths'].append('example.py')
    elif case == 'no_tests':
        scope['test_files'] = []
    elif case == 'wrong_type':
        scope['candidate_paths'] = 'example.py'
    else:
        scope['native'] = {'provider': 'unapproved'}

    class NoGit:
        def __getattr__(self, name):
            raise AssertionError('invalid proposal must not reach Git: ' + name)

    with pytest.raises(Halt):
        planner()(template, scope, NoGit())


@pytest.mark.parametrize('case', ['dirty', 'staged', 'wrong_base', 'missing_test', 'missing_brief_binding'])
def test_preparer_refuses_unsafe_base_or_incomplete_scope(blob_repo, case):
    git, _ = blob_repo
    template = configured_plan(git)
    scope = proposal()
    if case == 'dirty':
        (git.workspace / 'example.py').write_bytes(b'VALUE = 2\n')
    elif case == 'staged':
        (git.workspace / 'example.py').write_bytes(b'VALUE = 2\n')
        git.call('add', '--', 'example.py')
    elif case == 'wrong_base':
        template.data['base'] = '0' * 40
    elif case == 'missing_test':
        scope['test_files'] = ['tests/test_absent.py']
    else:
        del template.data['brief_sha256']
    template = Plan(template.data, digest(canonical(template.data)))
    before = git.call('status', '--porcelain')
    with pytest.raises(Halt):
        planner()(template, scope, git)
    assert git.call('status', '--porcelain') == before


def test_prepare_cli_is_registered_alongside_existing_hash_action(tmp_path):
    parser = argparse.ArgumentParser()
    setup_cli(parser)
    assert parser.parse_args(['hash', '--plan', str(tmp_path / 'template.json')]).action == 'hash'
    actions = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction)).choices
    assert 'prepare' in actions, 'existing lifecycle CLI cannot consume ordinary scope proposals'
    args = parser.parse_args(['prepare', '--plan', str(tmp_path / 'template.json'),
                              '--proposal', str(tmp_path / 'proposal.json')])
    assert args.proposal == tmp_path / 'proposal.json'


def test_prepare_cli_uses_readonly_native_checks_and_never_admits(blob_repo, tmp_path, monkeypatch, capsys):
    from contextlib import contextmanager
    from plugins.fleet_lifecycle import controller, runtime
    git, _ = blob_repo
    template = configured_plan(git)
    plan_path = tmp_path / 'template.json'
    plan_path.write_bytes(canonical(template.data))
    proposal_path = tmp_path / 'proposal.json'
    proposal_path.write_text(json.dumps(proposal()))
    calls = []

    class ReadonlyNative:
        def __init__(self, plan):
            self.plan = plan

        @contextmanager
        def connect(self, writable=False):
            assert writable is False
            calls.append('connect_readonly')
            yield object()

        def task(self, conn):
            calls.append('validate_native_task')

        approval_tests = staticmethod(Native.approval_tests)

    monkeypatch.setattr(controller, 'Native', ReadonlyNative)
    class ReadonlyRuntime(runtime.Runtime):
        def __init__(self, plan):
            self.git = git

        def identity(self):
            calls.append('identity')

    monkeypatch.setattr(runtime, 'Runtime', ReadonlyRuntime)
    args = SimpleNamespace(action='prepare', plan=plan_path, proposal=proposal_path)
    assert command(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['stage'] == 'prepared_not_admitted'
    Plan.validate(output['plan'])
    assert output['plan_hash'] == digest(canonical(output['plan']))
    assert output['approval_tests'] == Native.approval_tests(Plan(output['plan'], output['plan_hash']))
    assert calls == ['identity', 'connect_readonly', 'validate_native_task',
                     'identity', 'connect_readonly', 'validate_native_task']
    assert not git.call('status', '--porcelain')
    assert not (git.workspace.parent / 'director/fleet-lifecycle').exists()

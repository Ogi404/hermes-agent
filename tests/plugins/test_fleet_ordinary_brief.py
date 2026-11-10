"""Human briefs stay frozen; only separately approved implementation plans may bind them.

Real disposable native board/registry records; no live services or inference.
"""
import copy
import json
from uuid import uuid4

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as notify
from plugins.fleet_lifecycle.controller import Halt, Native, Plan, canonical, digest
from tests.hermes_cli.test_kanban_fleet_contract_guard import (
    fleet_home, _role_contract, _create_fleet_task,
)
from tests.plugins.test_fleet_implementation import implementation_plan
from tests.plugins.test_fleet_lifecycle_controller import plan_data

BRIEF = "Keep configuration instances independent.\n\nAcceptance: nested defaults must not leak.\nNon-goals: dependencies and deployment."


def brief_plan(tmp_path):
    data = implementation_plan(tmp_path)
    data['brief_sha256'] = digest(BRIEF.encode('utf-8'))
    return data


def test_implementation_plan_accepts_explicit_human_brief_binding_without_rewriting(tmp_path):
    data = brief_plan(tmp_path)
    original = copy.deepcopy(data)
    Plan.validate(data)
    assert data == original
    assert Native.approval_tests(Plan(data, digest(canonical(data)))) != Native.approval_tests(
        Plan({**data, 'brief_sha256': '0' * 64}, digest(canonical({**data, 'brief_sha256': '0' * 64})))
    )


@pytest.mark.parametrize('value', [None, '', 'bad', 'a' * 63, 'a' * 65, 'A' * 64, True, 42, {}])
def test_brief_binding_rejects_malformed_hash(tmp_path, value):
    data = brief_plan(tmp_path)
    data['brief_sha256'] = value
    with pytest.raises(Halt):
        Plan.validate(data)


def test_test_only_plan_cannot_opt_into_human_brief_scope(tmp_path):
    data = plan_data(tmp_path)
    data['brief_sha256'] = digest(BRIEF.encode())
    with pytest.raises(Halt):
        Plan.validate(data)


@pytest.fixture
def held_brief(fleet_home, tmp_path):
    _home, project_id = fleet_home

    def make(description=BRIEF, binding=True):
        data = brief_plan(tmp_path)
        contract = _role_contract(execution_mode='implement', description=description, linear_id=str(uuid4()))
        data.update(project_id=project_id, contract_hash=contract['contract_sha256'])
        with kbc.connect() as conn:
            tid = _create_fleet_task(conn, project_id, contract, initial_status='blocked')
            row = kb.get_task(conn, tid)
            data.update(task_id=tid, workspace=row.workspace_path, branch=row.branch_name,
                        database=str(kb.kanban_db_path(board='default')))
            notify.add_notify_sub(conn, task_id=tid, platform='slack', chat_id='CFIXTURE',
                                  notifier_profile='default', delivery_mode='notify',
                                  delivery_metadata={'slack_team_id': 'TFIXTURE'})
        if not binding:
            del data['brief_sha256']
        return Native(Plan(data, digest(canonical(data))))

    return make


def state(conn):
    return {name: tuple(tuple(row) for row in conn.execute('SELECT * FROM ' + name + ' ORDER BY id'))
            for name in ('tasks', 'task_runs', 'task_events')}


def test_changed_execution_scope_invalidates_actual_operational_approval(held_brief):
    from hermes_cli import kanban_human_gate as gates
    native = held_brief()
    Plan.validate(native.p)
    with kbc.connect() as conn:
        gate_id = kb.create_task(conn, title='Independent operational approval', assignee='director')
        native.p['gate']['task_id'] = gate_id
        native.plan = Plan(native.p, digest(canonical(native.p)))
        event, _ = gates.request_gate(conn, gate_id, stage='merge', sha=native.p['source_sha'],
                                     pr_url='https://github.com/example/controller/pull/1',
                                     tests=Native.approval_tests(native.plan),
                                     reviewer_verdict='APPROVE', requested_by='director')
        gates.decide_gate(conn, gate_id, stage='merge', decision='approve',
                          sha=native.p['source_sha'], actor='UOWNER', platform='slack')
        assert Native.approval(conn, native.plan) == event
        changed = copy.deepcopy(native.p)
        changed['candidate_paths'] = ['tests/test_other.py']
        changed['changes'] = [{'path': 'tests/test_other.py', 'base_sha256': None}]
        changed['test_files'] = ['tests/test_other.py']
        Plan.validate(changed)
        before = state(conn)
        with pytest.raises(Halt):
            Native.approval(conn, Plan(changed, digest(canonical(changed))))
        assert state(conn) == before


def test_native_accepts_bound_frozen_prose_without_admission_or_description_rewrite(held_brief):
    native = held_brief()
    Plan.validate(native.p)
    with kbc.connect() as conn:
        before = state(conn)
        row = native.task(conn)
        assert row['status'] == 'blocked'
        assert row['workflow_template_id'] is None
        assert row['current_run_id'] is None and row['worker_pid'] is None
        assert state(conn) == before
        from hermes_cli.kanban_contract_guard import _extract_contract
        contract, error = _extract_contract(row['body'])
        assert error is None and contract['description'] == BRIEF
        with pytest.raises(Halt):
            Native.approval(conn, native.plan)
        assert state(conn) == before, 'brief binding must not authorise or admit execution'


@pytest.mark.parametrize('case', ['changed_description', 'wrong_brief_hash', 'wrong_contract_hash',
                                 'missing_opt_in', 'blank_brief', 'empty_brief', 'non_string_brief'])
def test_native_rejects_unbound_or_invalid_brief_without_mutation(held_brief, case):
    description = {'changed_description': BRIEF + '\nNew scope', 'blank_brief': ' \n ',
                   'empty_brief': '', 'non_string_brief': None}.get(case, BRIEF)
    native = held_brief(description, binding=case != 'missing_opt_in')
    if case in ('blank_brief', 'empty_brief'):
        native.p['brief_sha256'] = digest(description.encode())
    if case == 'wrong_brief_hash':
        native.p['brief_sha256'] = '0' * 64
    if case == 'wrong_contract_hash':
        native.p['contract_hash'] = '0' * 64
    with kbc.connect() as conn:
        before = state(conn)
        with pytest.raises(Halt):
            native.task(conn)
        assert state(conn) == before

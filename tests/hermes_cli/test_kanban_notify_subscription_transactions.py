"""Subscription registration may explicitly compose with an outer transaction."""
import pytest
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_notify as notify
from tests.hermes_cli.test_kanban_fleet_contract_guard import fleet_home


def test_subscription_composition_commits_with_the_enclosing_task_transaction(fleet_home):
    with kbc.connect() as conn:
        with kb.write_txn(conn):
            tid=kb.create_task(conn,title='Synthetic held subscription',initial_status='blocked')
            notify.add_notify_sub(conn,task_id=tid,platform='slack',chat_id='CFIXTURE',allow_nested=True)
            assert len(notify.list_notify_subs(conn,tid))==1
        assert len(notify.list_notify_subs(conn,tid))==1


def test_outer_failure_rolls_back_task_and_nested_subscription_together(fleet_home):
    with kbc.connect() as conn:
        with pytest.raises(RuntimeError,match='synthetic_outer_failure'):
            with kb.write_txn(conn):
                tid=kb.create_task(conn,title='Synthetic rolled-back subscription',initial_status='blocked')
                notify.add_notify_sub(conn,task_id=tid,platform='slack',chat_id='CFIXTURE',allow_nested=True)
                raise RuntimeError('synthetic_outer_failure')
        assert not conn.execute('SELECT 1 FROM tasks WHERE id=?',(tid,)).fetchone()
        assert notify.list_notify_subs(conn,tid)==[]


def test_default_registration_still_rejects_implicit_nesting(fleet_home):
    with kbc.connect() as conn:
        tid=kb.create_task(conn,title='Default nonnested registration',initial_status='blocked')
        with kb.write_txn(conn):
            with pytest.raises(RuntimeError,match='already inside a transaction'):
                notify.add_notify_sub(conn,task_id=tid,platform='slack',chat_id='CFIXTURE')
        assert notify.list_notify_subs(conn,tid)==[]

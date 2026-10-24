"""Native passive Slack controller-failure delivery, using a recording adapter."""
import asyncio
from types import SimpleNamespace

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway import kanban_watchers_notifier as notifier
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn


class RecordingSlackAdapter:
    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append((chat_id, text, metadata))

    async def handle_message(self, event):
        self.handled.append(event)
        event._gateway_accepted = True


def test_controller_failure_reaches_passive_slack_without_task_transition(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "controller-alert.db"))
    kb.init_db()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="controller failure fixture", assignee="reviewer")
        kbn.add_notify_sub(
            conn, task_id=tid, platform="slack", chat_id="C-FIXTURE",
            notifier_profile="default", delivery_mode="notify",
        )
        before = kb.get_task(conn, tid)
        kb.add_comment(conn, tid, author="director", body="ordinary comments must remain silent")
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "fleet_controller_failed", {
                "phase": "verify", "reason_code": "verification_failed",
                "plan_hash": "a" * 64,
            })
        event_id = conn.execute("SELECT MAX(id) FROM task_events WHERE task_id=?", (tid,)).fetchone()[0]
    adapter = RecordingSlackAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.SLACK: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    runner._active_profile_name = lambda: "default"
    real_sleep = asyncio.sleep

    async def one_tick_sleep(delay):
        if delay == 5:
            return
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", one_tick_sleep)
    asyncio.run(runner._kanban_notifier_watcher(interval=1))
    assert len(adapter.sent) == 1, "trusted controller failure event has no passive Slack delivery"
    chat_id, text, _metadata = adapter.sent[0]
    assert chat_id == "C-FIXTURE" and tid in text
    assert "verify" in text and "verification_failed" in text
    assert "ordinary comments" not in text
    assert adapter.handled == [], "passive failure alert must not invoke a Default inference turn"
    with kbc.connect() as conn:
        after = kb.get_task(conn, tid)
        subscription = kbn.list_notify_subs(conn)[0]
    assert (after.status, after.claim_lock, after.current_run_id) == (before.status, before.claim_lock, before.current_run_id)
    assert subscription["last_ping_event_id"] == event_id


def test_controller_failure_is_diagnostic_but_never_an_automatic_wake():
    event = SimpleNamespace(kind="fleet_controller_failed", payload={"reason_code": "verification_failed"})
    assert notifier.diagnostic_event(event)
    assert event.kind not in notifier._WAKE_KINDS
    assert "commented" not in notifier.TERMINAL_KINDS


def test_controller_progress_reaches_passive_slack_without_task_transition(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "controller-progress.db"))
    kb.init_db()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="controller progress fixture", assignee="reviewer")
        kbn.add_notify_sub(
            conn, task_id=tid, platform="slack", chat_id="C-FIXTURE",
            notifier_profile="default", delivery_mode="notify",
        )
        before = kb.get_task(conn, tid)
        kb.add_comment(conn, tid, author="director", body="ordinary comments must remain silent")
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "fleet_controller_progress", {
                "phase": "verify", "reason_code": "verification_passed",
                "plan_hash": "a" * 64,
            })
        event_id = conn.execute("SELECT MAX(id) FROM task_events WHERE task_id=?", (tid,)).fetchone()[0]
    adapter = RecordingSlackAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.SLACK: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    runner._active_profile_name = lambda: "default"
    real_sleep = asyncio.sleep

    async def one_tick_sleep(delay):
        if delay == 5:
            return
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", one_tick_sleep)
    asyncio.run(runner._kanban_notifier_watcher(interval=1))
    assert len(adapter.sent) == 1, "trusted controller progress event has no passive Slack delivery"
    chat_id, text, _metadata = adapter.sent[0]
    assert chat_id == "C-FIXTURE" and tid in text
    assert "verify" in text and "verification_passed" in text
    assert "ordinary comments" not in text
    assert adapter.handled == [], "passive progress alert must not invoke a Default inference turn"
    with kbc.connect() as conn:
        after = kb.get_task(conn, tid)
        subscription = kbn.list_notify_subs(conn)[0]
    assert (after.status, after.claim_lock, after.current_run_id) == (before.status, before.claim_lock, before.current_run_id)
    assert subscription["last_ping_event_id"] == event_id

    event = SimpleNamespace(kind="fleet_controller_progress", payload={})
    assert event.kind not in notifier._WAKE_KINDS
    assert not notifier.diagnostic_event(event)

import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_cli import kanban_human_gate as gate


SHA_A = "a" * 40
SHA_B = "b" * 40
PR = "https://github.com/example/repo/pull/7"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _task(conn):
    return kb.create_task(conn, title="Reviewed change", assignee="builder")


def _request(conn, tid, sha=SHA_A):
    return gate.request_gate(
        conn, tid, stage="ready", sha=sha, pr_url=PR,
        tests="132 passed; 0 failed", reviewer_verdict="APPROVE",
        requested_by="controller",
    )


def test_gate_decision_is_exact_sha_idempotent_and_audited(kanban_home):
    with kbc.connect() as conn:
        tid = _task(conn)
        request_id, created = _request(conn, tid)
        assert created is True

        with pytest.raises(ValueError, match="stale gate decision rejected"):
            gate.decide_gate(
                conn, tid, stage="ready", decision="approve", sha=SHA_B,
                actor="U_OWNER", platform="slack",
            )

        decision_id, created = gate.decide_gate(
            conn, tid, stage="ready", decision="approve", sha=SHA_A,
            actor="U_OWNER", platform="slack",
        )
        assert created is True
        assert decision_id > request_id

        replay_id, replay_created = gate.decide_gate(
            conn, tid, stage="ready", decision="approve", sha=SHA_A,
            actor="U_OWNER", platform="slack",
        )
        assert (replay_id, replay_created) == (decision_id, False)
        with pytest.raises(ValueError, match="already has decision approve"):
            gate.decide_gate(
                conn, tid, stage="ready", decision="reject", sha=SHA_A,
                actor="U_OWNER", platform="slack",
            )

        status = gate.latest_gate(conn, tid, "ready")
        assert status["decision"] == {
            "stage": "ready", "decision": "approve", "sha": SHA_A,
            "actor": "U_OWNER", "platform": "slack",
            "request_event_id": request_id,
        }
        comments = kb.list_comments(conn, tid)
        assert comments[-1].body == f"HUMAN GATE APPROVE: stage=ready sha={SHA_A} via=slack"


def test_gate_request_rejects_noncanonical_evidence(kanban_home):
    with kbc.connect() as conn:
        tid = _task(conn)
        with pytest.raises(ValueError, match="full 40-character"):
            _request(conn, tid, "abc123")
        with pytest.raises(ValueError, match="canonical"):
            gate.request_gate(
                conn, tid, stage="ready", sha=SHA_A,
                pr_url="https://evil.example/pull/7", tests="ok",
                reviewer_verdict="APPROVE", requested_by="controller",
            )
        with pytest.raises(ValueError, match="independent APPROVE"):
            gate.request_gate(
                conn, tid, stage="ready", sha=SHA_A, pr_url=PR, tests="ok",
                reviewer_verdict="COMMENT", requested_by="controller",
            )


def test_push_gate_allows_request_before_pr_exists(kanban_home):
    with kbc.connect() as conn:
        tid = _task(conn)
        event_id, created = gate.request_gate(
            conn, tid, stage="push", sha=SHA_A, tests="132 passed",
            reviewer_verdict="APPROVE", requested_by="controller",
        )
        assert created is True
        current = gate.latest_gate(conn, tid, "push")
        assert current["request_event_id"] == event_id
        assert current["request"]["pr_url"] == ""


def test_superseding_request_is_idempotent_on_retry(kanban_home):
    with kbc.connect() as conn:
        tid = _task(conn)
        first_id, _ = _request(conn, tid, SHA_A)
        second_id, created = _request(conn, tid, SHA_B)
        retry_id, retry_created = _request(conn, tid, SHA_B)
        assert second_id > first_id
        assert created is True
        assert (retry_id, retry_created) == (second_id, False)
        current = gate.latest_gate(conn, tid, "ready")
        assert current["request"]["supersedes_request_event_id"] == first_id


def test_gate_events_cross_post_completion_comment_gap_and_advance_cursor(kanban_home):
    from gateway.kanban_watchers_notifier import TERMINAL_KINDS

    with kbc.connect() as conn:
        tid = _task(conn)
        kbn.add_notify_sub(conn, task_id=tid, platform="slack", chat_id="C1")
        kb.add_comment(conn, tid, "controller", "PR link posted after completion")
        request_id, _ = _request(conn, tid)

        old, cursor, events = kbn.claim_unseen_events_for_sub(
            conn, task_id=tid, platform="slack", chat_id="C1",
            kinds=TERMINAL_KINDS,
        )
        assert old < request_id
        assert cursor == request_id
        assert [event.kind for event in events] == ["gate_requested"]


def test_slash_gate_decision_uses_authenticated_gateway_actor(kanban_home):
    with kbc.connect() as conn:
        tid = _task(conn)
        _request(conn, tid)

    result = kc.run_slash(
        f"gate-decide {tid} ready approve --sha {SHA_A}",
        actor="U_OWNER", actor_platform="slack",
    )
    assert "Recorded approve" in result
    with kbc.connect() as conn:
        decision = gate.latest_gate(conn, tid, "ready")["decision"]
    assert decision["actor"] == "U_OWNER"
    assert decision["platform"] == "slack"


def test_gate_notification_contains_exact_command_without_waking_llm():
    from gateway import kanban_watchers_notifier as notifier

    ev = SimpleNamespace(kind="gate_requested", payload={
        "stage": "ready", "sha": SHA_A, "pr_url": PR,
        "tests": "132 passed", "reviewer_verdict": "APPROVE",
    })
    notice = SimpleNamespace(
        head="[board/task]", title="Reviewed change", task_id="t_123",
        board_slug="hermes-agent",
    )
    msg, handoff, detail = notifier._fmt_gate_requested(ev, notice)
    assert SHA_A in msg
    assert PR in msg
    assert f"/kanban --board hermes-agent gate-decide t_123 ready approve --sha {SHA_A}" in msg
    assert handoff is None and detail is None
    assert "gate_requested" not in notifier._WAKE_KINDS


def test_gate_notification_fails_closed_without_board_identity():
    from gateway import kanban_watchers_notifier as notifier

    ev = SimpleNamespace(kind="gate_requested", payload={
        "stage": "ready", "sha": SHA_A, "pr_url": PR,
        "tests": "132 passed", "reviewer_verdict": "APPROVE",
    })
    notice = SimpleNamespace(head="[task]", title="Reviewed change", task_id="t_123")

    msg, handoff, detail = notifier._fmt_gate_requested(ev, notice)

    assert "could not be routed safely" in msg
    assert "/kanban" not in msg
    assert handoff is None and detail is None

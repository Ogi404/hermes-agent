from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform
from gateway.slash_commands import GatewaySlashCommandsMixin
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_human_gate as gate


SHA = "a" * 40
PR = "https://github.com/example/repo/pull/7"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _runner(admin: bool):
    runner = object.__new__(GatewaySlashCommandsMixin)
    runner._resume_caller_is_admin = lambda source: admin
    return runner


def _event(text: str, user_id: str = "U_OPERATOR"):
    return SimpleNamespace(
        text=text,
        source=SimpleNamespace(
            platform=Platform.SLACK,
            chat_id="C_GATE",
            chat_type="channel",
            thread_id="123.456",
            user_id=user_id,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["gate-request", "gate-decide"])
async def test_non_admin_gate_mutation_is_denied_before_dispatch(monkeypatch, action):
    run_slash = MagicMock(side_effect=AssertionError("unauthorized mutation reached run_slash"))
    monkeypatch.setattr("hermes_cli.kanban.run_slash", run_slash)
    text = f"/kanban {action} t_missing ready approve --sha {SHA}"

    out = await GatewaySlashCommandsMixin._handle_kanban_command(_runner(False), _event(text))

    assert out == "Human-gate mutations require an explicitly configured gateway administrator."
    run_slash.assert_not_called()


@pytest.mark.asyncio
async def test_admin_gate_decision_succeeds_through_gateway_and_binds_actor(kanban_home):
    kb.create_board("hermes-agent")
    with kbc.connect(board="hermes-agent") as conn:
        tid = kb.create_task(conn, title="Reviewed change", assignee="builder")
        gate.request_gate(
            conn, tid, stage="ready", sha=SHA, pr_url=PR,
            tests="132 passed", reviewer_verdict="APPROVE",
            requested_by="controller",
        )

    out = await GatewaySlashCommandsMixin._handle_kanban_command(
        _runner(True),
        _event(
            f"/kanban --board hermes-agent gate-decide {tid} ready approve --sha {SHA}",
            "U_OWNER",
        ),
    )

    assert "Recorded approve" in out
    with kbc.connect(board="hermes-agent") as conn:
        decision = gate.latest_gate(conn, tid, "ready")["decision"]
    assert decision["actor"] == "U_OWNER"
    assert decision["platform"] == "slack"

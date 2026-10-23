"""Durable, zero-LLM human gates for Kanban-backed delivery workflows.

Gate requests carry a deliberately small evidence envelope to the originating
chat.  Decisions are bound to the exact requested commit SHA and recorded as
first-class task events, so a delayed Slack command cannot approve replacement
code accidentally.
"""

from __future__ import annotations

import re
import time
from typing import Any, Optional

from hermes_cli import kanban_db as kb


GATE_STAGES = ("push", "ready", "merge")
GATE_DECISIONS = ("approve", "hold", "reject")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_PR_RE = re.compile(r"^https://github\.com/[^/\s]+/[^/\s]+/pull/[1-9][0-9]*$")


def _clean(value: Any, *, limit: int) -> str:
    text = str(kb.redact_review_value(value or "")).strip()
    text = " ".join(text.split())
    return text[:limit]


def _sha(value: str) -> str:
    value = str(value or "").strip().lower()
    if not _SHA_RE.fullmatch(value):
        raise ValueError("gate SHA must be the full 40-character lowercase commit SHA")
    return value


def _stage(value: str) -> str:
    value = str(value or "").strip().lower()
    if value not in GATE_STAGES:
        raise ValueError(f"gate stage must be one of: {', '.join(GATE_STAGES)}")
    return value


def _decision(value: str) -> str:
    value = str(value or "").strip().lower()
    if value not in GATE_DECISIONS:
        raise ValueError(f"gate decision must be one of: {', '.join(GATE_DECISIONS)}")
    return value


def _events_desc(conn, task_id: str, *kinds: str):
    marks = ",".join("?" for _ in kinds)
    rows = conn.execute(
        f"SELECT id, kind, payload FROM task_events WHERE task_id = ? "
        f"AND kind IN ({marks}) ORDER BY id DESC",
        (task_id, *kinds),
    ).fetchall()
    for row in rows:
        yield int(row["id"]), str(row["kind"]), kb._json_dict(row["payload"])


def latest_gate(conn, task_id: str, stage: Optional[str] = None) -> Optional[dict]:
    """Return the newest request with its decision, if one exists."""
    wanted = _stage(stage) if stage is not None else None
    decisions: list[tuple[int, dict]] = []
    for event_id, kind, payload in _events_desc(conn, task_id, "gate_requested", "gate_decided"):
        if wanted is not None and payload.get("stage") != wanted:
            continue
        if kind == "gate_decided":
            decisions.append((event_id, payload))
            continue
        decision = next(
            (item for item_id, item in decisions
             if item_id > event_id and item.get("request_event_id") == event_id),
            None,
        )
        return {"request_event_id": event_id, "request": payload, "decision": decision}
    return None


def request_gate(
    conn,
    task_id: str,
    *,
    stage: str,
    sha: str,
    pr_url: str = "",
    tests: str,
    reviewer_verdict: str,
    requested_by: str,
) -> tuple[int, bool]:
    """Create a human-gate request. Returns ``(event_id, created)``."""
    stage, sha = _stage(stage), _sha(sha)
    pr_url = str(pr_url or "").strip()
    if not pr_url and stage != "push":
        raise ValueError(f"{stage} gate requires a canonical GitHub pull-request URL")
    if pr_url and not _PR_RE.fullmatch(pr_url):
        raise ValueError("gate PR must be a canonical https://github.com/OWNER/REPO/pull/N URL")
    tests = _clean(tests, limit=600)
    verdict = _clean(reviewer_verdict, limit=80).upper()
    requested_by = _clean(requested_by, limit=120) or "controller"
    if not tests:
        raise ValueError("gate request requires a test summary")
    if verdict != "APPROVE":
        raise ValueError("gate request requires an independent APPROVE reviewer verdict")

    with kb.write_txn(conn):
        kb._require_task(conn, task_id)
        current = latest_gate(conn, task_id, stage)
        payload = {
            "stage": stage,
            "sha": sha,
            "pr_url": pr_url,
            "tests": tests,
            "reviewer_verdict": verdict,
            "requested_by": requested_by,
        }
        if current and current["decision"] is None:
            comparable = dict(current["request"])
            comparable.pop("supersedes_request_event_id", None)
            if comparable == payload:
                return int(current["request_event_id"]), False
        if current and current["decision"] is None:
            payload["supersedes_request_event_id"] = int(current["request_event_id"])
        kb._append_event(conn, task_id, "gate_requested", payload)
        event_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        kb._insert_comment(
            conn, task_id, requested_by,
            f"HUMAN GATE REQUESTED: stage={stage} sha={sha} pr={pr_url} "
            f"reviewer={verdict} tests={tests}",
            int(time.time()),
        )
        return event_id, True


def decide_gate(
    conn,
    task_id: str,
    *,
    stage: str,
    decision: str,
    sha: str,
    actor: str,
    platform: str = "cli",
) -> tuple[int, bool]:
    """Record an exact-SHA decision. Returns ``(event_id, created)``."""
    stage, decision, sha = _stage(stage), _decision(decision), _sha(sha)
    actor = _clean(actor, limit=120)
    platform = _clean(platform, limit=40) or "cli"
    if not actor:
        raise ValueError("gate decision requires an authenticated actor")

    with kb.write_txn(conn):
        kb._require_task(conn, task_id)
        current = latest_gate(conn, task_id, stage)
        if current is None:
            raise ValueError(f"no {stage} gate request exists for {task_id}")
        expected = str(current["request"].get("sha") or "")
        if sha != expected:
            raise ValueError(
                f"stale gate decision rejected: requested SHA is {expected}, received {sha}"
            )
        prior = current["decision"]
        if prior is not None:
            if prior.get("decision") == decision and prior.get("sha") == sha:
                for event_id, kind, payload in _events_desc(conn, task_id, "gate_decided"):
                    if payload == prior:
                        return event_id, False
            raise ValueError(
                f"{stage} gate request for {sha} already has decision {prior.get('decision')}"
            )
        payload = {
            "stage": stage,
            "decision": decision,
            "sha": sha,
            "actor": actor,
            "platform": platform,
            "request_event_id": int(current["request_event_id"]),
        }
        kb._append_event(conn, task_id, "gate_decided", payload)
        event_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        kb._insert_comment(
            conn, task_id, actor,
            f"HUMAN GATE {decision.upper()}: stage={stage} sha={sha} via={platform}",
            int(time.time()),
        )
        return event_id, True

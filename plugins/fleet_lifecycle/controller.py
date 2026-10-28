"""Strict plans, native Kanban events, and an intent-before-action ledger.

No side effects occur at import or registration. Backend injection is for tests;
the operator command always constructs Runtime, never a model-provided callback.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import signal
from dataclasses import dataclass


WORKFLOW = "fleet-source-review-v1"
PROGRESS = {"verify": "verification_passed", "precommit_review": "review_approved",
            "commit": "commit_created", "exact_review": "review_approved"}
FAILURE = {"admit": "admission_failed", "waiting_source": "handoff_invalid",
           "verify": "verification_failed", "precommit_review": "review_failed",
           "commit": "commit_failed", "exact_review": "review_failed",
           "request_push": "gate_failed"}
NEXT = {"verify": "precommit_review", "precommit_review": "commit",
        "commit": "exact_review", "exact_review": "request_push",
        "request_push": "awaiting_push"}


class Halt(ValueError):
    """Only fixed reason codes may leave the trusted boundary."""

    def __init__(self, code="identity_drift"):
        self.code = code
        super().__init__(code)


def require(condition, code="identity_drift"):
    if not condition:
        raise Halt(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def _pairs(items):
    result = {}
    for key, value in items:
        require(key not in result, "invalid_plan")
        result[key] = value
    return result


def strict_json(raw):
    def invalid(_):
        raise Halt("invalid_plan")
    try:
        return json.loads(raw, object_pairs_hook=_pairs, parse_constant=invalid)
    except (ValueError, UnicodeError, RecursionError, TypeError):
        raise Halt("invalid_plan") from None


def fields(value, keys):
    require(isinstance(value, dict) and set(value) == set(keys.split()), "invalid_plan")


def token(value, pattern, limit=256):
    require(isinstance(value, str) and len(value) <= limit
            and re.fullmatch(pattern, value) is not None, "invalid_plan")


def sha(value, size=64):
    token(value, "[0-9a-f]{" + str(size) + "}", size)


def absolute(value):
    token(value, r"/[^\x00-\x1f,:]*", 2048)
    require(str(Path(value)) == value and ".." not in Path(value).parts, "invalid_plan")


def relative(value):
    token(value, r"[A-Za-z0-9_][A-Za-z0-9_./-]*", 240)
    parts = PurePosixPath(value).parts
    require(str(PurePosixPath(value)) == value and not any(
        p in {".", "..", ".git", ".hermes"} for p in parts), "invalid_plan")


def safe_path(path, *, directory=False, private=False):
    path = Path(path)
    require(path.is_absolute())
    for parent in [*reversed(path.parents), path]:
        info = parent.lstat()
        require(not stat.S_ISLNK(info.st_mode))
        if private:
            require(info.st_uid in {0, os.getuid()})
            require(not info.st_mode & 0o022 or bool(info.st_mode & stat.S_ISVTX))
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
    if private:
        require(info.st_uid == os.getuid() and not info.st_mode & 0o077)
    return path


def read_regular(path, limit=2 * 1024 * 1024):
    path = safe_path(Path(path))
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_size <= limit)
        data = os.read(fd, limit + 1)
        require(len(data) <= limit and len(data) == info.st_size)
        return data
    finally:
        os.close(fd)


@dataclass(frozen=True)
class Plan:
    data: dict
    digest: str

    @classmethod
    def load(cls, path):
        try:
            data = strict_json(read_regular(Path(path), 128 * 1024))
            cls.validate(data)
            return cls(data, digest(canonical(data)))
        except (OSError, Halt, TypeError, OverflowError):
            raise Halt("invalid_plan") from None

    @staticmethod
    def validate(p):
        fields(p, "schema board database task_id project_id workspace branch base contract_hash core source_sha candidate_paths test_files mutation resources profiles native state_root slack gate context commit")
        require(type(p["schema"]) is int and p["schema"] == 1, "invalid_plan")
        for key in ("database", "workspace", "core", "state_root"):
            absolute(p[key])
        token(p["board"], r"[a-z0-9][a-z0-9_-]{0,79}")
        token(p["task_id"], r"t_[0-9a-f]{8}")
        token(p["project_id"], r"[A-Za-z0-9_-]{1,100}")
        token(p["branch"], r"[A-Za-z0-9][A-Za-z0-9_/-]{1,160}")
        sha(p["base"], 40); sha(p["source_sha"], 40); sha(p["contract_hash"])
        for key in ("candidate_paths", "test_files"):
            require(isinstance(p[key], list) and 0 < len(p[key]) <= 24, "invalid_plan")
            require(p[key] == sorted(set(p[key])), "invalid_plan")
            for item in p[key]:
                relative(item)
                require(not item.startswith(("scripts/", "plugins/fleet_lifecycle/"))
                        and not item.endswith("conftest.py"), "invalid_plan")
        for item in p["test_files"]:
            require(re.fullmatch(r"tests/(?:[A-Za-z0-9_-]+/)*test_[A-Za-z0-9_]+[.]py", item), "invalid_plan")
        m = p["mutation"]
        fields(m, "path base_sha256 mutant_sha256 before after before_sha256 after_sha256 test_file test_name")
        sha(m["base_sha256"]); sha(m["mutant_sha256"])
        relative(m["path"])
        require(p["candidate_paths"] == p["test_files"] and len(p["test_files"]) == 1
                and m["path"] not in p["candidate_paths"] and not m["path"].startswith("tests/")
                and m["test_file"] in p["test_files"], "invalid_plan")
        token(m["test_name"], r"test_[A-Za-z0-9_]+", 160)
        for key in ("before", "after"):
            require(isinstance(m[key], str) and 0 < len(m[key].encode()) <= 8192, "invalid_plan")
            sha(m[key + "_sha256"])
            require(digest(m[key].encode()) == m[key + "_sha256"], "invalid_plan")
        require(m["before"] != m["after"], "invalid_plan")
        r = p["resources"]
        fields(r, "image seconds cpus memory_mb pids scratch_mb")
        token(r["image"], r"sha256:[0-9a-f]{64}", 71)
        for key, high in (("seconds", 900), ("cpus", 4), ("memory_mb", 8192), ("pids", 256), ("scratch_mb", 4096)):
            require(type(r[key]) is int and 1 <= r[key] <= high, "invalid_plan")
        require(r["memory_mb"] >= 256 and r["scratch_mb"] >= 64, "invalid_plan")
        fields(p["profiles"], "director builder reviewer")
        for role, profile in p["profiles"].items():
            fields(profile, "name home config_sha256")
            token(profile["name"], r"[a-z][a-z0-9_-]{0,63}")
            require(profile["name"] != "default", "invalid_plan")
            absolute(profile["home"]); sha(profile["config_sha256"])
        require(len({v["home"] for v in p["profiles"].values()}) == 3
                and len({v["name"] for v in p["profiles"].values()}) == 3, "invalid_plan")
        require(p["state_root"] == str(Path(p["profiles"]["director"]["home"]) / "fleet-lifecycle"), "invalid_plan")
        require(not Path(p["state_root"]).is_relative_to(p["workspace"]), "invalid_plan")
        n = p["native"]
        fields(n, "launcher_entry launcher launcher_sha256 runtime_command_sha256 client client_sha256 binary_entry binary binary_sha256 tools_root model provider")
        for key in ("launcher_entry", "launcher", "client", "binary_entry", "binary", "tools_root"):
            absolute(n[key])
        for key in ("launcher_sha256", "runtime_command_sha256", "client_sha256", "binary_sha256"):
            sha(n[key])
        require(Path(n["client"]).is_relative_to(p["profiles"]["reviewer"]["home"]), "invalid_plan")
        require(n["provider"] == "fleet-claude-cli" and n["model"] == "claude-sonnet-5", "invalid_plan")
        fields(p["slack"], "profile channel thread team")
        require(p["slack"]["profile"] == "default", "invalid_plan")
        token(p["slack"]["channel"], r"C[A-Z0-9]{1,40}")
        token(p["slack"]["team"], r"T[A-Z0-9]{1,40}")
        token(p["slack"]["thread"], r"(?:[0-9]{10}[.][0-9]{6})?")
        g = p["gate"]
        fields(g, "task_id stage source_sha actor")
        token(g["task_id"], r"t_[0-9a-f]{8}")
        require(g["task_id"] != p["task_id"] and g["stage"] in {"push", "ready", "merge"}
                and g["source_sha"] == p["source_sha"], "invalid_plan")
        token(g["actor"], r"U[A-Z0-9]{1,40}")
        require(isinstance(p["context"], list) and 0 < len(p["context"]) <= 12, "invalid_plan")
        for entry in p["context"]:
            fields(entry, "path functions"); relative(entry["path"])
            require(isinstance(entry["functions"], list), "invalid_plan")
        c = p["commit"]
        fields(c, "name email message")
        token(c["name"], r"[A-Za-z][A-Za-z ._-]{0,63}")
        token(c["email"], r"[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+", 128)
        token(c["message"], r"[A-Za-z0-9][A-Za-z0-9 ()_:,./-]{0,159}")


class Ledger:
    """One interprocess lock and FULL-synchronous SQLite ledger per operator home."""

    def __init__(self, root):
        self.root = Path(root)
        self.lock = self.conn = None

    def __enter__(self):
        if not self.root.exists():
            safe_path(self.root.parent, directory=True)
            self.root.mkdir(mode=0o700)
        safe_path(self.root, directory=True, private=True)
        self.lock = os.open(self.root / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.lock); self.lock = None
            return None
        path = self.root / "ledger.sqlite3"
        if not path.exists():
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        safe_path(path, private=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("CREATE TABLE IF NOT EXISTS plans (hash TEXT PRIMARY KEY, state TEXT NOT NULL)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS authorizations (board TEXT, event INTEGER, plan TEXT, PRIMARY KEY(board,event))")
        self.conn.commit()
        return self

    def __exit__(self, *exc):
        if self.conn:
            self.conn.close()
        if self.lock is not None:
            os.close(self.lock)

    def load(self, key):
        row = self.conn.execute("SELECT state FROM plans WHERE hash=?", (key,)).fetchone()
        return strict_json(row[0]) if row else None

    def save(self, key, value):
        with self.conn:
            self.conn.execute("INSERT INTO plans VALUES (?,?) ON CONFLICT(hash) DO UPDATE SET state=excluded.state",
                              (key, canonical(value).decode()))


class Native:
    def __init__(self, plan):
        self.plan = plan
        self.p = plan.data

    @contextlib.contextmanager
    def connect(self, writable=False):
        from hermes_cli import kanban_db as kb
        path = safe_path(Path(self.p["database"]))
        require(kb.kanban_db_path(board=self.p["board"]) == path)
        conn = sqlite3.connect(path.as_uri() + ("?mode=rw" if writable else "?mode=ro"), uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def approval_tests(plan):
        p = plan.data
        return "fleet-plan={} source={} task={} contract={}".format(plan.digest, p["gate"]["source_sha"], p["task_id"], p["contract_hash"])

    @staticmethod
    def approval(conn, plan):
        from hermes_cli.kanban_human_gate import latest_gate
        g = plan.data["gate"]
        current = latest_gate(conn, g["task_id"], g["stage"])
        require(current is not None, "approval_invalid")
        request, decision = current["request"], current["decision"]
        request_id = current["request_event_id"]
        require(request.get("sha") == g["source_sha"]
                and request.get("tests") == Native.approval_tests(plan)
                and request.get("reviewer_verdict") == "APPROVE"
                and isinstance(decision, dict), "approval_invalid")
        require(decision.get("request_event_id") == request_id
                and decision.get("sha") == g["source_sha"] and decision.get("stage") == g["stage"]
                and decision.get("decision") == "approve" and decision.get("platform") == "slack"
                and decision.get("actor") == g["actor"], "approval_invalid")
        return request_id

    @staticmethod
    def frozen(row):
        keys = ("body", "title", "project_id", "workspace_kind", "workspace_path", "branch_name",
                "created_by", "idempotency_key", "model_override", "provider_override")
        return digest(canonical({key: row[key] for key in keys}))

    def no_workers(self, conn):
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_db_dispatch import _worker_alive
        for run in conn.execute("SELECT * FROM task_runs WHERE task_id=?", (self.p["task_id"],)):
            require(run["ended_at"] is not None, "handoff_invalid")
            if run["worker_pid"] is not None:
                require((run["claim_lock"] or "").startswith(kb._host_prefix()), "handoff_invalid")
                require(not _worker_alive(run["worker_pid"], run["worker_started_at"]), "handoff_invalid")

    def scope_description(self, *, markdown=False):
        description = canonical({key: self.p[key] for key in
            ("base", "candidate_paths", "test_files", "mutation")}).decode()
        if markdown:
            return "fleet-lifecycle-test-only-v1\n\n```json\n" + description + "\n```"
        return "fleet-lifecycle-test-only-v1 " + description

    def subscription(self, conn):
        from hermes_cli.kanban_db_notify import list_notify_subs
        route = self.p["slack"]
        rows = [s for s in list_notify_subs(conn, self.p["task_id"])
                if s["platform"] == "slack" and s["chat_id"] == route["channel"]
                and (s.get("thread_id") or "") == route["thread"]
                and s.get("notifier_profile") == route["profile"] and s.get("delivery_mode") == "notify"
                and (s.get("delivery_metadata") or {}).get("slack_team_id") == route["team"]]
        require(len(rows) == 1)
        return rows[0]

    def task(self, conn, admitted=False):
        from hermes_cli.kanban_contract_guard import fleet_contract_guard_reason, _extract_contract
        p = self.p
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (p["task_id"],)).fetchone()
        require(row is not None)
        require(row["created_by"] == "fleet-linear-reconciler"
                and (row["idempotency_key"] or "").startswith("linear:"))
        require(fleet_contract_guard_reason(row) is None)
        contract, error = _extract_contract(row["body"])
        require(error is None and contract["schema_version"] == 2 and contract["contract_sha256"] == p["contract_hash"])
        require(row["project_id"] == p["project_id"] and row["workspace_kind"] == "worktree"
                and row["workspace_path"] == p["workspace"] and row["branch_name"] == p["branch"])
        require(contract["task_lane"] == "implementation" and contract["execution_mode"] == "implement"
                and contract["description"] in (self.scope_description(), self.scope_description(markdown=True)))
        for role, lane in (("director", "orchestration"), ("builder", "implementation"), ("reviewer", "review")):
            require(p["profiles"][role]["name"] in contract[lane + "_profiles"])
        if admitted:
            require(row["workflow_template_id"] == WORKFLOW and row["current_step_key"] == "waiting_source")
        else:
            require(row["status"] in {"blocked", "triage"} and row["workflow_template_id"] is None
                    and row["current_step_key"] is None and row["claim_lock"] is None
                    and row["current_run_id"] is None and row["worker_pid"] is None)
            self.no_workers(conn)
            require(not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind=\"fleet_controller_admitted\"", (p["task_id"],)).fetchone())
        self.subscription(conn)
        return row

    def admit(self, runtime):
        from hermes_cli import kanban_db as kb
        with self.connect(True) as conn, kb.write_txn(conn):
            row = self.task(conn)
            request_id = self.approval(conn, self.plan)
            require(request_id == self.authorization_id, "approval_invalid")
            for event in conn.execute("SELECT payload FROM task_events WHERE kind='fleet_controller_admitted'"):
                require(strict_json(event["payload"]).get("request_event_id") != request_id, "approval_invalid")
            runtime.admission_snapshot()
            run_floor = conn.execute("SELECT COALESCE(MAX(id),0) FROM task_runs WHERE task_id=?",
                                     (self.p["task_id"],)).fetchone()[0]
            conn.execute("UPDATE tasks SET workflow_template_id=?,current_step_key=? WHERE id=?",
                         (WORKFLOW, "waiting_source", self.p["task_id"]))
            kb._append_event(conn, self.p["task_id"], "fleet_controller_admitted", {
                "plan_hash": self.plan.digest, "request_event_id": request_id, "run_floor": run_floor, "frozen": self.frozen(row),
                "source_sha": self.p["source_sha"], "contract_hash": self.p["contract_hash"]})
            return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    def handoff(self, state):
        with self.connect() as conn:
            row = self.task(conn, True)
            require(self.frozen(row) == state["frozen"], "handoff_invalid")
            if row["status"] in {"blocked", "triage", "running", "ready", "todo"}:
                return None
            require(row["status"] == "review" and row["claim_lock"] is None
                    and row["worker_pid"] is None and row["current_run_id"] is None, "handoff_invalid")
            events = conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind=\"review_requested\" AND id>? ORDER BY id",
                                  (self.p["task_id"], state["admission_event"])).fetchall()
            require(len(events) == 1, "handoff_invalid")
            ev = events[0]
            run = conn.execute("SELECT * FROM task_runs WHERE id=? AND task_id=?", (ev["run_id"], self.p["task_id"])).fetchone()
            admission = conn.execute("SELECT payload FROM task_events WHERE id=? AND task_id=? AND kind='fleet_controller_admitted'",
                                     (state["admission_event"], self.p["task_id"])).fetchone()
            require(admission is not None, "handoff_invalid")
            evidence = strict_json(admission["payload"])
            require(evidence["plan_hash"] == self.plan.digest and evidence["frozen"] == state["frozen"], "handoff_invalid")
            require(run is not None and run["id"] > evidence["run_floor"] and run["status"] == "review" and run["profile"] == self.p["profiles"]["builder"]["name"]
                    and run["outcome"] == "review_requested" and run["ended_at"] is not None
                    and run["ended_at"] >= run["started_at"] and run["claim_lock"]
                    and run["step_key"] == "waiting_source", "handoff_invalid")
            self.no_workers(conn)
            payload = strict_json(ev["payload"])
            require(payload.get("implementer") == run["profile"]
                    and payload.get("reviewer") == self.p["profiles"]["reviewer"]["name"], "handoff_invalid")
            return {"event_id": ev["id"], "run_id": run["id"]}

    def stable(self, state):
        require(self.handoff(state) == state["handoff"])

    def emit_on(self, conn, kind, phase, reason, run_id=None, exact=None):
        from hermes_cli import kanban_db as kb
        require(kind in {"fleet_controller_progress", "fleet_controller_failed"})
        allowed = {FAILURE.get(phase), "identity_drift", "interrupted_action", "review_rejected"}
        require(phase in FAILURE and (reason == PROGRESS.get(phase) if kind.endswith("progress") else reason in allowed))
        payload = {"phase": phase, "reason_code": reason, "plan_hash": self.plan.digest}
        sha(self.plan.digest)
        if exact is not None:
            sha(exact, 40); payload["sha"] = exact
        with kb.write_txn(conn):
            for row in conn.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind=?", (self.p["task_id"], kind)):
                prior = strict_json(row["payload"])
                if prior.get("plan_hash") == self.plan.digest and prior.get("phase") == phase:
                    require(prior == payload)
                    return row["id"]
            kb._append_event(conn, self.p["task_id"], kind, payload, run_id=run_id)
            return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    def emit(self, event, state):
        with self.connect(True) as conn:
            return self.emit_on(conn, *event, run_id=(state.get("handoff") or {}).get("run_id"),
                                exact=state.get("head"))

    def request_push(self, state):
        from hermes_cli.kanban_human_gate import request_gate
        self.stable(state)
        proof = digest(canonical(state["verification"]))
        tests = "fleet-plan={} receipts={} contract={}".format(self.plan.digest, proof, self.p["contract_hash"])
        with self.connect(True) as conn:
            return request_gate(conn, self.p["task_id"], stage="push", sha=state["head"], tests=tests,
                                reviewer_verdict="APPROVE", requested_by=self.p["profiles"]["director"]["name"])[0]

    def delivery(self, event_id):
        with self.connect() as conn:
            sub = self.subscription(conn)
            return "delivered" if sub.get("last_ping_event_id") == event_id else "pending"


class Controller:
    def __init__(self, plan, runtime, native=None):
        self.plan, self.runtime = plan, runtime
        self.native = native or Native(plan)

    def check(self):
        self.runtime.principal()
        self.runtime.identity()
        self.runtime.admission_snapshot()
        with self.native.connect() as conn:
            row = self.native.task(conn)
            authorization = self.native.approval(conn, self.plan)
        return row, authorization

    def flush(self, ledger, state):
        for event in list(state.get("outbox", [])):
            event_id = self.native.emit(event, state)
            state.setdefault("events", []).append(event_id)
            state["outbox"].remove(event)
            ledger.save(self.plan.digest, state)

    def halt(self, ledger, state, phase, reason):
        if state.get("owned"):
            self.runtime.cleanup(state)
        state["stage"], state["intent"], state["reason_code"] = "halted", None, reason
        state.setdefault("outbox", []).append(["fleet_controller_failed", phase, reason])
        ledger.save(self.plan.digest, state)
        self.flush(ledger, state)
        return state

    def admit(self):
        self.runtime.principal()
        with Ledger(self.plan.data["state_root"]) as ledger:
            if ledger is None:
                return {"stage": "overlap"}
            existing = ledger.load(self.plan.digest)
            if existing:
                return self._recover(ledger, existing)
            row, authorization = self.check()
            board = self.plan.data.get("database", "unit-fixture")
            require(not ledger.conn.execute("SELECT 1 FROM authorizations WHERE board=? AND event=?",
                                            (board, authorization)).fetchone(), "approval_invalid")
            state = {"stage": "admit", "intent": "admit", "attempts": [], "outbox": [],
                     "authorization_event": authorization}
            if row is not None:
                state["frozen"] = Native.frozen(row)
            with ledger.conn:
                ledger.conn.execute("INSERT INTO authorizations VALUES (?,?,?)", (board, authorization, self.plan.digest))
                ledger.conn.execute("INSERT INTO plans VALUES (?,?)", (self.plan.digest, canonical(state).decode()))
            self.native.authorization_id = authorization
            try:
                self.runtime.identity()
                state["admission_event"] = self.native.admit(self.runtime)
                state.update(stage="waiting_source", intent=None)
                ledger.save(self.plan.digest, state)
                return state
            except Exception:
                return self.halt(ledger, state, "admit", "admission_failed")

    def _recover(self, ledger, state):
        if state.get("intent"):
            self.runtime.cleanup(state)
            return self.halt(ledger, state, state["intent"], "interrupted_action")
        if state["stage"] not in {"halted", "awaiting_push"}:
            try:
                self.runtime.identity()
                with self.native.connect() as conn:
                    row = self.native.task(conn, True)
                    if row is not None:
                        require(Native.frozen(row) == state["frozen"])
            except Exception:
                return self.halt(ledger, state, state["stage"], "identity_drift")
        self.flush(ledger, state)
        return state

    def tick(self):
        self.runtime.principal()
        with Ledger(self.plan.data["state_root"]) as ledger:
            if ledger is None:
                return {"stage": "overlap"}
            state = ledger.load(self.plan.digest)
            require(state is not None, "not_admitted")
            self._recover(ledger, state)
            phase = state["stage"]
            if phase in {"halted", "awaiting_push"}:
                return state
            try:
                self.runtime.identity()
                if phase == "waiting_source":
                    handoff = self.native.handoff(state)
                    if handoff is None:
                        return state
                    state["handoff"] = handoff
                    state["snapshot"] = self.runtime.snapshot()
                    self.native.stable(state)
                    state["stage"] = "verify"
                    ledger.save(self.plan.digest, state)
                    return state
                self.native.stable(state)
                self.runtime.unchanged(state)
                if phase in {"precommit_review", "exact_review"}:
                    if hasattr(self.runtime, "prepare_review"):
                        self.runtime.prepare_review(state, phase)
                    require(phase not in state["attempts"] and len(state["attempts"]) < 2)
                    state["attempts"].append(phase)
                state["intent"] = phase
                ledger.save(self.plan.digest, state)
                self.runtime.persist = lambda: ledger.save(self.plan.digest, state)
                result = self.runtime.action(phase, state, self.native)
                self.runtime.identity()
                self.native.stable(state)
                self.runtime.unchanged(state, result if phase == "commit" else None)
                if phase == "verify":
                    state["verification"] = result
                elif phase == "commit":
                    state["head"] = result
                elif phase == "request_push":
                    state["gate_event"] = result
                else:
                    state[phase] = result
                state.update(stage=NEXT[phase], intent=None)
                if phase in PROGRESS:
                    state["outbox"].append(["fleet_controller_progress", phase, PROGRESS[phase]])
                ledger.save(self.plan.digest, state)
                self.flush(ledger, state)
                return state
            except Exception as exc:
                reason = exc.code if isinstance(exc, Halt) and exc.code in {"identity_drift", "review_rejected"} else FAILURE[phase]
                return self.halt(ledger, state, phase, reason)


def setup_cli(parser):
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("hash", "check", "admit", "tick"):
        child = actions.add_parser(name)
        child.add_argument("--plan", required=True, type=Path)


def command(args):
    previous = signal.getsignal(signal.SIGTERM)
    def interrupted(_signum, _frame):
        raise Halt("interrupted_action")
    signal.signal(signal.SIGTERM, interrupted)
    try:
        plan = Plan.load(args.plan)
        if args.action == "hash":
            print(canonical({"plan_hash": plan.digest, "approval_tests": Native.approval_tests(plan)}).decode())
            return 0
        from plugins.fleet_lifecycle.runtime import Runtime
        controller = Controller(plan, Runtime(plan))
        if args.action == "check":
            controller.check()
            print("plan_valid " + plan.digest)
            return 0
        state = getattr(controller, args.action)()
        summary = {"plan_hash": plan.digest, "stage": state["stage"]}
        if state.get("gate_event"):
            summary["gate_delivery"] = controller.native.delivery(state["gate_event"])
        print(canonical(summary).decode())
        return 1 if state["stage"] == "halted" else 0
    except Exception as exc:
        print("fleet_lifecycle: " + (exc.code if isinstance(exc, Halt) else "controller_failed"))
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)

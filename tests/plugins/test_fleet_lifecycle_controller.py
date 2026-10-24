"""Trusted lifecycle operator surface: no inference or live fleet operations."""
import argparse
from hermes_cli.plugins import PluginManager


def test_fleet_lifecycle_cli_is_discoverable_and_rejects_missing_plan(tmp_path):
    home = tmp_path / "operator-home"
    home.mkdir()
    (home / "config.yaml").write_text("plugins:" + chr(10) + "  enabled: [teams_pipeline, fleet_lifecycle]" + chr(10))
    manager = PluginManager(scope_key=str(home))
    manager.discover_and_load()
    assert "teams-pipeline" in manager._cli_commands, "fixture must discover an existing enabled CLI"
    assert "fleet-lifecycle" in manager._cli_commands, (
        "persistent trusted lifecycle has no executable operator CLI"
    )
    command = manager._cli_commands["fleet-lifecycle"]
    assert callable(command["handler_fn"]), "registration alone is not a controller"
    parser = argparse.ArgumentParser()
    command["setup_fn"](parser)
    args = parser.parse_args(["check", "--plan", str(tmp_path / "absent-plan.json")])
    assert command["handler_fn"](args) != 0, "missing admission plan must fail closed"

# Explicit fake side effects below are unit fixtures, never live acceptance.
import json
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_human_gate as gates


def test_strict_json_rejects_duplicates_nonfinite_and_unknown(tmp_path):
    from plugins.fleet_lifecycle.controller import Plan, Halt
    before = set(tmp_path.iterdir())
    for text in ("{\"schema\":1,\"schema\":1}", "{\"schema\":NaN}", "{\"extra\":1}"):
        path = tmp_path / "plan.json"
        path.write_text(text)
        with pytest.raises(Halt):
            Plan.load(path)
    assert set(tmp_path.iterdir()) == before | {tmp_path / "plan.json"}


def test_native_approval_exact_binding_and_passive_event_idempotence(tmp_path, monkeypatch):
    from plugins.fleet_lifecycle.controller import Native, Halt, Plan
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    kb.init_db()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="implementation", assignee="builder")
        gate_id = kb.create_task(conn, title="separate operational admission gate", assignee="director")
        before = kb.get_task(conn, tid)
        data = plan_data(tmp_path, tid, gate_id)
        path = tmp_path / "plan.json"
        path.write_text(json.dumps(data))
        plan = Plan.load(path)
        raw = path.read_bytes()
        with pytest.raises(Halt):
            Native.approval(conn, plan)
        request_id, _ = gates.request_gate(conn, gate_id, stage="merge", sha=data["source_sha"],
            pr_url="https://github.com/example/controller/pull/1", tests=Native.approval_tests(plan),
            reviewer_verdict="APPROVE", requested_by="director")
        gates.decide_gate(conn, gate_id, stage="merge", decision="approve", sha=data["source_sha"],
                          actor="UOWNER", platform="cli")
        with pytest.raises(Halt):
            Native.approval(conn, plan)
        for actor, source, tests in (("UOTHER", data["source_sha"], Native.approval_tests(plan)),
                                     ("UOWNER", "d"*40, Native.approval_tests(plan)),
                                     ("UOWNER", data["source_sha"], "fleet-plan=" + "0"*64)):
            gates.request_gate(conn, gate_id, stage="merge", sha=source,
                pr_url="https://github.com/example/controller/pull/1", tests=tests,
                reviewer_verdict="APPROVE", requested_by="director")
            gates.decide_gate(conn, gate_id, stage="merge", decision="approve", sha=source,
                              actor=actor, platform="slack")
            with pytest.raises(Halt):
                Native.approval(conn, plan)
        request_id, _ = gates.request_gate(conn, gate_id, stage="merge", sha=data["source_sha"],
            pr_url="https://github.com/example/controller/pull/1", tests=Native.approval_tests(plan),
            reviewer_verdict="APPROVE", requested_by="director")
        gates.decide_gate(conn, gate_id, stage="merge", decision="approve", sha=data["source_sha"],
                          actor="UOWNER", platform="slack")
        assert Native.approval(conn, plan) == request_id
        assert Native.approval(conn, Plan.load(path)) == request_id
        assert path.read_bytes() == raw  # No future event ID inserted into the plan.
        changed = json.loads(raw)
        changed["commit"]["message"] = "Different approved scope"
        path.write_text(json.dumps(changed))
        with pytest.raises(Halt):
            Native.approval(conn, Plan.load(path))
        native = Native(plan)
        first = native.emit_on(conn, "fleet_controller_progress", "verify", "verification_passed")
        assert native.emit_on(conn, "fleet_controller_progress", "verify", "verification_passed") == first
        assert native.emit_on(conn, "fleet_controller_failed", "exact_review", "review_rejected") > first
        with pytest.raises(Halt):
            native.emit_on(conn, "fleet_controller_failed", "stdout secret", "review_rejected")
        after = kb.get_task(conn, tid)
        assert (after.status, after.claim_lock, after.current_run_id) == (before.status, before.claim_lock, before.current_run_id)


def test_durable_intent_restart_and_overlap(tmp_path):
    from plugins.fleet_lifecycle.controller import Ledger
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    with Ledger(root) as ledger:
        assert ledger is not None
        ledger.save("a"*64, {"stage":"verify", "intent":"verify"})
        with Ledger(root) as overlap:
            assert overlap is None
    with Ledger(root) as ledger:
        assert ledger.load("a"*64)["intent"] == "verify"


def test_junit_proof_distinguishes_assertion_from_collection_and_skip():
    from plugins.fleet_lifecycle.runtime import junit_proof
    assert junit_proof(b"<testsuite><testcase classname=\"tests.test_x\" name=\"test_regression\"/></testsuite>", False, "test_regression") == 1
    assert junit_proof(b"<testsuite><testcase name=\"test_regression\"><failure type=\"AssertionError\">AssertionError: changed</failure></testcase></testsuite>", True, "test_regression") == 1
    for xml in (b"<testsuite><testcase name=\"test_regression\"><error/></testcase></testsuite>",
                b"<testsuite><testcase name=\"test_regression\"><skipped/></testcase></testsuite>", b"<testsuite/>"):
        with pytest.raises(Exception):
            junit_proof(xml, True, "test_regression")


def test_verdict_binding_usage_and_findings():
    from plugins.fleet_lifecycle.reviewer import validate_verdict
    from plugins.fleet_lifecycle.controller import Halt
    binding = {"phase":"precommit_review", "base":"a"*40, "head":"a"*40,
               "diff_hash":"b"*64, "manifest_hash":"c"*64}
    assert validate_verdict({**binding, "verdict":"APPROVE", "findings":[]}, binding)["verdict"] == "APPROVE"
    for result in ({**binding, "verdict":"APPROVE", "findings":["fix it"]},
                   {**binding, "head":"d"*40, "verdict":"APPROVE", "findings":[]}):
        with pytest.raises(Halt):
            validate_verdict(result, binding)


def test_completion_safety_replayed_admission_preserves_allowance(tmp_path):
    """Explicit fake side effects: replay cannot require the now-dirty source base."""
    from contextlib import nullcontext
    from types import SimpleNamespace
    from plugins.fleet_lifecycle.controller import Controller, require

    class FakeRuntime:
        dirty = False
        def principal(self):
            pass
        def identity(self):
            pass
        def admission_snapshot(self):
            require(not self.dirty, "identity_drift")
        def action(self, *_args):
            raise AssertionError("admission replay must not execute an action")

    class FakeNative:
        admission_calls = 0
        def connect(self, *_args):
            return nullcontext(None)
        def task(self, *_args):
            return None
        def approval(self, *_args):
            return 27
        def admit(self, _runtime):
            self.admission_calls += 1
            return 31
        def emit(self, *_args):
            raise AssertionError("successful admission replay has no new alert")

    root = tmp_path / "replay-private"
    root.mkdir(mode=0o700)
    plan = SimpleNamespace(data={"state_root": str(root)}, digest="a" * 64)
    runtime, native = FakeRuntime(), FakeNative()
    controller = Controller(plan, runtime, native=native)
    original = controller.admit()
    assert original["stage"] == "waiting_source"
    runtime.dirty = True  # Builder may legitimately have edited since admission.
    replay = controller.admit()
    assert replay == original and native.admission_calls == 1


def test_completion_safety_mutant_pytest_raises_failure_is_valid():
    """Missing an expected ValueError is a genuine Pytest assertion kill, not collection error."""
    from plugins.fleet_lifecycle.runtime import junit_proof
    xml = (b'<testsuite><testcase classname="tests.test_cycle" name="test_regression">'
           b'<failure type="Failed" message="Failed: DID NOT RAISE &lt;class ValueError&gt;">'
           b'Failed: DID NOT RAISE &lt;class ValueError&gt;</failure></testcase></testsuite>')
    assert junit_proof(xml, True, "test_regression") == 1


def plan_data(tmp_path, task_id="t_12345678", gate_id="t_87654321"):
    from plugins.fleet_lifecycle.controller import digest
    profiles = {role: {"name": role, "home": str(tmp_path / role), "config_sha256": "a"*64}
                for role in ("director", "builder", "reviewer")}
    before, after = "raise ValueError('cycle')", "pass # mutation"
    return {"schema": 1, "board": "default", "database": str(tmp_path / "board.db"),
        "task_id": task_id, "project_id": "project", "workspace": str(tmp_path / "worktree"),
        "branch": "hermes-agent/fixture", "base": "a"*40, "contract_hash": "c"*64,
        "core": str(tmp_path / "core"), "source_sha": "b"*40,
        "candidate_paths": ["tests/test_cycle.py"], "test_files": ["tests/test_cycle.py"],
        "mutation": {"path": "production_guard.py", "base_sha256": "d"*64,
            "mutant_sha256": "e"*64, "before": before, "after": after,
            "before_sha256": digest(before.encode()), "after_sha256": digest(after.encode()),
            "test_file": "tests/test_cycle.py", "test_name": "test_regression"},
        "resources": {"image": "sha256:" + "a"*64, "seconds": 60, "cpus": 1, "memory_mb": 512, "pids": 64, "scratch_mb": 256},
        "profiles": profiles, "state_root": str(tmp_path / "director" / "fleet-lifecycle"),
        "native": {"launcher": str(tmp_path / "launcher"), "launcher_sha256": "f"*64,
            "launcher_entry": str(tmp_path / "launcher-entry"), "runtime_command_sha256": "f"*64,
            "client": str(tmp_path / "reviewer" / "native-client.py"), "client_sha256": "f"*64,
            "binary": str(tmp_path / "binary"), "binary_entry": str(tmp_path / "binary-entry"),
            "binary_sha256": "f"*64, "tools_root": str(tmp_path / "tools"),
            "model": "claude-sonnet-5", "provider": "fleet-claude-cli"},
        "slack": {"profile": "default", "channel": "CFIXTURE", "thread": "", "team": "TFIXTURE"},
        "gate": {"task_id": gate_id, "stage": "merge", "source_sha": "b"*40, "actor": "UOWNER"},
        "context": [{"path": "production_guard.py", "functions": []}],
        "commit": {"name": "Fixture", "email": "fixture@example.invalid", "message": "Add cycle regression"}}


def test_plan_separates_base_mutation_from_new_test_and_rejects_future_id(tmp_path):
    from plugins.fleet_lifecycle.controller import Plan, Halt
    p = plan_data(tmp_path)
    Plan.validate(p)
    for change in ({"path": p["test_files"][0]}, {"base_sha256": "bad"}):
        bad = json.loads(json.dumps(p)); bad["mutation"].update(change)
        with pytest.raises(Halt): Plan.validate(bad)
    p["gate"]["request_event_id"] = 1
    with pytest.raises(Halt): Plan.validate(p)


def test_principal_precedes_all_state_writes(tmp_path):
    from types import SimpleNamespace
    from plugins.fleet_lifecycle.controller import Controller, Halt
    class WrongPrincipal:
        def principal(self): raise Halt("identity_drift")
    root = tmp_path / "absent"
    controller = Controller(SimpleNamespace(data={"state_root": str(root)}, digest="a"*64), WrongPrincipal())
    for action in (controller.admit, controller.tick):
        with pytest.raises(Halt): action()
        assert not root.exists()


@pytest.mark.parametrize("failure", [
    b'<failure type="Failed">Failed: explicit failure</failure>',
    b'<failure type="RuntimeError">RuntimeError: unexpected</failure>',
    b'<failure type="Failed">Failed: DID NOT RAISE &lt;class TypeError&gt;</failure>',
])
def test_junit_rejects_generic_or_wrong_expectation_failure(failure):
    from plugins.fleet_lifecycle.controller import Halt
    from plugins.fleet_lifecycle.runtime import junit_proof
    with pytest.raises(Halt):
        junit_proof(b'<testsuite><testcase name="test_regression">'+failure+b'</testcase></testsuite>', True, "test_regression")


def test_interrupted_admission_does_not_readmit(tmp_path):
    from types import SimpleNamespace
    from plugins.fleet_lifecycle.controller import Controller, Ledger
    root = tmp_path / "private"; root.mkdir(mode=0o700)
    class Runtime:
        def principal(self): pass
        def cleanup(self, state): state["cleaned"] = True
    class Native:
        def emit(self, event, state): return 99
        def admit(self, runtime): raise AssertionError("must never redo uncertain board write")
    plan = SimpleNamespace(data={"state_root": str(root)}, digest="a"*64)
    with Ledger(root) as ledger:
        ledger.save(plan.digest, {"stage": "admit", "intent": "admit", "attempts": [], "outbox": []})
    controller = Controller(plan, Runtime(), Native())
    state = controller.admit()
    assert state["stage"] == "halted" and state["reason_code"] == "interrupted_action"
    assert state["cleaned"] and state["events"] == [99]
    assert controller.admit() == state


from tests.hermes_cli.test_kanban_fleet_contract_guard import (
    fleet_home, _role_contract, _create_fleet_task,
)


@pytest.fixture
def native_lifecycle(fleet_home, tmp_path):
    """Real registered schema-2 board/kernel/gates; explicit fake external effects."""
    from plugins.fleet_lifecycle.controller import Controller, Native, Plan, canonical, Halt
    from hermes_cli import kanban_db_notify as notify
    home, project_id = fleet_home
    data = plan_data(tmp_path)
    data["project_id"] = project_id
    scope = "fleet-lifecycle-test-only-v1 " + canonical({key: data[key] for key in
        ("base", "candidate_paths", "test_files", "mutation")}).decode()
    contract = _role_contract(execution_mode="implement", description=scope)
    data["contract_hash"] = contract["contract_sha256"]
    with kbc.connect() as conn:
        tid = _create_fleet_task(conn, project_id, contract, initial_status="blocked")
        gate_id = kb.create_task(conn, title="Operational controller authorization", assignee="director")
        task = kb.get_task(conn, tid)
        data.update(task_id=tid, workspace=task.workspace_path, branch=task.branch_name,
                    database=str(kb.kanban_db_path(board="default")))
        data["gate"]["task_id"] = gate_id
        notify.add_notify_sub(conn, task_id=tid, platform="slack", chat_id="CFIXTURE",
            notifier_profile="default", delivery_mode="notify", delivery_metadata={"slack_team_id": "TFIXTURE"})
    path = tmp_path / "native-plan.json"; path.write_text(json.dumps(data)); plan = Plan.load(path)
    with kbc.connect() as conn:
        request_id, _ = gates.request_gate(conn, gate_id, stage="merge", sha=data["source_sha"],
            pr_url="https://github.com/example/controller/pull/1", tests=Native.approval_tests(plan),
            reviewer_verdict="APPROVE", requested_by="director")
        gates.decide_gate(conn, gate_id, stage="merge", decision="approve", sha=data["source_sha"],
                          actor="UOWNER", platform="slack")
    root = Path(data["state_root"]); root.parent.mkdir(mode=0o700, exist_ok=True)
    class ExplicitFakeBackend:
        fail = None
        calls = None
        def __init__(self): self.calls = []
        def principal(self): pass
        def identity(self): pass
        def admission_snapshot(self): pass
        def snapshot(self): return {"manifest": [], "manifest_hash": "d"*64, "diff_hash": "e"*64}
        def unchanged(self, *_args): pass
        def cleanup(self, _state): pass
        def action(self, phase, state, native):
            self.calls.append(phase)
            if phase == self.fail: raise Halt("review_rejected" if "review" in phase else "verification_failed")
            if phase == "verify": return [{"explicit_fake": True, "collected": 1}]
            if phase == "commit": return "f"*40
            if phase == "request_push": return native.request_push(state)
            return {"verdict": "APPROVE", "findings": []}
    backend = ExplicitFakeBackend()
    controller = Controller(plan, backend)
    return controller, backend, tid, request_id


from pathlib import Path


def native_builder_handoff(tid):
    with kbc.connect() as conn:
        assert kb.unblock_task(conn, tid)  # Explicit fixture action, never Controller.admit.
        claimed = kb.claim_task(conn, tid, claimer=kb._host_prefix() + "fixture-builder")
        assert claimed is not None and claimed.current_run_id is not None
        assert kb.request_review(conn, tid, reviewer="reviewer", summary="new regression supplied",
                                 expected_run_id=claimed.current_run_id)
        return claimed.current_run_id


def test_real_native_full_lifecycle_with_explicit_fake_side_effects(native_lifecycle):
    from plugins.fleet_lifecycle.controller import Ledger
    controller, backend, tid, auth = native_lifecycle
    admitted = controller.admit()
    assert admitted["authorization_event"] == auth and admitted["stage"] == "waiting_source"
    assert controller.admit() == admitted
    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "blocked" and task.current_run_id is None
    run_id = native_builder_handoff(tid)
    assert controller.tick()["stage"] == "verify"
    stages = ["precommit_review", "commit", "exact_review", "request_push", "awaiting_push"]
    for expected in stages: assert controller.tick()["stage"] == expected
    final = controller.tick()
    assert backend.calls == ["verify", "precommit_review", "commit", "exact_review", "request_push"]
    assert final["attempts"] == ["precommit_review", "exact_review"]
    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "review" and task.current_run_id is None
        run = conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()
        assert run["outcome"] == "review_requested" and run["claim_lock"]
        assert len(conn.execute("SELECT id FROM task_events WHERE task_id=? AND kind='fleet_controller_admitted'", (tid,)).fetchall()) == 1
        gate = gates.latest_gate(conn, tid, "push")
        assert gate["request"]["sha"] == "f"*40 and gate["decision"] is None
        assert final["gate_event"] == gate["request_event_id"]
    assert controller.admit() == final  # No second review, commit, or gate request.


@pytest.mark.parametrize("failure", ["verify", "precommit_review", "exact_review"])
def test_native_failed_stage_has_no_later_commit_or_gate(native_lifecycle, failure):
    controller, backend, tid, _auth = native_lifecycle
    controller.admit(); native_builder_handoff(tid); controller.tick()
    backend.fail = failure
    for _ in range(5):
        state = controller.tick()
        if state["stage"] == "halted": break
    assert state["stage"] == "halted"
    assert backend.calls[-1] == failure
    if failure != "exact_review": assert "commit" not in backend.calls
    calls = list(backend.calls)
    assert controller.tick() == state and controller.admit() == state
    assert backend.calls == calls and len(state["attempts"]) <= 2
    with kbc.connect() as conn: assert gates.latest_gate(conn, tid, "push") is None


def test_closed_native_run_with_live_retained_worker_is_not_handoff(native_lifecycle, monkeypatch):
    from hermes_cli import kanban_db_dispatch as dispatch
    controller, backend, tid, _ = native_lifecycle
    controller.admit(); run_id = native_builder_handoff(tid)
    with kbc.connect() as conn, kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET worker_pid=1234,worker_started_at='unverified' WHERE id=?", (run_id,))
    monkeypatch.setattr(dispatch, "_worker_alive", lambda pid, fingerprint: pid == 1234)
    state = controller.tick()
    assert state["stage"] == "halted" and not backend.calls


def test_frozen_body_drift_halts_before_verification(native_lifecycle):
    controller, backend, tid, _ = native_lifecycle
    controller.admit(); native_builder_handoff(tid)
    with kbc.connect() as conn, kb.write_txn(conn):
        conn.execute("UPDATE tasks SET body=body || ' changed prose' WHERE id=?", (tid,))
    assert controller.tick()["stage"] == "halted" and not backend.calls


def test_git_adapter_new_test_only_snapshot_commit_and_baseline_mutation(tmp_path):
    from hermes_platform.resolver import locate_command
    from plugins.fleet_lifecycle.controller import Plan, Halt, digest
    from plugins.fleet_lifecycle.runtime import Git, Runtime
    repo = tmp_path / "repo"; repo.mkdir()
    env = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_COUNT": "0", "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid"}
    git = Git(locate_command("git").command[0], repo, env)
    data = plan_data(tmp_path); data["workspace"] = str(repo)
    git.call("init", "-b", data["branch"])
    production = repo / "production_guard.py"
    raw = b"def guard():\n    raise ValueError('cycle')\n"
    production.write_bytes(raw)
    git.call("add", "--", "production_guard.py"); git.call("commit", "-m", "Fixture baseline")
    data["base"] = git.call("rev-parse", "HEAD").decode().strip()
    data["mutation"]["base_sha256"] = digest(raw)
    data["mutation"]["mutant_sha256"] = digest(raw.replace(data["mutation"]["before"].encode(), data["mutation"]["after"].encode()))
    runtime = Runtime.__new__(Runtime); runtime.p = data; runtime.git = git
    runtime.admission_snapshot()
    test = repo / "tests/test_cycle.py"; test.parent.mkdir(); test.write_text("def test_regression():\n    assert True\n")
    snapshot = runtime.snapshot()
    assert [item["path"] for item in snapshot["manifest"]] == data["candidate_paths"]
    assert production.read_bytes() == raw
    production.write_bytes(raw + b"# forbidden production edit\n")
    with pytest.raises(Halt): runtime.snapshot()
    production.write_bytes(raw)
    test.chmod(0o755)
    with pytest.raises(Halt): runtime.snapshot()
    test.chmod(0o644)
    head = git.commit(data, snapshot)
    assert head != data["base"] and production.read_bytes() == raw
    assert git.call("show", head+":tests/test_cycle.py") == test.read_bytes()


def test_owned_unit_cleanup_refuses_foreign_description(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from plugins.fleet_lifecycle.controller import Halt
    from plugins.fleet_lifecycle.reviewer import stop_owned_unit
    from plugins.fleet_lifecycle import runtime
    calls = []
    resource = {"kind": "review_unit", "name": "hermes-fleet-review-" + "a"*16 + "-" + "b"*32 + ".service",
                "description": "hermes-fleet:" + "a"*64 + ":" + "b"*32}
    rt = SimpleNamespace(plan=SimpleNamespace(digest="a"*64), env={}, systemctl="/fixture/systemctl")
    def fake_run(argv, **kwargs):
        calls.append(argv)
        return runtime.Result(0, b"LoadState=loaded\nActiveState=active\nDescription=foreign\n", b"")
    monkeypatch.setattr(runtime, "run", fake_run)
    with pytest.raises(Halt): stop_owned_unit(rt, resource)
    assert len(calls) == 1 and "stop" not in calls[0]


@pytest.fixture
def recording_native_facade():
    """Small source-ABI double; all completions/usage are explicit test data."""
    from types import SimpleNamespace
    calls = []

    class RecordingFacade:
        allowed = set()
        required = False
        response = None
        payload = None
        configuration_error = None

        def __init__(self, *, command, args=(), base_url="cli://fleet-claude-code",
                     api_key=None, timeout=900.0, default_headers=None, max_retries=0):
            calls.append(("ctor", dict(command=command, args=args, base_url=base_url,
                api_key=api_key, timeout=timeout, default_headers=default_headers,
                max_retries=max_retries)))
            self._configuration_error = self.configuration_error
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

        @staticmethod
        def _tool_policy(tools, choice):
            calls.append(("policy", tools, choice))
            return RecordingFacade.allowed, RecordingFacade.required, "test-only tool-free policy"

        @staticmethod
        def _prompt(messages, section):
            calls.append(("prompt", messages, section))
            RecordingFacade.payload = json.dumps({"messages": messages,
                "tool_policy": section}, ensure_ascii=True, allow_nan=False).encode()
            return RecordingFacade.payload

        def create(self, **kwargs):
            calls.append(("completion", kwargs))
            return self.response

        def close(self):
            calls.append(("close",))

    return SimpleNamespace(FleetClaudeCLIClient=RecordingFacade, calls=calls,
        _MODEL="claude-sonnet-5", _COMMAND="/unit-fixture/never-execute",
        _BASE_URL="cli://fleet-claude-code", _MAX_REQUEST=1024*1024, _MAX_SECONDS=900.0)


def test_native_facade_serialization_interface_without_inference(recording_native_facade):
    from plugins.fleet_lifecycle.reviewer import prepare_native_request
    module = recording_native_facade
    messages = [{"role": "system", "content": "unit review policy"},
                {"role": "user", "content": "review data"}]
    request = prepare_native_request(module, messages, model=module._MODEL,
                                     command=module._COMMAND, timeout=60)
    assert request == dict(model="claude-sonnet-5", messages=messages,
                           tools=[], stream=False, timeout=60)
    assert request["messages"] is messages
    assert 0 < len(module.FleetClaudeCLIClient.payload) <= module._MAX_REQUEST
    assert module.calls[0] == ("policy", [], None)
    assert module.calls[1][0] == "prompt" and module.calls[1][1] is messages
    assert [c[0] for c in module.calls] == ["policy", "prompt"]


@pytest.mark.parametrize("failure", ["oversize", "model", "command", "allowed", "required",
    "request_limit", "seconds_limit", "base_url", "empty_payload",
    "model_constant", "command_constant"])
def test_native_request_preflight_rejects_before_completion(recording_native_facade, failure):
    from plugins.fleet_lifecycle.controller import Halt
    from plugins.fleet_lifecycle.reviewer import prepare_native_request
    module = recording_native_facade
    messages = [{"role": "user", "content": "unit data"}]
    model, command = module._MODEL, module._COMMAND
    if failure == "oversize": messages[0]["content"] = "x" * module._MAX_REQUEST
    if failure == "model": model = "unapproved-model"
    if failure == "command": command = "/unapproved-command"
    if failure == "model_constant": module._MODEL = "unapproved-model"
    if failure == "command_constant": module._COMMAND = "/unapproved-command"
    if failure == "allowed": module.FleetClaudeCLIClient.allowed = {"terminal"}
    if failure == "required": module.FleetClaudeCLIClient.required = True
    if failure == "request_limit": module._MAX_REQUEST -= 1
    if failure == "seconds_limit": module._MAX_SECONDS += 1
    if failure == "base_url": module._BASE_URL = "https://not-a-transport.invalid"
    if failure == "empty_payload": module.FleetClaudeCLIClient._prompt = lambda *_: b""
    with pytest.raises(Halt):
        prepare_native_request(module, messages, model=model, command=command, timeout=60)
    assert not any(c[0] in {"ctor", "completion"} for c in module.calls)


@pytest.mark.parametrize("timeout", [0, -1, True, None, "60", float("inf"),
    float("nan"), 901, 10**1000])
def test_native_request_invalid_timeout_is_pre_ipc(recording_native_facade, timeout):
    from plugins.fleet_lifecycle.controller import Halt
    from plugins.fleet_lifecycle.reviewer import prepare_native_request
    module = recording_native_facade
    with pytest.raises(Halt):
        prepare_native_request(module, [{"role": "user", "content": "unit data"}],
            model=module._MODEL, command=module._COMMAND, timeout=timeout)
    assert module.calls == []


@pytest.fixture
def embedded_native_helper(tmp_path, monkeypatch, recording_native_facade):
    """Run the real embedded helper only against a hash-bound FAKE native module.

    No native implementation, inference, auth store, subprocess or network is used.
    """
    import io
    import signal
    import socket
    import subprocess
    import sys
    from types import ModuleType, SimpleNamespace
    import hermes_constants
    from plugins.fleet_lifecycle import reviewer
    from plugins.fleet_lifecycle.controller import canonical, digest
    module = recording_native_facade
    p = plan_data(tmp_path)
    home = Path(p["profiles"]["reviewer"]["home"]); home.mkdir()
    owner = tmp_path / "unit-os-home"; owner.mkdir()
    config = home / "config.yaml"
    config.write_text(json.dumps({"model": {"provider": "fleet-claude-cli",
                                           "default": "claude-sonnet-5"}}))
    p["profiles"]["reviewer"]["config_sha256"] = digest(config.read_bytes())
    native = p["native"]
    for key in ("binary", "launcher"):
        path = Path(native[key]); path.write_bytes(b"unit fixture; NEVER execute")
        native[key + "_sha256"] = digest(path.read_bytes())
        native[key + "_entry"] = str(path)
    module._COMMAND = native["binary_entry"]
    bridge = ModuleType("fleet_native_unit_double")
    bridge.RecordingFacade = module.FleetClaudeCLIClient
    monkeypatch.setitem(sys.modules, bridge.__name__, bridge)
    fake_source = ("# Explicit test double, not the installed native client.\n"
        "from fleet_native_unit_double import RecordingFacade as FleetClaudeCLIClient\n"
        + "\n".join(key + " = " + repr(getattr(module, key)) for key in
                      ("_COMMAND", "_MODEL", "_BASE_URL", "_MAX_REQUEST", "_MAX_SECONDS")))
    Path(native["client"]).write_text(fake_source)
    native["client_sha256"] = digest(fake_source.encode())
    binding = {"phase": "precommit_review", "base": "a"*40, "head": "a"*40,
               "diff_hash": "b"*64, "manifest_hash": "c"*64}
    verdict = {**binding, "verdict": "APPROVE", "findings": []}
    # These numbers are fabricated UNIT DATA, never evidence of native usage.
    module.FleetClaudeCLIClient.response = SimpleNamespace(model="claude-sonnet-5",
        choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[], content=json.dumps(verdict)))],
        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=3, total_tokens=5))
    envelope = {"binding": binding, "unit_only_artifact": "untrusted unit source data"}
    cwd = tmp_path / "review-cwd"; cwd.mkdir(); monkeypatch.chdir(cwd)
    monkeypatch.setenv("HERMES_HOME", str(home)); monkeypatch.setenv("HOME", str(owner))
    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: home)
    monkeypatch.setattr(reviewer.pwd, "getpwuid", lambda _uid: SimpleNamespace(pw_dir=str(owner)))
    monkeypatch.setattr(signal, "signal", lambda *_: None)
    def forbidden(*_args, **_kwargs):
        raise AssertionError("unit helper must never start IPC")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    def invoke():
        monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(
            canonical({"plan": p, "envelope": envelope}))))
        exec(reviewer._HELPER, {"__name__": "__unit_native_helper__"})
    return SimpleNamespace(plan=p, envelope=envelope, facade=module.FleetClaudeCLIClient,
                           calls=module.calls, invoke=invoke, home=home, config=config)


def test_embedded_helper_constructs_preflights_completes_and_closes(embedded_native_helper, capsys, monkeypatch):
    from plugins.fleet_lifecycle import reviewer
    fixture = embedded_native_helper
    prepared = []
    real_prepare = reviewer.prepare_native_request
    def record_prepare(*args, **kwargs):
        request = real_prepare(*args, **kwargs)
        prepared.append(request)
        return request
    monkeypatch.setattr(reviewer, "prepare_native_request", record_prepare)
    fixture.invoke()
    result = json.loads(capsys.readouterr().out)
    assert result["verdict"]["verdict"] == "APPROVE"
    assert result["usage"] == dict(prompt_tokens=2, completion_tokens=3, total_tokens=5)
    assert len(prepared) == 1
    calls = fixture.calls
    assert [c[0] for c in calls] == ["policy", "prompt", "ctor", "completion", "close"]
    assert calls[2][1] == dict(command=fixture.plan["native"]["binary_entry"], args=(),
        base_url="cli://fleet-claude-code", api_key=None, timeout=60, default_headers=None, max_retries=0)
    request = calls[3][1]
    assert request == prepared[0] and request["messages"] is calls[1][1]
    assert set(request) == {"model", "messages", "tools", "stream", "timeout"}
    assert request["tools"] == [] and request["stream"] is False
    assert request["model"] == "claude-sonnet-5" and request["timeout"] == 60
    assert json.loads(request["messages"][1]["content"]) == fixture.envelope


@pytest.mark.parametrize("failure", ["client_hash", "binary_hash", "launcher_hash",
    "binary_entry", "launcher_entry", "profile_hash", "home", "hermes_home",
    "resolved_home", "provider", "model", "fallback", "fallback_providers",
    "model_fallback", "config_provider", "config_model", "timeout", "policy", "oversize"])
def test_embedded_helper_fails_closed_before_completion(embedded_native_helper, failure, monkeypatch, capsys):
    import hermes_constants
    from plugins.fleet_lifecycle.controller import digest
    fixture = embedded_native_helper
    p = fixture.plan
    if failure.endswith("_hash") and failure != "profile_hash":
        p["native"][failure[:-5] + "_sha256"] = "0" * 64
    if failure in {"binary_entry", "launcher_entry"}:
        p["native"][failure] = p["native"]["client"]
    if failure == "profile_hash": p["profiles"]["reviewer"]["config_sha256"] = "0" * 64
    if failure == "home": monkeypatch.setenv("HOME", str(fixture.home))
    if failure == "hermes_home": monkeypatch.setenv("HERMES_HOME", str(fixture.home.parent))
    if failure == "resolved_home":
        monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: fixture.home.parent)
    if failure == "provider": p["native"]["provider"] = "not-approved"
    if failure == "model": p["native"]["model"] = "not-approved"
    if failure in {"fallback", "fallback_providers", "model_fallback", "config_provider", "config_model"}:
        config = json.loads(fixture.config.read_text())
        if failure == "fallback": config["fallback_model"] = "not-approved"
        if failure == "fallback_providers": config["fallback_providers"] = ["not-approved"]
        if failure == "model_fallback": config["model"]["fallback"] = "not-approved"
        if failure == "config_provider": config["model"]["provider"] = "not-approved"
        if failure == "config_model": config["model"]["default"] = "not-approved"
        fixture.config.write_text(json.dumps(config))
        p["profiles"]["reviewer"]["config_sha256"] = digest(fixture.config.read_bytes())
    if failure == "timeout": p["resources"]["seconds"] = 0
    if failure == "policy": fixture.facade.allowed = {"terminal"}
    if failure == "oversize":
        fixture.facade._prompt = lambda *_: b"x" * (1024*1024+1)
    with pytest.raises(SystemExit) as error:
        fixture.invoke()
    assert error.value.code == 1
    assert json.loads(capsys.readouterr().out) == {"error": "review_failed"}
    assert not any(c[0] in {"ctor", "completion"} for c in fixture.calls)


def test_embedded_helper_loads_verified_bytes_not_mutable_filename(embedded_native_helper, monkeypatch, capsys):
    from plugins.fleet_lifecycle import controller
    fixture = embedded_native_helper
    real_read = controller.read_regular
    client_path = Path(fixture.plan["native"]["client"])
    reads = []
    def replace_after_read(path, *args, **kwargs):
        raw = real_read(path, *args, **kwargs)
        if Path(path) == client_path:
            reads.append(path)
            client_path.write_text("raise AssertionError(\"unverified filename reload\")\n")
        return raw
    monkeypatch.setattr(controller, "read_regular", replace_after_read)
    fixture.invoke()
    assert len(reads) == 1
    assert json.loads(capsys.readouterr().out)["verdict"]["verdict"] == "APPROVE"
    assert [c[0] for c in fixture.calls] == ["policy", "prompt", "ctor", "completion", "close"]


def test_embedded_helper_closes_rejected_constructor_without_completion(embedded_native_helper, capsys):
    fixture = embedded_native_helper
    fixture.facade.configuration_error = "unit-only unsupported configuration"
    with pytest.raises(SystemExit): fixture.invoke()
    assert json.loads(capsys.readouterr().out) == {"error": "review_failed"}
    assert [c[0] for c in fixture.calls] == ["policy", "prompt", "ctor", "close"]


@pytest.mark.parametrize("failure", ["usage", "binding", "findings", "tools", "completion"])
def test_embedded_helper_closes_on_invalid_response(embedded_native_helper, failure, capsys):
    fixture = embedded_native_helper
    response = fixture.facade.response
    if failure == "usage": response.usage.prompt_tokens = 0
    if failure in {"binding", "findings"}:
        verdict = json.loads(response.choices[0].message.content)
        if failure == "binding": verdict["head"] = "d"*40
        else: verdict["findings"] = ["unit issue"]
        response.choices[0].message.content = json.dumps(verdict)
    if failure == "tools": response.choices[0].message.tool_calls = ["forbidden unit tool"]
    if failure == "completion": fixture.facade.response = None
    with pytest.raises(SystemExit): fixture.invoke()
    assert json.loads(capsys.readouterr().out) == {"error": "review_failed"}
    assert [c[0] for c in fixture.calls][-3:] == ["ctor", "completion", "close"]


def test_docker_adapter_uses_bounded_tmpfs_owned_cleanup_and_real_receipt_validation(tmp_path, monkeypatch):
    import base64, hashlib, io, tarfile
    from types import SimpleNamespace
    from plugins.fleet_lifecycle import runtime as module
    from plugins.fleet_lifecycle.controller import canonical, Halt
    data = plan_data(tmp_path)
    root = Path(data["state_root"]); root.mkdir(mode=0o700, parents=True)
    raw = b"def guard(): pass\n"
    blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        info = tarfile.TarInfo("production_guard.py"); info.mode = 0o644; info.size = len(raw)
        archive.addfile(info, io.BytesIO(raw))
    class FakeGitObjects:
        def call(self, *args, **kwargs):
            if args[0] == "archive": return stream.getvalue()
            assert args[0] == "ls-tree"
            return ("100644 blob " + blob + "\tproduction_guard.py\0").encode()
    rt = module.Runtime.__new__(module.Runtime)
    rt.plan = SimpleNamespace(data=data, digest="a"*64); rt.p = data
    rt.git = FakeGitObjects(); rt.docker = "/fixture/docker"; rt.env = {}
    rt.generation = tmp_path / "generation"; rt.test_python = rt.generation / "venv/bin/python"
    state = {"snapshot": {"manifest": [], "manifest_hash": "b"*64}}
    persisted, calls = [], []
    rt.persist = lambda: persisted.append(json.loads(json.dumps(state)))
    active = set()
    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "run":
            name = argv[argv.index("--name")+1]; active.add(name)
            assert persisted[-1]["owned"][0]["name"] == name
            assert "--network=none" in argv and "--read-only" in argv and "--cap-drop=ALL" in argv
            assert "--security-opt=no-new-privileges" in argv and "--pids-limit" in argv
            assert any(a.startswith("/var/tmp:rw,nosuid,nodev,size=256m") for a in argv)
            assert all("docker.sock" not in a for a in argv)
            # Match exact mount destinations, not the /var/tmp prefix of this
            # test's own readonly dependency-generation fixture paths.
            mounts = [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "--mount"]
            expected = {str(rt.generation), data["native"]["tools_root"]}
            assert len(mounts) == len(expected)
            seen = set()
            for mount in mounts:
                options = dict(part.split("=", 1) for part in mount.split(",") if "=" in part)
                assert options.get("type") == "bind" and "readonly" in mount.split(",")
                assert options.get("src") == options.get("dst")
                assert options["dst"] != "/var/tmp" and options["dst"] in expected
                seen.add(options["dst"])
            assert seen == expected
            assert kwargs["stdin"] and kwargs["input_limit"] == 132*1024*1024
            xml = b'<testsuite><testcase classname="tests.test_cycle" name="test_regression"/></testsuite>'
            packet = {key: base64.b64encode(value).decode() for key,value in
                      (("junit",xml),("stdout",b"recorded baseline"),("stderr",b""))}
            return module.Result(0, canonical(packet), b"")
        if argv[1] == "inspect":
            return module.Result(0, canonical({"hermes.fleet.action": state["owned"][0]["label"]}), b"")
        if argv[1] == "rm":
            assert argv[-1] in active; active.remove(argv[-1]); return module.Result(0,b"",b"")
        assert argv[1:3] == ["container","ls"]
        return module.Result(0,"\n".join(active).encode(),b"")
    monkeypatch.setattr(module, "run", fake_run)
    monkeypatch.setattr(module.os, "getuid", lambda: 1000)
    receipt = module.DockerVerifier(rt).invocation(state, "tests/test_cycle.py", False)
    assert receipt["collected"] == 1 and receipt["scratch"] == "bounded_tmpfs"
    assert not active and not state["owned"]
    assert (root / ("action-"+receipt["action"]) / "junit.xml").is_file()
    assert not list(root.rglob("base.tar"))  # No aggregate host source scratch.


def test_reviewer_dispatch_preserves_interpreter_flags_and_complete_bootstrap():
    from plugins.fleet_lifecycle.reviewer import replace_dispatch
    from plugins.fleet_lifecycle.controller import Halt
    bootstrap = "import runpy; marker='owning dependency bootstrap'; runpy.run_module('hermes_cli.main', run_name='__main__', alter_sys=True)"
    argv = ["/owning/python", "-I", "-B", "-c", bootstrap]
    result = replace_dispatch(argv, "pass")
    assert result[:-1] == argv[:-1]
    assert result[-1].startswith("import runpy; marker='owning dependency bootstrap'; ")
    assert "hermes_cli.main" not in result[-1]
    with pytest.raises(Halt): replace_dispatch(argv[:-1]+[bootstrap+"; print('extra')"], "pass")


def test_unit_cleanup_stops_only_persisted_owned_unit(monkeypatch):
    from types import SimpleNamespace
    from plugins.fleet_lifecycle import runtime
    from plugins.fleet_lifecycle.reviewer import stop_owned_unit
    resource = {"name": "hermes-fleet-review-"+"a"*16+"-"+"b"*32+".service",
                "description": "hermes-fleet:"+"a"*64+":"+"b"*32}
    rt = SimpleNamespace(plan=SimpleNamespace(digest="a"*64),env={},systemctl="/fixture/systemctl")
    calls = []; active = [True]
    def fake_run(argv, **kwargs):
        calls.append(argv)
        if "stop" in argv:
            assert argv[-1] == resource["name"]; active[0] = False
            return runtime.Result(0,b"",b"")
        text = "LoadState=loaded\nDescription="+resource["description"]+"\nActiveState="+("active" if active[0] else "inactive")+"\n"
        return runtime.Result(0,text.encode(),b"")
    monkeypatch.setattr(runtime,"run",fake_run)
    stop_owned_unit(rt,resource)
    assert [a[2] for a in calls] == ["show","stop","show"]

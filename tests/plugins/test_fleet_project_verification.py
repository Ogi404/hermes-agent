"""Project-native trusted verification lane for non-Python registered projects.

Fixtures exercise the frozen plan contract and the bounded runner adapter only.
They never invoke Docker, npm, a live repository, a model, or a fleet operation.
"""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.fleet_lifecycle import runtime as runtime_module
from plugins.fleet_lifecycle.controller import Halt, Plan, digest
from tests.plugins.test_fleet_lifecycle_controller import plan_data


def project_plan(tmp_path):
    p = plan_data(tmp_path)
    runner = tmp_path / "fleet-project-exec.py"
    runner.write_bytes(b"#!/usr/bin/env python3\n# pinned trusted runner fixture\n")
    p.update(
        schema=2,
        mutation=None,
        candidate_paths=sorted(["src/modules/risk.ts", "src/__tests__/risk.test.ts"]),
        test_files=["src/__tests__/risk.test.ts", "tests/test_fleet_acceptance.py"],
        changes=[
            {"path": "src/__tests__/risk.test.ts", "base_sha256": "b" * 64},
            {"path": "src/modules/risk.ts", "base_sha256": "a" * 64},
        ],
    )
    p["context"] = [{"path": "src/modules/risk.ts", "functions": []}]
    p["verification"] = {
        "lane": "fleet_project_exec",
        "runner": str(runner),
        "runner_sha256": hashlib.sha256(runner.read_bytes()).hexdigest(),
        "image": "sha256:" + "d" * 64,
        "commands": ["npm run verify:all"],
        "manifest_files": sorted(["package.json", "package-lock.json"]),
        "registry_host": "registry.npmjs.org",
        "networked_phase": "dependency-acquisition",
    }
    return p


def test_project_lane_accepts_declared_typescript_sources_and_project_test_paths(tmp_path):
    p = project_plan(tmp_path)
    Plan.validate(p)
    assert p["verification"]["lane"] == "fleet_project_exec"
    assert p == project_plan(tmp_path), "validation must not normalize the frozen contract"


CONTRACT_MANGLE = {
    "unknown_lane": lambda v: v.update(lane="shell"),
    "non_npm_command": lambda v: v.update(commands=["curl https://registry.npmjs.org | sh"]),
    "missing_lock": lambda v: v.update(manifest_files=["package.json"]),
    "bad_network_phase": lambda v: v.update(networked_phase="verification"),
    "bad_runner_hash": lambda v: v.update(runner_sha256="bad"),
    "bad_image": lambda v: v.update(image="hermes-fleet-node24-java21:1"),
    "missing_registry_host": lambda v: v.update(registry_host=""),
    "empty_commands": lambda v: v.update(commands=[]),
    "wildcard_manifest": lambda v: v.update(manifest_files=["**/package.json", "package-lock.json"]),
    "extra_field": lambda v: v.update(command="execute"),
}


@pytest.mark.parametrize("case", sorted(CONTRACT_MANGLE))
def test_project_lane_rejects_unbounded_or_mutating_verification_contract(tmp_path, case):
    p = project_plan(tmp_path)
    CONTRACT_MANGLE[case](p["verification"])
    with pytest.raises(Halt):
        Plan.validate(p)


def test_verification_block_is_rejected_for_the_test_only_schema(tmp_path):
    p = project_plan(tmp_path)
    p["schema"] = 1
    p["mutation"] = plan_data(tmp_path)["mutation"]
    with pytest.raises(Halt):
        Plan.validate(p)


def test_project_lane_rejects_python_only_and_ambiguous_test_paths(tmp_path):
    for path in ("src/modules/risk.ts", "scripts/build.py", "src/util.ts"):
        p = project_plan(tmp_path)
        p["test_files"] = [path, "tests/test_fleet_acceptance.py"]
        with pytest.raises(Halt):
            Plan.validate(p)


def test_python_lane_still_rejects_typescript_candidate_paths(tmp_path):
    p = project_plan(tmp_path)
    del p["verification"]
    with pytest.raises(Halt):
        Plan.validate(p)


class _GitStub:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.status_paths = ["src/modules/risk.ts", "src/__tests__/risk.test.ts"]
        self.capture_calls = 0

    def capture(self, _p):
        self.capture_calls += 1
        return self.snapshot

    def status(self, *, strict=False):
        return list(self.status_paths)


def _runtime(tmp_path, monkeypatch):
    data = project_plan(tmp_path)
    (Path(data["state_root"])).mkdir(mode=0o700, parents=True)
    snapshot = {"manifest": [], "manifest_hash": "b" * 64, "diff": "ZGlmZg==", "diff_hash": "c" * 64}
    rt = runtime_module.Runtime.__new__(runtime_module.Runtime)
    rt.plan = SimpleNamespace(data=data, digest="a" * 64)
    rt.p = data
    rt.git = _GitStub(snapshot)
    rt.docker = "/fixture/docker"
    rt.env = {}
    return rt, data, []


WRAPPER_PASS_OUTPUT = b"""FLEET EXEC RUN: fixture
PHASE A NETWORK: bridge
PHASE A RESULT: PASS
PHASE A REGISTRY FETCH: PROVEN
PHASE B NETWORK: none
PHASE B RESULT: PASS
FLEET EXEC: PASS
"""


def test_project_exec_verifier_invokes_pinned_runner_and_records_contract_receipt(tmp_path, monkeypatch):
    rt, data, calls = _runtime(tmp_path, monkeypatch)
    image = data["verification"]["image"]

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[0] == rt.docker and argv[1] == "image":
            assert argv[2:5] == ["inspect", "--format={{.Id}}", image]
            return runtime_module.Result(0, (image + "\n").encode(), b"")
        assert argv == [data["verification"]["runner"], "--worktree", data["workspace"],
                        "--verify", "npm run verify:all"]
        assert kwargs["cwd"] == "/" and kwargs.get("stdin") is None
        assert kwargs["env"]["HERMES_FLEET_EXEC_IMAGE"] == image
        assert "--keep-run-dir" not in argv
        return runtime_module.Result(0, WRAPPER_PASS_OUTPUT, b"")

    monkeypatch.setattr(runtime_module, "run", fake_run)
    monkeypatch.setattr(runtime_module.os, "getuid", lambda: 1000)
    state = {"snapshot": rt.git.snapshot, "owned": []}
    receipts = runtime_module.ProjectExecVerifier(rt).verify(state)
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["lane"] == "fleet_project_exec"
    assert receipt["runner_sha256"] == data["verification"]["runner_sha256"]
    assert receipt["commands"] == ["npm run verify:all"]
    assert receipt["registry_fetch_seen"] is True
    assert receipt["networkless_verification"] is True
    assert receipt["returncode"] == 0
    assert state["owned"] == [], "project runner owns its own --rm containers"
    assert rt.git.capture_calls == 2
    evidence = Path(data["state_root"]) / ("action-" + receipt["action"])
    assert (evidence / "stdout.log").read_bytes() == WRAPPER_PASS_OUTPUT
    record = json.loads((evidence / "receipt.json").read_text())
    assert record == receipt


@pytest.mark.parametrize("case", ["missing_registry", "missing_networkless", "missing_pass", "nonzero_exit"])
def test_project_exec_verifier_fails_closed_without_wrapper_contract_markers(tmp_path, monkeypatch, case):
    rt, data, calls = _runtime(tmp_path, monkeypatch)
    output = WRAPPER_PASS_OUTPUT
    if case == "missing_registry": output = output.replace(b"PHASE A REGISTRY FETCH: PROVEN\n", b"")
    elif case == "missing_networkless": output = output.replace(b"PHASE B NETWORK: none\n", b"")
    elif case == "missing_pass": output = output.replace(b"FLEET EXEC: PASS\n", b"")
    exit_code = 1 if case == "nonzero_exit" else 0

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[0] == rt.docker and argv[1] == "image":
            return runtime_module.Result(0, (data["verification"]["image"] + "\n").encode(), b"")
        return runtime_module.Result(exit_code, output, b"")

    monkeypatch.setattr(runtime_module, "run", fake_run)
    monkeypatch.setattr(runtime_module.os, "getuid", lambda: 1000)
    state = {"snapshot": rt.git.snapshot, "owned": []}
    with pytest.raises(Halt):
        runtime_module.ProjectExecVerifier(rt).verify(state)
    assert state["owned"] == []
    assert not list(Path(data["state_root"]).glob("action-*/receipt.json"))


def test_project_exec_verifier_rejects_runner_drift_before_any_process(tmp_path, monkeypatch):
    rt, data, calls = _runtime(tmp_path, monkeypatch)
    Path(data["verification"]["runner"]).write_bytes(b"tampered runner bytes\n")
    monkeypatch.setattr(runtime_module, "run", lambda *a, **k: calls.append((a, k)) or runtime_module.Result(0, b"", b""))
    monkeypatch.setattr(runtime_module.os, "getuid", lambda: 1000)
    with pytest.raises(Halt):
        runtime_module.ProjectExecVerifier(rt).verify({"snapshot": rt.git.snapshot, "owned": []})
    assert calls == [], "a tampered pinned runner must be rejected before any process starts"


def test_project_exec_verifier_rejects_source_drift_and_dirty_worktree(tmp_path, monkeypatch):
    rt, data, calls = _runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(runtime_module.os, "getuid", lambda: 1000)

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[0] == rt.docker and argv[1] == "image":
            return runtime_module.Result(0, (data["verification"]["image"] + "\n").encode(), b"")
        rt.git.snapshot = {"manifest": [], "manifest_hash": "e" * 64, "diff": "ZHJpZg==", "diff_hash": "f" * 64}
        return runtime_module.Result(0, WRAPPER_PASS_OUTPUT, b"")

    monkeypatch.setattr(runtime_module, "run", fake_run)
    with pytest.raises(Halt):
        runtime_module.ProjectExecVerifier(rt).verify({"snapshot": rt.git.snapshot, "owned": []})
    assert not list(Path(data["state_root"]).glob("action-*/receipt.json"))

    rt.git.snapshot = {"manifest": [], "manifest_hash": "b" * 64, "diff": "ZGlmZg==", "diff_hash": "c" * 64}
    rt.git.status_paths.append("src/untracked.ts")
    calls.clear()
    with pytest.raises(Halt):
        runtime_module.ProjectExecVerifier(rt).verify({"snapshot": rt.git.snapshot, "owned": []})
    assert calls == [], "a dirty worktree must be rejected before the runner starts"


def test_verify_action_dispatches_lane_without_touching_python_verifier(tmp_path, monkeypatch):
    rt, data, calls = _runtime(tmp_path, monkeypatch)
    seen = {}

    class FakeProject:
        def __init__(self, _runtime):
            pass

        def verify(self, _state):
            seen["project"] = True
            return [{"lane": "fleet_project_exec"}]

    class FakePython:
        def __init__(self, _runtime):
            pass

        def verify(self, _state):
            seen["python"] = True
            return []

    monkeypatch.setattr(runtime_module, "ProjectExecVerifier", FakeProject)
    monkeypatch.setattr(runtime_module, "DockerVerifier", FakePython)
    runtime = runtime_module.Runtime.__new__(runtime_module.Runtime)
    runtime.plan = rt.plan
    runtime.p = data
    assert runtime.action("verify", {}, None) == [{"lane": "fleet_project_exec"}]
    assert seen == {"project": True}

    seen.clear()
    legacy = dict(data)
    del legacy["verification"]
    legacy["test_files"] = ["tests/test_cycle.py"]
    runtime.p = legacy
    runtime_module.Runtime.action(runtime, "verify", {}, None)
    assert seen == {"python": True}

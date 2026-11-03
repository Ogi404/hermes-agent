"""Bounded Python implementation contracts; isolated Git fixtures, never live operations."""
import base64
import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.fleet_lifecycle.controller import Halt, Native, Plan, canonical, digest
from plugins.fleet_lifecycle.runtime import Git, Runtime
from tests.plugins.test_fleet_lifecycle_controller import plan_data
from tests.plugins.test_fleet_verification_packet import blob_repo


def implementation_plan(tmp_path):
    p = plan_data(tmp_path)
    p.update(schema=2, mutation=None,
             candidate_paths=["example.py", "tests/test_example.py"],
             test_files=["tests/test_example.py"],
             changes=[{"path": "example.py", "base_sha256": "a" * 64},
                      {"path": "tests/test_example.py", "base_sha256": None}])
    return p


def test_implementation_contract_accepts_explicit_pinned_production_edit(tmp_path):
    p = implementation_plan(tmp_path)
    Plan.validate(p)
    native = Native(SimpleNamespace(data=p))
    keys = ("base", "candidate_paths", "test_files", "changes")
    payload = canonical({k: p[k] for k in keys}).decode()
    assert native.scope_description() == "fleet-lifecycle-implementation-v1 " + payload
    assert native.scope_description(markdown=True) == "fleet-lifecycle-implementation-v1\n\n```json\n" + payload + "\n```"
    assert p == implementation_plan(tmp_path), "validation must not normalize the frozen contract"


@pytest.mark.parametrize("case", ["unbound", "bad_hash", "duplicate", "extra", "mutation", "no_tests", "outside_test", "not_python", "control_file"])
def test_implementation_contract_rejects_unbounded_or_conflicting_scope(tmp_path, case):
    p = implementation_plan(tmp_path)
    if case == "unbound": p["changes"].pop()
    elif case == "bad_hash": p["changes"][0]["base_sha256"] = "bad"
    elif case == "duplicate": p["changes"].append(copy.deepcopy(p["changes"][0]))
    elif case == "extra": p["changes"][0]["command"] = "execute"
    elif case == "mutation": p["mutation"] = plan_data(tmp_path)["mutation"]
    elif case == "no_tests": p["test_files"] = []
    elif case == "outside_test": p["test_files"] = ["outside.py"]
    else:
        name = "example.sh" if case == "not_python" else "scripts/runner.py"
        p["candidate_paths"][0] = name
        p["candidate_paths"].sort()
        p["changes"][0]["path"] = name
        p["changes"].sort(key=lambda x: x["path"])
    with pytest.raises(Halt): Plan.validate(p)


def test_legacy_contract_still_rejects_production_edit(tmp_path):
    p = plan_data(tmp_path)
    Plan.validate(p)
    p["candidate_paths"] = ["example.py", *p["test_files"]]
    with pytest.raises(Halt): Plan.validate(p)


def test_git_capture_binds_existing_preimage_new_bytes_and_complete_diff(blob_repo):
    git, base = blob_repo
    repo = git.workspace
    (repo / "example.py").write_bytes(b"VALUE = 1\n")
    git.call("add", "--", "example.py")
    git.call("commit", "-m", "Fixture production base")
    base = git.call("rev-parse", "HEAD").decode().strip()
    old = git.call("show", base + ":example.py")
    p = implementation_plan(repo.parent)
    p.update(base=base, branch="fixture", workspace=str(repo))
    p["changes"][0]["base_sha256"] = digest(old)
    (repo / "example.py").write_bytes(b"VALUE = 2\n")
    (repo / "tests").mkdir()
    (repo / "tests/test_example.py").write_bytes(b"def test_value():\n    assert True\n")
    snapshot = git.capture(p)
    assert [m["path"] for m in snapshot["manifest"]] == p["candidate_paths"]
    item = snapshot["manifest"][0]
    assert base64.b64decode(item["base_bytes"]) == old
    assert item["base_sha256"] == digest(old)
    assert base64.b64decode(item["bytes"]) == b"VALUE = 2\n"
    assert snapshot["manifest"][1]["base_sha256"] is None
    diff = base64.b64decode(snapshot["diff"])
    assert b"-VALUE = 1" in diff and b"+VALUE = 2" in diff
    assert b"diff --git a/tests/test_example.py b/tests/test_example.py" in diff
    assert not git.call("diff", "--cached", "--name-only"), "capture must not stage the real index"
    assert set(git.status()) == set(p["candidate_paths"])
    bad = copy.deepcopy(p)
    bad["changes"][0]["base_sha256"] = "0" * 64
    with pytest.raises(Halt): git.capture(bad)
    # Exact manifest still owns commit and existing source must not be reclassified as new.
    p["commit"] = {"name": "Fixture", "email": "fixture@example.invalid", "message": "Implement pinned value"}
    head = git.commit(p, snapshot)
    assert git.call("rev-list", "--parents", "-n", "1", head).decode().strip() == head + " " + base
    assert git.call("show", head + ":example.py") == b"VALUE = 2\n"
    git.clean()


def test_admission_checks_production_and_test_base_without_mutation(blob_repo):
    git, base = blob_repo
    repo = git.workspace
    (repo / "example.py").write_bytes(b"VALUE = 1\n")
    (repo / "tests").mkdir()
    (repo / "tests/test_example.py").write_bytes(b"def test_existing():\n    assert True\n")
    git.call("add", "--", "example.py", "tests/test_example.py")
    git.call("commit", "-m", "Fixture admission base")
    base = git.call("rev-parse", "HEAD").decode().strip()
    p = implementation_plan(repo.parent)
    p.update(base=base, branch="fixture", workspace=str(repo), candidate_paths=["example.py"],
             changes=[{"path": "example.py", "base_sha256": digest(b"VALUE = 1\n")}],
             resources={"scratch_mb": 256})
    rt = Runtime.__new__(Runtime)
    rt.git, rt.p = git, p
    rt.admission_snapshot()
    assert not git.call("status", "--porcelain=v1")
    for mode in ("preimage", "missing_test"):
        bad = copy.deepcopy(p)
        if mode == "preimage": bad["changes"][0]["base_sha256"] = "0" * 64
        else: bad["test_files"] = ["tests/test_absent.py"]
        rt.p = bad
        with pytest.raises(Halt): rt.admission_snapshot()
        assert not git.call("status", "--porcelain=v1")



def test_implementation_verifier_runs_only_frozen_baseline_without_mutation(blob_repo, tmp_path, monkeypatch):
    """Real packet/capture/receipt path with a recording Docker boundary, no inference."""
    import io
    import json
    import tarfile
    from plugins.fleet_lifecycle import runtime
    git, base = blob_repo
    repo = git.workspace
    (repo / "example.py").write_bytes(b"VALUE = 1\n")
    git.call("add", "--", "example.py")
    git.call("commit", "-m", "Fixture implementation verification")
    base = git.call("rev-parse", "HEAD").decode().strip()
    p = implementation_plan(repo.parent)
    p.update(base=base, branch="fixture", workspace=str(repo), state_root=str(tmp_path / "evidence"))
    p["changes"][0]["base_sha256"] = digest(b"VALUE = 1\n")
    p["resources"]["scratch_mb"] = 256
    Path(p["state_root"]).mkdir()
    (repo / "example.py").write_bytes(b"VALUE = 2\n")
    (repo / "tests").mkdir()
    (repo / "tests/test_example.py").write_bytes(b"def test_value():\n    assert True\n")
    rt = SimpleNamespace(p=p, git=git, plan=SimpleNamespace(digest="b" * 64), docker="/fixture/docker",
                         env={}, generation=Path("/fixture/generation"),
                         test_python=Path("/fixture/generation/venv/bin/python"))
    state = {"snapshot": git.capture(p), "owned": []}
    rt.own = lambda s, r: s["owned"].append(r)
    rt.release = lambda s, r: s["owned"].remove(r)
    configs = []
    active = set()
    actual_run = runtime.run
    def recording_run(argv, **kwargs):
        if argv[0] != rt.docker: return actual_run(argv, **kwargs)
        if argv[1] == "run":
            assert "--network=none" in argv and "--read-only" in argv
            assert "--cap-drop=ALL" in argv and "--security-opt=no-new-privileges" in argv
            with tarfile.open(fileobj=io.BytesIO(kwargs["stdin"])) as packet:
                cfg = json.loads(packet.extractfile("config.json").read())
                archive = packet.extractfile("base.tar").read()
            runtime.validate_archive(git, base, archive)
            configs.append(cfg)
            assert cfg["mutation"] is None and cfg["mutant"] is False
            assert cfg["test_file"] == "tests/test_example.py"
            assert cfg["manifest"] == state["snapshot"]["manifest"]
            active.add(argv[argv.index("--name") + 1])
            xml = b'<testsuite><testcase classname="tests.test_example" name="test_value"/></testsuite>'
            return runtime.Result(0, canonical({k: base64.b64encode(v).decode() for k, v in
                                  (("junit", xml), ("stdout", b"recorded fixture pass"), ("stderr", b""))}), b"")
        if argv[1] == "inspect":
            return runtime.Result(0, canonical({"hermes.fleet.action": state["owned"][0]["label"]}), b"")
        if argv[1] == "rm":
            active.remove(argv[-1])
            return runtime.Result(0, b"", b"")
        assert argv[1:3] == ["container", "ls"]
        return runtime.Result(0, "\n".join(active).encode(), b"")
    monkeypatch.setattr(runtime, "run", recording_run)
    receipts = runtime.DockerVerifier(rt).verify(state)
    assert len(receipts) == len(configs) == 1
    assert receipts[0]["mutant"] is False and receipts[0]["collected"] == 1
    assert receipts[0]["cleanup"] == "owned_removed" and not active and not state["owned"]
    assert (Path(p["state_root"]) / ("action-" + receipts[0]["action"]) / "receipt.json").is_file()
    assert (repo / "example.py").read_bytes() == b"VALUE = 2\n"
    assert not git.call("diff", "--cached", "--name-only")


def test_schema2_capture_rejects_extra_source_and_symlink(blob_repo):
    git, base = blob_repo
    repo = git.workspace
    p = implementation_plan(repo.parent)
    p.update(base=base, branch="fixture", workspace=str(repo))
    p["changes"][0]["base_sha256"] = None
    (repo / "example.py").write_bytes(b"VALUE = 2\n")
    (repo / "tests").mkdir()
    (repo / "tests/test_example.py").write_bytes(b"def test_value():\n    assert True\n")
    outsider = repo / "unexpected.py"
    outsider.write_bytes(b"unapproved = True\n")
    with pytest.raises(Halt): git.capture(p)
    outsider.unlink()
    target = repo / "example.py"
    target.unlink()
    target.symlink_to(repo / "omitted.txt")
    with pytest.raises(Halt): git.capture(p)
    assert not git.call("diff", "--cached", "--name-only")



@pytest.mark.parametrize("test_file,mutant", [("tests/test_example.py", True), ("tests/test_outside.py", False)])
def test_schema2_verification_rejects_unfrozen_action_before_resources(tmp_path, monkeypatch, test_file, mutant):
    from plugins.fleet_lifecycle import runtime
    p = implementation_plan(tmp_path)
    p["state_root"] = str(tmp_path / "must-not-be-created")
    rt = SimpleNamespace(p=p)
    def forbidden(*args, **kwargs):
        pytest.fail("rejected verification must not create resources or launch children")
    monkeypatch.setattr(runtime, "run", forbidden)
    with pytest.raises(Halt, match="verification_failed"):
        runtime.DockerVerifier(rt).invocation({}, test_file, mutant)
    assert not Path(p["state_root"]).exists()



def test_schema2_snapshot_diff_uses_stable_full_object_ids(blob_repo):
    """Git's automatic abbreviation is metadata-dependent, not candidate identity."""
    git, base = blob_repo
    repo = git.workspace
    p = implementation_plan(repo.parent)
    p.update(base=base, branch="fixture", workspace=str(repo))
    p["changes"][0]["base_sha256"] = None
    (repo / "example.py").write_bytes(b"VALUE = 2\n")
    (repo / "tests").mkdir()
    (repo / "tests/test_example.py").write_bytes(b"def test_value():\n    assert True\n")
    git.call("config", "core.abbrev", "7")
    first = git.capture(p)
    git.call("config", "core.abbrev", "12")
    second = git.capture(p)
    assert first == second, "object-ID abbreviation must not invalidate unchanged source"

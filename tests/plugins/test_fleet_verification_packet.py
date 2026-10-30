"""Exact Git-object verification packets; no live fleet or inference."""
import io
import tarfile
from pathlib import Path

import pytest

from hermes_platform.resolver import locate_command
from plugins.fleet_lifecycle import runtime


@pytest.fixture
def blob_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_COUNT": "0",
           "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
           "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid"}
    git = runtime.Git(locate_command("git").command[0], repo, env)
    git.call("init", "-b", "fixture")
    (repo / ".gitattributes").write_text("*.ps1 text eol=crlf\nomitted.txt export-ignore\n")
    (repo / "script.ps1").write_bytes(b"Write-Output 'fixture'\n")
    (repo / "omitted.txt").write_bytes(b"full Git tree, not export policy\n")
    (repo / "large.txt").write_bytes(b"unchanged base data\n" * 16384)
    (repo / "executable.sh").write_bytes(b"#!/bin/sh\nexit 0\n")
    (repo / "executable.sh").chmod(0o755)
    git.call("-c", "core.autocrlf=false", "add", "--", ".")
    git.call("commit", "-m", "Fixture exact blobs")
    return git, git.call("rev-parse", "HEAD").decode().strip()


def test_exact_git_blob_packet_is_compressed_and_ignores_export_transforms(blob_repo):
    git, base = blob_repo
    archive = runtime.archive_snapshot(git, base, scratch_mb=256, max_bytes=4096)
    assert archive.startswith(b"\x1f\x8b") and len(archive) <= 4096
    runtime.validate_archive(git, base, archive)
    with tarfile.open(fileobj=io.BytesIO(archive)) as packet:
        assert packet.extractfile("script.ps1").read() == git.call("show", base + ":script.ps1")
        assert packet.extractfile("omitted.txt").read() == b"full Git tree, not export policy\n"
        assert packet.getmember("executable.sh").mode == 0o755
        assert packet.getmember("large.txt").size > 128 * 1024
    assert not git.call("status", "--porcelain=v1")


def test_insufficient_scratch_rejects_before_blob_capture_or_source_dispatch(blob_repo, monkeypatch):
    git, base = blob_repo
    original = git.call
    calls = []

    def capture(*args, **kwargs):
        calls.append(args)
        assert args[0] != "cat-file", "capacity rejection must precede blob transfer"
        return original(*args, **kwargs)

    monkeypatch.setattr(git, "call", capture)
    with pytest.raises(runtime.Halt, match="verification_failed"):
        runtime.archive_snapshot(git, base, scratch_mb=64, max_bytes=4096)
    assert calls and all(args[0] == "ls-tree" for args in calls)


@pytest.mark.parametrize("path", ["../escape", "/absolute", "a/../escape", ".git/config", "x/.GiT/config", "a\\\\b", "a//b", "a/./b", "a\nname"])
def test_archive_path_rejects_escape_metadata_and_noncanonical_names(path):
    with pytest.raises(runtime.Halt):
        runtime.archive_relative(path)


def test_compressed_output_cap_is_enforced(blob_repo):
    git, base = blob_repo
    with pytest.raises(runtime.Halt, match="verification_failed"):
        runtime.archive_snapshot(git, base, scratch_mb=256, max_bytes=32)


def test_forged_blob_response_is_not_archived(blob_repo, monkeypatch):
    git, base = blob_repo
    original = git.call

    def capture(*args, **kwargs):
        raw = original(*args, **kwargs)
        if args[0] == "cat-file":
            raw = raw.replace(b"Write-Output", b"Write-Invalid", 1)
        return raw

    monkeypatch.setattr(git, "call", capture)
    with pytest.raises(runtime.Halt, match="verification_failed"):
        runtime.archive_snapshot(git, base, scratch_mb=256)


def test_admission_checks_packet_capacity_before_source_claim(blob_repo):
    git, base = blob_repo
    rt = runtime.Runtime.__new__(runtime.Runtime)
    rt.git = git
    rt.p = {"branch": "fixture", "base": base, "workspace": str(git.workspace),
            "candidate_paths": ["tests/test_new.py"], "resources": {"scratch_mb": 64}}
    rt.mutation_base = lambda: None
    with pytest.raises(runtime.Halt, match="verification_failed"):
        rt.admission_snapshot()
    assert not git.call("status", "--porcelain=v1")


def test_real_docker_adapter_receives_compressed_exact_object_packet(blob_repo, tmp_path, monkeypatch):
    import base64
    import json
    from types import SimpleNamespace

    git, base = blob_repo
    root = tmp_path / "evidence"
    root.mkdir()
    data = {"base": base, "state_root": str(root), "native": {"tools_root": "/fixture/tools"},
            "mutation": {"test_file": "tests/test_new.py", "test_name": "test_new"},
            "resources": {"image": "sha256:" + "a" * 64, "cpus": 1, "memory_mb": 1024,
                          "pids": 128, "seconds": 60, "scratch_mb": 256}}
    rt = SimpleNamespace(p=data, git=git, plan=SimpleNamespace(digest="b" * 64),
                         docker="/fixture/docker", env={}, generation=Path("/fixture/generation"),
                         test_python=Path("/fixture/generation/venv/bin/python"))
    rt.own = lambda state, resource: state.setdefault("owned", []).append(resource)
    rt.release = lambda state, resource: state["owned"].remove(resource)
    observed = []
    state = {"snapshot": {"manifest": [], "manifest_hash": "c" * 64}}

    def fake_run(argv, **kwargs):
        if argv[1] == "run":
            with tarfile.open(fileobj=io.BytesIO(kwargs["stdin"])) as outer:
                compressed = outer.extractfile("base.tar").read()
            assert compressed.startswith(b"\x1f\x8b")
            runtime.validate_archive(git, base, compressed)
            observed.append(len(compressed))
            xml = b'<testsuite><testcase classname="tests.test_new" name="test_new"/></testsuite>'
            return runtime.Result(0, json.dumps({key: base64.b64encode(value).decode() for key, value in
                                  (("junit", xml), ("stdout", b"fixture"), ("stderr", b""))}).encode(), b"")
        if argv[1] == "inspect":
            return runtime.Result(0, json.dumps({"hermes.fleet.action": state["owned"][0]["label"]}).encode(), b"")
        return runtime.Result(0, b"", b"")

    # Keep actual Git subprocesses separate from this recording Docker boundary.
    original = runtime.run
    def dispatch(argv, **kwargs):
        return fake_run(argv, **kwargs) if argv[0] == rt.docker else original(argv, **kwargs)
    monkeypatch.setattr(runtime, "run", dispatch)
    receipt = runtime.DockerVerifier(rt).invocation(state, "tests/test_new.py", False)
    assert receipt["collected"] == 1 and observed and not state["owned"]


def test_archive_rejects_declared_blob_size_before_reading_untrusted_body(blob_repo, monkeypatch):
    git, base = blob_repo
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        info = tarfile.TarInfo(".gitattributes")
        info.mode = 0o644
        raw = git.call("show", base + ":.gitattributes") + b"extra"
        info.size = len(raw)
        archive.addfile(info, io.BytesIO(raw))

    def forbidden_read(*args, **kwargs):
        pytest.fail("size mismatch must reject before extractfile reads body")
    monkeypatch.setattr(tarfile.TarFile, "extractfile", forbidden_read)
    with pytest.raises(runtime.Halt, match="verification_failed"):
        runtime.validate_archive(git, base, stream.getvalue())

"""Production Git, contained Docker verification, and native review adapters.

Candidate Python is parsed as data or executed only inside Docker. No host
shell, dependency acquisition, network Git command, or product edit exists.
"""
from __future__ import annotations

import ast
import base64
import io
import hashlib
import os
import re
from pathlib import Path
import selectors
import signal
import subprocess
import tarfile
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from plugins.fleet_lifecycle.controller import (
    Halt, canonical, digest, read_regular, relative, require, safe_path, sha, strict_json,
)


MAX_OUTPUT = 4 * 1024 * 1024


@dataclass
class Result:
    returncode: int
    stdout: bytes
    stderr: bytes


def run(argv, *, cwd, env, seconds=60, limit=MAX_OUTPUT, stdin=None, input_limit=1024 * 1024):
    """Bounded pipes and whole-session cleanup; never a shell."""
    process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    output = {process.stdout: bytearray(), process.stderr: bytearray()}
    deadline = time.monotonic() + seconds
    selector = selectors.DefaultSelector()
    try:
        for pipe in output:
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ)
        if stdin is not None:
            require(len(stdin) <= input_limit)
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE)
            pending = memoryview(stdin)
        while selector.get_map():
            require(time.monotonic() < deadline, "action_timeout")
            for key, _ in selector.select(min(0.25, max(0, deadline - time.monotonic()))):
                pipe = key.fileobj
                if pipe is process.stdin:
                    count = os.write(pipe.fileno(), pending[:8192])
                    pending = pending[count:]
                    if not pending:
                        selector.unregister(pipe); pipe.close()
                    continue
                chunk = os.read(pipe.fileno(), 65536)
                if not chunk:
                    selector.unregister(pipe)
                else:
                    output[pipe].extend(chunk)
                    require(sum(len(v) for v in output.values()) <= limit, "output_limit")
        code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        return Result(code, bytes(output[process.stdout]), bytes(output[process.stderr]))
    finally:
        selector.close()
        # Never signal a released/reaped PID: only an unreaped leader retains
        # its process-group identity. Escaped native sessions are cgroup-owned.
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()


def junit_proof(raw, mutant, selected, selected_file=None):
    require(len(raw) <= MAX_OUTPUT and b"<!" not in raw, "verification_failed")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise Halt("verification_failed") from None
    cases = list(root.iter("testcase"))
    require(bool(cases) and not list(root.iter("error")) and not list(root.iter("skipped")), "verification_failed")
    failures = list(root.iter("failure"))
    if not mutant:
        require(not failures, "verification_failed")
    else:
        relevant = [c for c in cases if c.get("name") == selected or c.get("name", "").startswith(selected + "[")]
        require(bool(relevant) and bool(failures), "verification_failed")
        if selected_file is not None:
            classname = selected_file.removesuffix(".py").replace("/", ".")
            require(all(c.get("classname") == classname or c.get("file") == selected_file
                        for c in relevant), "verification_failed")
        def expected_failure(f):
            kind = f.get("type") or ""
            text = (f.get("message") or "") + "\n" + (f.text or "")
            return (kind == "AssertionError" or (not kind and re.search(r"(?m)^E\s+AssertionError(?:[:\s]|$)", text) is not None) or
                    (kind in {"", "Failed", "pytest.fail.Exception"}
                     and re.search(r"(?m)^(?:E[ \t]+)?Failed: DID NOT RAISE (?:<class ['\"]?ValueError['\"]?>|ValueError)$", text) is not None))
        assertion_failures = [f for c in relevant for f in c.findall("failure") if expected_failure(f)]
        require(bool(assertion_failures) and len(assertion_failures) == len(failures), "verification_failed")
    return len(cases)


class Git:
    def __init__(self, executable, workspace, env):
        self.executable, self.workspace, self.env = executable, Path(workspace), env

    def call(self, *args, ok=True, limit=MAX_OUTPUT):
        command = [self.executable, "--no-pager", "-c", "core.hooksPath=/dev/null",
                   "-c", "core.fsmonitor=false", "-c", "core.attributesFile=/dev/null", "-c", "tar.umask=0022",
                   "-c", "protocol.allow=never", "-c", "submodule.recurse=false",
                   "-c", "diff.external=", "-c", "commit.gpgsign=false", "-c", "core.quotePath=false",
                   "-C", str(self.workspace), *args]
        result = run(command, cwd="/", env=self.env, limit=limit)
        require(not ok or result.returncode == 0)
        return result.stdout

    def guard(self, branch, head):
        config = self.call("config", "--local", "--null", "--list")
        require(not any(item.split(b"\n", 1)[0].lower().startswith((b"filter.", b"include.", b"includeif."))
                        for item in config.split(b"\0") if item))
        require(self.call("symbolic-ref", "--short", "HEAD").decode().strip() == branch)
        require(self.call("rev-parse", "HEAD").decode().strip() == head)
        require(self.call("rev-parse", "--show-toplevel").decode().strip() == str(self.workspace))
        require(self.call("rev-parse", "--is-shallow-repository").strip() == b"false")
        # Git has worktree-local configuration as well as common local config.
        extra = self.call("config", "--worktree", "--null", "--list", ok=False)
        require(not any(item.split(b"\n", 1)[0].lower().startswith((b"filter.", b"include.", b"includeif."))
                        for item in extra.split(b"\0") if item))

    def status(self):
        raw = self.call("status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=matching")
        entries = []
        for item in raw.split(b"\0"):
            if item:
                require(item[:3] in {b" M ", b"?? "})
                path = item[3:].decode("utf-8", "strict")
                relative(path)
                entries.append(path)
        return entries

    def clean(self):
        require(not self.status())
        require(not self.call("diff", "--cached", "--name-only"))

    def capture(self, p):
        self.guard(p["branch"], p["base"])
        paths = self.status()
        require(set(paths) == set(p["candidate_paths"]))
        require(not self.call("diff", "--cached", "--name-only"))
        for path in paths:
            require(not self.call("ls-tree", "-z", p["base"], "--", path))
        require(p["mutation"]["test_file"] in paths)
        manifest, total = [], 0
        for path in sorted(paths):
            content = read_regular(self.workspace / path, 1024 * 1024)
            total += len(content)
            require(total <= 2 * 1024 * 1024)
            mode = (self.workspace / path).lstat().st_mode & 0o777
            require(mode == 0o644)
            manifest.append({"path": path, "mode": "100644", "sha256": digest(content),
                             "bytes": base64.b64encode(content).decode()})
        self.guard(p["branch"], p["base"])
        require(paths == self.status())
        diff = self.call("diff", "--no-ext-diff", "--no-textconv", "--binary", p["base"], "--", *paths)
        # New files are also bound by full byte manifests, independent of git diff.
        return {"manifest": manifest, "manifest_hash": digest(canonical(manifest)),
                "diff": base64.b64encode(diff).decode(), "diff_hash": digest(diff)}

    def commit(self, p, snapshot):
        require(self.capture(p) == snapshot)
        paths = [m["path"] for m in snapshot["manifest"]]
        self.call("add", "--", *paths)
        raw = self.call("diff", "--cached", "--name-only", "-z").split(b"\0")
        require(sorted(x.decode() for x in raw if x) == sorted(paths))
        for item in snapshot["manifest"]:
            entry = self.call("ls-files", "--stage", "--", item["path"]).decode()
            require(entry.startswith("100644 ") and " 0\t" in entry)
            require(digest(self.call("show", ":" + item["path"])) == item["sha256"])
            require(digest(read_regular(self.workspace / item["path"])) == item["sha256"])
        tree = self.call("write-tree").decode().strip(); sha(tree, 40)
        self.guard(p["branch"], p["base"])
        c = p["commit"]
        original = self.env
        self.env = {**original, "GIT_AUTHOR_NAME": c["name"], "GIT_AUTHOR_EMAIL": c["email"],
                    "GIT_COMMITTER_NAME": c["name"], "GIT_COMMITTER_EMAIL": c["email"]}
        try:
            self.call("-c", "core.autocrlf=false", "commit", "--no-verify", "--no-gpg-sign", "-m", c["message"])
        finally:
            self.env = original
        head = self.call("rev-parse", "HEAD").decode().strip(); sha(head, 40)
        require(self.call("rev-list", "--parents", "-n", "1", head).decode().strip() == head + " " + p["base"])
        require(self.call("show", "-s", "--format=%T", head).decode().strip() == tree)
        require(self.call("show", "-s", "--format=%B", head).decode().strip() == c["message"])
        require(sorted(x.decode() for x in self.call("diff-tree", "--no-commit-id", "--name-only", "-r", "-z", head).split(b"\0") if x) == sorted(paths))
        self.guard(p["branch"], head)
        self.clean()
        return head


# Extraction and mutation run ONLY inside the disposable container. The input
# tar and all controller-supplied scripts are readonly, never worker commands.
_CONTAINER = r"""
import base64, json, os, pathlib, subprocess, tarfile, sys
os.mkdir("/var/tmp/input")
with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as bundle:
    bundle.extractall("/var/tmp/input", filter="data")
cfg=json.loads(pathlib.Path("/var/tmp/input/config.json").read_text())
source=pathlib.Path("/var/tmp/source"); source.mkdir()
with tarfile.open("/var/tmp/input/base.tar") as archive:
    archive.extractall(source, filter="data")
for item in cfg["manifest"]:
    path=source/item["path"]
    for ancestor in [path, *path.parents]:
        if ancestor == source.parent: break
        if ancestor.is_symlink(): raise RuntimeError("unsafe overlay")
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(base64.b64decode(item["bytes"], validate=True)); path.chmod(0o644)
if cfg["mutant"]:
    m=cfg["mutation"]; path=source/m["path"]
    import hashlib
    original=path.read_bytes()
    if hashlib.sha256(original).hexdigest() != m["base_sha256"]: raise RuntimeError("mutation base")
    if original.count(m["before"].encode()) != 1: raise RuntimeError("mutation occurrence")
    changed=original.replace(m["before"].encode(),m["after"].encode(),1)
    if hashlib.sha256(changed).hexdigest() != m["mutant_sha256"]: raise RuntimeError("mutation result")
    path.write_bytes(changed)
os.mkdir("/var/tmp/home")
env={"PATH":"/usr/local/bin:/usr/bin:/bin", "HOME":"/var/tmp/home",
     "HERMES_PYTHON":cfg["python"], "HERMES_TEST_WORKERS":"1",
     "HERMES_TEST_FILE_RETRIES":"0", "HERMES_TEST_FILE_TIMEOUT":str(cfg["seconds"]),
     "TZ":"UTC", "LANG":"C.UTF-8"}
# Avoid the canonical runner provisioning when the mounted interpreter is not usable.
subprocess.run([cfg["python"],"-I","-c","import pytest"],env=env,check=True)
args=["bash","scripts/run_tests.sh",cfg["test_file"],"--","--junitxml=/var/tmp/result.xml"]
if cfg["mutant"]: args.extend(["-k",cfg["mutation"]["test_name"]])
with open("/var/tmp/stdout", "wb") as out, open("/var/tmp/stderr", "wb") as err:
    result=subprocess.run(args,cwd=source,env=env,stdout=out,stderr=err,timeout=cfg["seconds"])
packet={}
for key, filename in (("junit","result.xml"),("stdout","stdout"),("stderr","stderr")):
    with open("/var/tmp/"+filename,"rb") as stream: raw=stream.read(1024*1024+1)
    if len(raw)>1024*1024: raise RuntimeError("receipt limit")
    packet[key]=base64.b64encode(raw).decode()
print(json.dumps(packet,separators=(",",":")))
raise SystemExit(result.returncode)
"""


def validate_archive(git, base, raw):
    """Bind archive bytes/modes to every Git blob; reject links/export transforms."""
    expected = {}
    for item in git.call("ls-tree", "-r", "-z", "--full-tree", base, limit=16 * 1024 * 1024).split(b"\0"):
        if not item:
            continue
        header, path = item.split(b"\t", 1)
        mode, kind, oid = header.decode().split()
        name = path.decode(); relative(name)
        require(mode in {"100644", "100755"} and kind == "blob", "verification_failed")
        expected[name] = (mode, oid)
    seen = set()
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for member in archive:
            relative(member.name.rstrip("/"))
            if member.isdir():
                continue
            require(member.isfile() and member.name in expected and member.name not in seen, "verification_failed")
            mode, oid = expected[member.name]
            require(member.mode & 0o777 == int(mode, 8) & 0o777, "verification_failed")
            data = archive.extractfile(member).read()
            object_id = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
            require(object_id == oid, "verification_failed")
            seen.add(member.name)
    require(seen == set(expected), "verification_failed")


class DockerVerifier:
    def __init__(self, runtime):
        self.runtime = runtime

    def owned_remove(self, name, label):
        rt = self.runtime
        inspect = run([rt.docker, "inspect", "--format={{json .Config.Labels}}", name],
                      cwd="/", env=rt.env, seconds=15)
        if inspect.returncode != 0:
            # Distinguish absence from daemon/permission failure; never claim cleanup.
            listing = run([rt.docker, "container", "ls", "--all", "--format={{.Names}}"], cwd="/", env=rt.env, seconds=15)
            require(listing.returncode == 0 and name not in listing.stdout.decode().splitlines(), "verification_failed")
            return
        if inspect.returncode == 0:
            labels = strict_json(inspect.stdout)
            require(labels.get("hermes.fleet.action") == label)
            result = run([rt.docker, "rm", "--force", name], cwd="/", env=rt.env, seconds=30)
            require(result.returncode == 0, "verification_failed")
            remaining = run([rt.docker, "container", "ls", "--all", "--format={{.Names}}"], cwd="/", env=rt.env, seconds=15)
            require(remaining.returncode == 0 and name not in remaining.stdout.decode().splitlines(), "verification_failed")

    def invocation(self, state, test_file, mutant):
        rt, p = self.runtime, self.runtime.p
        action = uuid.uuid4().hex
        name, label = "hermes-fleet-" + action, rt.plan.digest + "-" + action
        evidence = Path(p["state_root"]) / ("action-" + action)
        evidence.mkdir(mode=0o700)
        # UID is the controller user, not root. Docker never sees a profile mount.
        require(os.getuid() != 0, "verification_failed")
        base = rt.git.call("archive", "--format=tar", p["base"], limit=128 * 1024 * 1024)
        require(len(base) <= min(128 * 1024 * 1024, p["resources"]["scratch_mb"] * 512 * 1024), "verification_failed")
        validate_archive(rt.git, p["base"], base)
        config = {"manifest": state["snapshot"]["manifest"], "mutation": p["mutation"],
                  "mutant": mutant, "test_file": test_file, "python": str(rt.test_python),
                  "seconds": p["resources"]["seconds"]}
        # No source/archive/scratch bind on the host: bounded RAM packet streams
        # into the container's aggregate-sized tmpfs. Only bounded receipts persist.
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as bundle:
            for filename, raw in (("base.tar", base), ("config.json", canonical(config))):
                info = tarfile.TarInfo(filename); info.size = len(raw); info.mode = 0o444
                bundle.addfile(info, io.BytesIO(raw))
        packet = stream.getvalue()
        require(len(packet) <= 132 * 1024 * 1024, "verification_failed")
        r = p["resources"]
        argv = [rt.docker, "run", "--interactive", "--pull=never", "--name", name,
                "--label", "hermes.fleet.action=" + label, "--network=none", "--read-only",
                "--cap-drop=ALL", "--security-opt=no-new-privileges", "--user", f"{os.getuid()}:{os.getgid()}",
                "--cpus", str(r["cpus"]), "--memory", str(r["memory_mb"]) + "m",
                "--memory-swap", str(r["memory_mb"]) + "m", "--pids-limit", str(r["pids"]),
                "--ulimit", "fsize={0}:{0}".format(r["scratch_mb"] * 1024 * 1024),
                "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=128m",
                "--tmpfs", f"/var/tmp:rw,nosuid,nodev,size={r['scratch_mb']}m,mode=1777",
                "--mount", f"type=bind,src={rt.generation},dst={rt.generation},readonly",
                "--mount", "type=bind,src={0},dst={0},readonly".format(p["native"]["tools_root"]),
                "--entrypoint", str(rt.test_python), r["image"], "-I", "-c", _CONTAINER]
        resource = {"kind": "container", "name": name, "label": label}
        rt.own(state, resource)
        result = None
        try:
            result = run(argv, cwd="/", env=rt.env, seconds=r["seconds"] + 10,
                         stdin=packet, input_limit=132 * 1024 * 1024)
        finally:
            self.owned_remove(name, label)
            rt.release(state, resource)
        packet = strict_json(result.stdout)
        require(isinstance(packet, dict) and set(packet) == {"junit", "stdout", "stderr"}, "verification_failed")
        decoded = {key: base64.b64decode(value, validate=True) for key, value in packet.items()}
        require(all(len(value) <= 1024 * 1024 for value in decoded.values()), "verification_failed")
        (evidence / "stdout.log").write_bytes(decoded["stdout"])
        (evidence / "stderr.log").write_bytes(decoded["stderr"])
        require(result.returncode == (1 if mutant else 0), "verification_failed")
        xml = decoded["junit"]
        count = junit_proof(xml, mutant, p["mutation"]["test_name"], test_file if mutant else None)
        (evidence / "junit.xml").write_bytes(xml)
        receipt = {"action": action, "manifest_hash": state["snapshot"]["manifest_hash"],
                   "test_file": test_file, "mutant": mutant, "returncode": result.returncode,
                   "collected": count, "junit_hash": digest(xml), "stdout_hash": digest(decoded["stdout"]),
                   "stderr_hash": digest(decoded["stderr"]), "resources": r, "container": name,
                   "label": label, "cleanup": "owned_removed", "scratch": "bounded_tmpfs"}
        (evidence / "receipt.json").write_bytes(canonical(receipt))
        return receipt

    def verify(self, state):
        p = self.runtime.p
        receipts = [self.invocation(state, path, False) for path in p["test_files"]]
        receipts.append(self.invocation(state, p["mutation"]["test_file"], True))
        return receipts


class Runtime:
    def __init__(self, plan):
        self.plan, self.p = plan, plan.data
        from hermes_platform.resolver import locate_command
        resolved = [locate_command(name) for name in ("git", "docker", "systemd-run", "systemctl")]
        require(all(r.found and r.command for r in resolved))
        self.git_binary, self.docker, self.systemd_run, self.systemctl = (r.command[0] for r in resolved)
        self.env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_CONFIG_COUNT": "0",
                    "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1", "GIT_ATTR_NOSYSTEM": "1"}
        self.git = Git(self.git_binary, self.p["workspace"], self.env)
        self.test_python = self.generation = None

    def principal(self):
        from hermes_constants import get_hermes_home
        from hermes_cli.profiles import get_active_profile_name
        director = self.p["profiles"]["director"]
        require(get_hermes_home() == Path(director["home"]) and get_active_profile_name() == director["name"])
        safe_path(Path(director["home"]), directory=True)

    def identity(self):
        self.principal()
        from hermes_constants import get_hermes_home
        from hermes_cli.profiles import get_active_profile_name
        from pm.testenv import testenv_python
        from pm.environments import store_root
        p = self.p
        require(Path(__file__).absolute().parent.parent.parent == Path(p["core"]))
        safe_path(Path(p["core"]), directory=True)
        safe_path(Path(p["workspace"]), directory=True)
        require(not Path(p["core"]).is_relative_to(p["workspace"]))
        director = p["profiles"]["director"]
        require(get_hermes_home() == Path(director["home"]) and get_active_profile_name() == director["name"])
        for profile in p["profiles"].values():
            safe_path(Path(profile["home"]), directory=True)
            require(digest(read_regular(Path(profile["home"]) / "config.yaml")) == profile["config_sha256"])
        for key in ("binary", "launcher"):
            require(Path(p["native"][key + "_entry"]).resolve(strict=True) == Path(p["native"][key]))
        for key in ("client", "binary", "launcher"):
            require(digest(read_regular(p["native"][key], 64 * 1024 * 1024)) == p["native"][key + "_sha256"])
        core = Git(self.git_binary, p["core"], self.env)
        require(core.call("rev-parse", "HEAD").decode().strip() == p["source_sha"])
        require(not core.call("status", "--porcelain=v1", "--untracked-files=all"))
        tools = safe_path(Path(p["native"]["tools_root"]), directory=True)
        require(tools == store_root(Path(p["core"]), honor_runtime_override=False))
        python = testenv_python(Path(p["core"]))
        require(python is not None)
        # PM interpreter symlinks are resolved only within its managed tool store.
        generation = python.parent.parent.parent
        safe_path(generation, directory=True)
        require(python.resolve().is_relative_to(tools) or python.resolve().is_relative_to(generation))
        require(not generation.is_relative_to(p["workspace"]))
        self.test_python, self.generation = python, generation
        # Config is parsed inside the isolated reviewer helper too, immediately before use.
        from plugins.fleet_lifecycle.reviewer import profile_policy
        profile_policy(p)

    def mutation_base(self):
        m = self.p["mutation"]
        entry = self.git.call("ls-tree", "-z", self.p["base"], "--", m["path"])
        require(entry.startswith(b"100644 blob ") and entry.endswith(b"\t" + m["path"].encode() + b"\0"))
        raw = self.git.call("show", self.p["base"] + ":" + m["path"])
        require(digest(raw) == m["base_sha256"] and raw.count(m["before"].encode()) == 1)
        require(digest(raw.replace(m["before"].encode(), m["after"].encode(), 1)) == m["mutant_sha256"])

    def admission_snapshot(self):
        self.git.guard(self.p["branch"], self.p["base"])
        self.git.clean()
        for path in self.p["candidate_paths"]:
            require(not self.git.call("ls-tree", "-z", self.p["base"], "--", path))
            require(not (Path(self.p["workspace"]) / path).exists())
        self.mutation_base()

    def snapshot(self):
        self.mutation_base()
        return self.git.capture(self.p)

    def own(self, state, resource):
        state.setdefault("owned", []).append(resource)
        self.persist()

    def release(self, state, resource):
        state["owned"].remove(resource)
        self.persist()

    def cleanup(self, state):
        for resource in list(state.get("owned", [])):
            if resource["kind"] == "container":
                require(re.fullmatch(r"hermes-fleet-[0-9a-f]{32}", resource["name"]) is not None)
                require(resource["label"] == self.plan.digest + "-" + resource["name"].removeprefix("hermes-fleet-"))
                DockerVerifier(self).owned_remove(resource["name"], resource["label"])
            elif resource["kind"] == "review_unit":
                from plugins.fleet_lifecycle.reviewer import stop_owned_unit
                stop_owned_unit(self, resource)
            else:
                raise Halt("identity_drift")
            state["owned"].remove(resource)

    def unchanged(self, state, new_head=None):
        head = new_head or state.get("head")
        if head:
            sha(head, 40)
            self.git.guard(self.p["branch"], head)
            self.git.clean()
            for item in state["snapshot"]["manifest"]:
                path = Path(self.p["workspace"]) / item["path"]
                require(path.lstat().st_mode & 0o777 == 0o644)
                require(digest(read_regular(path)) == item["sha256"])
                require(digest(self.git.call("show", head + ":" + item["path"])) == item["sha256"])
        else:
            require(self.git.capture(self.p) == state["snapshot"])

    def context(self, state, phase):
        snapshot = state["snapshot"]
        selected = []
        for spec in self.p["context"]:
            raw = self.git.call("show", self.p["base"] + ":" + spec["path"])
            require(len(raw) <= 2 * 1024 * 1024)
            text = raw.decode()
            if spec["functions"]:
                tree = ast.parse(text)
                nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                         and node.name in spec["functions"]]
                require(len(nodes) == len(spec["functions"]))
                text = "\n\n".join(ast.get_source_segment(text, n) for n in nodes)
            require(len(text.encode()) <= 48 * 1024)
            selected.append({"path": spec["path"], "base_context": text})
        binding = {"phase": phase, "base": self.p["base"], "head": state.get("head", self.p["base"]),
                   "diff_hash": snapshot["diff_hash"], "manifest_hash": snapshot["manifest_hash"]}
        envelope = {"binding": binding, "context": selected, "candidate": snapshot,
                    "verification": state["verification"]}
        require(len(canonical(envelope)) <= 192 * 1024, "review_failed")
        return envelope

    def prepare_review(self, state, phase):
        envelope = self.context(state, phase)
        # Strictly below the facade's 1 MiB serialized-message cap, even after
        # the outer JSON escaping and fixed native tool-policy/instruction text.
        require(2 * len(canonical(envelope)) + 16384 < 1024 * 1024, "review_failed")

    def action(self, phase, state, native):
        if phase == "verify":
            return DockerVerifier(self).verify(state)
        if phase in {"precommit_review", "exact_review"}:
            from plugins.fleet_lifecycle.reviewer import Reviewer
            return Reviewer(self).review(self.context(state, phase), state)
        if phase == "commit":
            require(state["precommit_review"]["verdict"] == "APPROVE" and state["verification"])
            return self.git.commit(self.p, state["snapshot"])
        require(phase == "request_push" and state["exact_review"]["verdict"] == "APPROVE")
        return native.request_push(state)

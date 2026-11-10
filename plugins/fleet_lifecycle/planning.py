"""Read-only preparation of a scoped implementation plan; never admission."""
from __future__ import annotations

import copy
from pathlib import Path

from plugins.fleet_lifecycle.controller import (
    Plan, canonical, digest, fields, read_regular, require, strict_json,
)


def prepare_plan(template, proposal, git):
    """Bind a proposal to exact Git preimages without changing platform authority."""
    Plan.validate(template.data)
    require(template.digest == digest(canonical(template.data)), "invalid_plan")
    require(template.data["schema"] == 2 and "brief_sha256" in template.data, "invalid_plan")
    fields(proposal, "candidate_paths test_files context")
    prepared = copy.deepcopy(template.data)
    for key in ("candidate_paths", "test_files", "context"):
        prepared[key] = copy.deepcopy(proposal[key])
    paths = prepared["candidate_paths"]
    require(isinstance(paths, list) and 0 < len(paths) <= 24, "invalid_plan")
    # Validate the entire proposed scope before even a read-only Git operation.
    prepared["changes"] = [{"path": path, "base_sha256": None} for path in paths]
    Plan.validate(prepared)
    require(str(git.workspace) == prepared["workspace"], "identity_drift")
    git.guard(prepared["branch"], prepared["base"])
    require(not git.status(strict=True), "identity_drift")
    git.implementation_index(prepared["base"])
    total = 0
    for change in prepared["changes"]:
        raw = git.base_blob(prepared["base"], change["path"])
        if raw is not None:
            total += len(raw)
            require(total <= 2 * 1024 * 1024, "invalid_plan")
            change["base_sha256"] = digest(raw)
    Plan.validate(prepared)
    # Reuse the actual admission preimage/mode/test-availability proof, not a
    # weaker planner-specific imitation. This method does no state writes.
    from plugins.fleet_lifecycle.runtime import Runtime
    check = Runtime.__new__(Runtime)
    check.git, check.p = git, prepared
    check.implementation_admission()
    return Plan(prepared, digest(canonical(prepared)))


def prepare_command(template, proposal_path):
    """Native CLI consumer: validate source/identity twice, output a proposal only."""
    from plugins.fleet_lifecycle.controller import Native
    from plugins.fleet_lifecycle.runtime import Runtime
    proposal = strict_json(read_regular(Path(proposal_path), 64 * 1024))
    fields(proposal, "candidate_paths test_files context")
    runtime = Runtime(template)
    runtime.identity()
    native = Native(template)
    with native.connect() as conn:
        native.task(conn)
    prepared = prepare_plan(template, proposal, runtime.git)
    runtime.identity()
    with Native(prepared).connect() as conn:
        Native(prepared).task(conn)
    print(canonical({"stage": "prepared_not_admitted", "plan": prepared.data,
                     "plan_hash": prepared.digest,
                     "approval_tests": Native.approval_tests(prepared)}).decode())
    return 0

"""Read-only preparation of a scoped implementation plan; never admission."""
from __future__ import annotations

import copy
from pathlib import Path

from plugins.fleet_lifecycle.controller import (
    Halt, Plan, absolute, canonical, digest, fields, read_regular, require, sha, strict_json,
)


def proposal_scope(proposal):
    require(isinstance(proposal, dict), 'invalid_plan')
    fields({k: v for k, v in proposal.items() if k != 'preview'},
           'candidate_paths test_files context')
    if 'preview' in proposal:
        fields(proposal['preview'], 'baseline baseline_sha256 expected_background_rgb')
        absolute(proposal['preview']['baseline'])
        sha(proposal['preview']['baseline_sha256'])


def bind_preview(plan, proposal):
    if 'preview' not in proposal:
        return
    spec = plan.get('verification', {}).get('preview')
    require(isinstance(spec, dict), 'invalid_plan')
    require(Path(proposal['preview']['baseline']).is_relative_to(plan['state_root']),
            'preview_baseline_outside_state_root')
    # Only reviewable data can vary: execution binaries, image, network and
    # verification commands always come from the deployment-owned policy.
    spec.update(copy.deepcopy(proposal['preview']))


def preview_reference(plan, git):
    from types import SimpleNamespace
    from plugins.fleet_lifecycle.project_visual import preflight
    from plugins.fleet_lifecycle.navigation_preview import PreviewError, index_observations
    spec = plan.data['verification']['preview']
    preflight(SimpleNamespace(p=plan.data, git=git), spec)
    raw = read_regular(Path(spec['baseline']), 1024 * 1024)
    require(digest(raw) == spec['baseline_sha256'], 'preview_identity_changed')
    try:
        index_observations(strict_json(raw))
    except PreviewError:
        raise Halt('preview_baseline_invalid') from None


def prepare_plan(template, proposal, git):
    """Bind a proposal to exact Git preimages without changing platform authority."""
    Plan.validate(template.data)
    require(template.digest == digest(canonical(template.data)), "invalid_plan")
    require(template.data["schema"] == 2 and "brief_sha256" in template.data, "invalid_plan")
    proposal_scope(proposal)
    prepared = copy.deepcopy(template.data)
    for key in ("candidate_paths", "test_files", "context"):
        prepared[key] = copy.deepcopy(proposal[key])
    bind_preview(prepared, proposal)
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
    result = Plan(prepared, digest(canonical(prepared)))
    if 'preview' in proposal:
        preview_reference(result, git)
    return result


def prepare_command(template, proposal_path):
    """Native CLI consumer: validate source/identity twice, output a proposal only."""
    from plugins.fleet_lifecycle.controller import Native
    from plugins.fleet_lifecycle.runtime import Runtime
    proposal = strict_json(read_regular(Path(proposal_path), 64 * 1024))
    proposal_scope(proposal)
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

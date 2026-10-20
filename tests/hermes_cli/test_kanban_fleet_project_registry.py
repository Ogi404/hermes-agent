"""Shared fleet cards retain the deterministic Director registry identity."""
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from hermes_cli import kanban_contract_guard as guard
from hermes_cli import projects_db


def contract_row(repo):
    c = dict(schema_version=2, issue_id="PIT-6", linear_id="issue-uuid",
             title="Acceptance", description="Static-only acceptance", status="Todo",
             project_slug="pitboss", repository="pitboss", url="https://linear.example/PIT-6",
             labels=["agent-ready"], relations=dict(blocks=[], blockedBy=[], relatedTo=[], duplicateOf=None),
             agent_ready=True, blocked=False, redactions=[], execution_mode="implement",
             source_updated_at="2026-10-20T17:00:00Z", fetched_at="2026-10-20T17:01:00Z",
             task_lane="implementation", control_plane_profiles=["default"],
             orchestration_profiles=["director"], implementation_profiles=["builder"],
             review_profiles=["reviewer"])
    c["contract_sha256"] = hashlib.sha256(json.dumps(
        {k:c[k] for k in guard.ROLE_CONTRACT_HASH_FIELDS}, sort_keys=True,
        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return dict(id="t_0123abcd", title="PIT-6: Acceptance", assignee="builder", status="ready",
                body=guard.CONTRACT_BEGIN + "\n```json\n" + json.dumps(c) + "\n```\n" + guard.CONTRACT_END,
                created_by=guard.FLEET_CREATED_BY, idempotency_key="linear:issue-uuid",
                project_id="p_owner", workspace_kind="worktree",
                workspace_path=str(repo / ".worktrees" / "t_0123abcd"),
                branch_name="pitboss/t_0123abcd-acceptance")


def registry(home, repo, project_id, slug="pitboss", archived=0):
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("{}")
    with projects_db.connect_closing(home / "projects.db") as conn:
        conn.execute("INSERT INTO projects (id,slug,name,primary_path,created_at,archived) VALUES (?,?,?,?,?,?)",
                     (project_id, slug, slug, str(repo), 0, archived))
        conn.commit()


@pytest.fixture
def homes(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / ".hermes"
    repo = tmp_path / "compliance"
    repo.mkdir()
    registry(root / "profiles/director", repo, "p_owner")
    for name in ("default", "builder", "reviewer"):
        home = root if name == "default" else root / "profiles" / name
        registry(home, repo, "p_" + name)
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root, repo


@pytest.mark.parametrize("profile", ["default", "director", "builder", "reviewer"])
def test_same_card_valid_across_profile_registries(homes, monkeypatch, profile):
    root, repo = homes
    home = root if profile == "default" else root / "profiles" / profile
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert guard.fleet_contract_guard_reason(contract_row(repo)) is None


def test_local_id_cannot_substitute_for_owner_identity(homes):
    _, repo = homes
    row = contract_row(repo)
    row["project_id"] = "p_default"
    assert guard.fleet_contract_guard_reason(row) == "task_project_unknown"


def test_forged_repository_root_is_rejected(homes):
    _, repo = homes
    row = contract_row(repo.parent / "wrong-repo")
    assert guard.fleet_contract_guard_reason(row) == "task_workspace_path_invalid"


@pytest.mark.parametrize("field,value", [("archived", 1), ("slug", "other-project")])
def test_authoritative_record_controls_acceptance(homes, field, value):
    root, repo = homes
    with sqlite3.connect(root / "profiles/director/projects.db") as conn:
        conn.execute(f"UPDATE projects SET {field}=? WHERE id='p_owner'", (value,))
    expected = "task_project_unknown" if field == "archived" else "repository_project_mismatch"
    assert guard.fleet_contract_guard_reason(contract_row(repo)) == expected


def test_missing_owner_store_is_not_created_or_locally_substituted(homes):
    root, repo = homes
    db = root / "profiles/director/projects.db"
    db.rename(db.with_suffix(".preserved"))
    assert guard.fleet_contract_guard_reason(contract_row(repo)) == "task_project_unknown"
    assert not db.exists()


@pytest.mark.parametrize("assignee", ["default", "director", "reviewer"])
def test_role_boundary_remains_before_registry_lookup(homes, assignee):
    _, repo = homes
    row = contract_row(repo)
    row["assignee"] = assignee
    assert guard.fleet_contract_guard_reason(row) == "project_role_permission_forbidden"


def test_registry_resolution_writes_no_database_bytes(homes):
    root, repo = homes
    paths = [root / "projects.db", *(root / "profiles" / p / "projects.db" for p in ("director", "builder", "reviewer"))]
    before = {p:p.read_bytes() for p in paths}
    assert guard.fleet_contract_guard_reason(contract_row(repo)) is None
    assert before == {p:p.read_bytes() for p in paths}


def test_unknown_id_stays_unknown_despite_matching_slug_path(homes):
    _, repo = homes
    row = contract_row(repo)
    row["project_id"] = "p_unknown"
    assert guard.fleet_contract_guard_reason(row) == "task_project_unknown"


@pytest.mark.parametrize("shape", ["corrupt", "empty-schema", "symlink", "directory"])
def test_invalid_owner_store_is_fail_closed(homes, shape):
    root, repo = homes
    db = root / "profiles/director/projects.db"
    prior = db.with_suffix(".preserved")
    db.rename(prior)
    if shape == "corrupt":
        db.write_bytes(b"not a sqlite database")
    elif shape == "empty-schema":
        sqlite3.connect(db).close()
    elif shape == "symlink":
        db.symlink_to(prior)
    else:
        db.mkdir()
    assert guard.fleet_contract_guard_reason(contract_row(repo)) == "task_project_unknown"


def test_context_home_not_launch_environment_controls_registry(homes, tmp_path, monkeypatch):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    root, repo = homes
    other = tmp_path / "custom-install"
    registry(other / "profiles/director", repo.parent / "foreign", "p_owner")
    monkeypatch.setenv("HERMES_HOME", str(other))
    token = set_hermes_home_override(root / "profiles/builder")
    try:
        assert guard.fleet_contract_guard_reason(contract_row(repo)) is None
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize("primary", [None, "relative/repo", "/repo/../other"])
def test_invalid_authoritative_root_is_not_derived_from_task(homes, primary):
    root, repo = homes
    with sqlite3.connect(root / "profiles/director/projects.db") as conn:
        conn.execute("UPDATE projects SET primary_path=? WHERE id='p_owner'", (primary,))
    assert guard.fleet_contract_guard_reason(contract_row(repo)) == "task_project_unknown"


def test_owner_query_permission_failure_is_typed(homes, monkeypatch):
    from hermes_cli import kanban_fleet_project_registry as registry_module

    _, repo = homes
    def unavailable(*_args, **_kwargs):
        raise sqlite3.OperationalError("access denied")
    monkeypatch.setattr(registry_module.sqlite3, "connect", unavailable)
    assert guard.fleet_contract_guard_reason(contract_row(repo)) == "task_project_unknown"


def test_connection_rejects_database_write(homes, monkeypatch):
    from hermes_cli import kanban_fleet_project_registry as registry_module

    _, repo = homes
    actual_connect = sqlite3.connect
    inspected = []
    def verify_read_only(*args, **kwargs):
        conn = actual_connect(*args, **kwargs)
        try:
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                conn.execute("UPDATE projects SET name='forbidden-write' WHERE id='p_owner'")
        except BaseException:
            conn.close()
            raise
        inspected.append(True)
        return conn
    monkeypatch.setattr(registry_module.sqlite3, "connect", verify_read_only)
    assert guard.fleet_contract_guard_reason(contract_row(repo)) is None
    assert inspected == [True]

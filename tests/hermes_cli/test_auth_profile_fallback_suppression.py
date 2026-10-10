"""Profile source tombstones constrain global-root credential fallback."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _make_auth_store(pool: dict | None = None) -> dict:
    store: dict = {"version": 1}
    if pool is not None:
        store["credential_pool"] = pool
    return store


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    global_root = tmp_path / ".hermes"
    global_root.mkdir()
    profile_dir = global_root / "profiles" / "coder"
    profile_dir.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_dir))
    return {"global": global_root, "profile": profile_dir}


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2))


def test_source_suppression_filters_global_provider_slice(profile_env):
    """Removing an ambient source in a profile tombstones the borrowed root row."""
    from hermes_cli.auth import read_credential_pool

    _write(profile_env["global"] / "auth.json", _make_auth_store(pool={
        "deepseek": [{
            "id": "global-env",
            "source": "env:DEEPSEEK_API_KEY",
            "access_token": "root-secret",
        }, {
            "id": "global-manual",
            "source": "manual",
            "access_token": "manual-secret",
        }],
    }))
    profile_store = _make_auth_store(pool={"deepseek": []})
    profile_store["suppressed_sources"] = {"deepseek": ["env:DEEPSEEK_API_KEY"]}
    _write(profile_env["profile"] / "auth.json", profile_store)

    assert [row["id"] for row in read_credential_pool("deepseek")] == ["global-manual"]


def test_source_suppression_filters_global_whole_pool_read(profile_env):
    """Inventory reads expose the same effective pool as provider-slice reads."""
    from hermes_cli.auth import read_credential_pool

    _write(profile_env["global"] / "auth.json", _make_auth_store(pool={
        "deepseek": [{
            "id": "global-env",
            "source": "env:DEEPSEEK_API_KEY",
            "access_token": "root-secret",
        }],
        "openrouter": [{
            "id": "global-openrouter",
            "source": "manual",
            "access_token": "other-secret",
        }],
    }))
    profile_store = _make_auth_store(pool={})
    profile_store["suppressed_sources"] = {"deepseek": ["env:DEEPSEEK_API_KEY"]}
    _write(profile_env["profile"] / "auth.json", profile_store)

    effective = read_credential_pool()
    assert "deepseek" not in effective
    assert [row["id"] for row in effective["openrouter"]] == ["global-openrouter"]


def test_legacy_mapping_suppression_filters_global_pool(profile_env):
    """Legacy mapping-form tombstones remain effective without mutating the store."""
    from hermes_cli.auth import read_credential_pool

    _write(profile_env["global"] / "auth.json", _make_auth_store(pool={
        "copilot": [{"id": "global-gh", "source": "gh_cli", "access_token": "secret"}],
    }))
    profile_store = _make_auth_store(pool={})
    profile_store["suppressed_sources"] = {"copilot": {"gh_cli": True}}
    _write(profile_env["profile"] / "auth.json", profile_store)

    assert read_credential_pool("copilot") == []

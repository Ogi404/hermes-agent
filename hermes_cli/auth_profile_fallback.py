"""Profile-local policy for credentials borrowed from the global auth store."""

from __future__ import annotations

from typing import Any, Optional


def filter_suppressed_global_pool_entries(
    provider_id: str, entries: list[Any], auth_store: dict[str, Any],
) -> list[Any]:
    """Hide borrowed root rows whose source this profile explicitly removed.

    ``hermes auth remove`` records a profile-local source suppression so an
    ambient credential cannot immediately re-seed. Global-root fallback must
    honor the same tombstone; otherwise a named profile that removes an
    ``env:*`` or ``gh_cli`` row silently borrows the identical row from the
    default profile on its next read.
    """
    suppressed = auth_store.get("suppressed_sources")
    raw_sources = suppressed.get(provider_id) if isinstance(suppressed, dict) else None
    sources = set(raw_sources) if isinstance(raw_sources, (list, dict)) else set()
    if not sources:
        return list(entries)
    return [
        entry for entry in entries
        if not (isinstance(entry, dict) and str(entry.get("source") or "") in sources)
    ]


def read_credential_pool(provider_id: Optional[str] = None) -> dict[str, Any]:
    """Return the profile-owned pool plus permitted global-root fallback rows."""
    from hermes_cli.auth import _load_auth_store, _load_global_auth_store

    auth_store = _load_auth_store()
    pool = auth_store.get("credential_pool")
    pool = pool if isinstance(pool, dict) else {}
    global_pool = _load_global_auth_store().get("credential_pool")
    global_pool = global_pool if isinstance(global_pool, dict) else {}

    if provider_id is None:
        merged = dict(pool)
        for global_provider, global_entries in global_pool.items():
            allowed_entries = filter_suppressed_global_pool_entries(
                str(global_provider),
                global_entries if isinstance(global_entries, list) else [],
                auth_store,
            )
            existing = merged.get(global_provider)
            if allowed_entries and not (isinstance(existing, list) and existing):
                merged[global_provider] = allowed_entries
        return merged

    provider_entries = pool.get(provider_id)
    if isinstance(provider_entries, list) and provider_entries:
        return list(provider_entries)
    global_entries = global_pool.get(provider_id)
    return filter_suppressed_global_pool_entries(
        provider_id,
        global_entries if isinstance(global_entries, list) else [],
        auth_store,
    )

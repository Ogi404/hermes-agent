"""Read the deterministic fleet producer's project identity, never a worker's.

The installed reconciler creates cards with ``hermes -p director kanban``.
Its opaque project IDs therefore belong to Director's registry even though
the board is shared. Schema-2 fleet validation uses that explicit authority;
it does not enumerate profiles, infer identity from a branch/workspace, or
substitute another profile's same-slug project. Changing producer ownership
requires a coordinated change here, not a runtime fallback. No credentials,
configuration contents, or other profile state are loaded by this reader.
"""

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import stat

from hermes_constants import get_default_hermes_root, get_hermes_home


FLEET_PROJECT_REGISTRY_PROFILE = "director"


def fleet_project_record(project_id: str) -> tuple[str, str] | None:
    """Return exact immutable-ID slug/root from an existing read-only store.

    The active home is passed explicitly: a multiplexed gateway's process
    environment can still belong to its launch profile. Missing/unreadable
    stores reject without creating, initializing, migrating or substituting.
    """
    root = get_default_hermes_root(home=get_hermes_home())
    home = root / "profiles" / FLEET_PROJECT_REGISTRY_PROFILE
    path = home / "projects.db"
    try:
        if home.is_symlink() or home.parent.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
            return None
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as conn:
            record = conn.execute(
                "SELECT slug,primary_path,archived FROM projects WHERE id=?", (project_id,),
            ).fetchone()
    except (OSError, sqlite3.Error, ValueError):
        return None
    if record is None or record[2] != 0:
        return None
    slug, primary, _archived = record
    if (not isinstance(slug, str) or not slug or not isinstance(primary, str)
            or not Path(primary).is_absolute() or os.path.normpath(primary) != primary):
        return None
    return slug, primary

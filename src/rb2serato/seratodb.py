"""Small, careful edits to Serato DJ Pro 4's own SQLite library.

Only used for what the DBv2 import cannot do: the column layout of the *library view*
(container_asset_list_columns with usage = 1). The DBv2 importer fills usage = 0 from the
crate files' `ovct` entries but leaves the view on "name only". Serato must be closed;
both master.sqlite and the location database (root.sqlite) are updated identically and
backed up first.
"""

from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

LIBRARY_DIR = Path.home() / "Library" / "Application Support" / "Serato" / "Library"

# Legacy DBv2 column names -> Serato 4 column ids (as seen in container_asset_list_columns).
COLUMN_IDS = {
    "song": "name", "artist": "artist", "bpm": "bpm", "key": "key", "playCount": "dj_play_count",
    "album": "album", "length": "length_ms", "comment": "comments", "added": "time_added",
    "genre": "genre", "year": "year", "label": "label", "grouping": "grouping", "remixer": "remixer",
    "composer": "composer", "track": "track_number", "bitrate": "file_bit_rate", "filename": "file_name",
    "size": "file_size", "samplerate": "file_sample_rate", "rating": "rating",
    # the fixed icon columns, so they can be placed explicitly
    "lock": "is_missing", "status": "is_missing", "type": "type", "color": "color", "colour": "color",
    "number": "list_order", "#": "list_order", "art": "art",
}
# Columns Serato always puts first in the library view (as in a crate it creates itself).
FIXED_PREFIX = ["is_missing", "type", "color", "list_order", "art"]
VIEW_USAGE = 1
# Serato separates column names/widths with ASCII 0x1e (record separator). Beware: the
# sqlite3 command line tool prints that byte as "^^", which is not what is stored.
SEP = "\x1e"


def view_column_names(columns: Sequence[Tuple[str, str]]) -> str:
    ids = [COLUMN_IDS.get(name, name) for name, _ in columns]
    if any(i in ids for i in FIXED_PREFIX):
        # Caller placed the icon columns explicitly; only make sure the status columns exist.
        if "is_missing" not in ids:
            ids.insert(0, "is_missing")
        if "type" not in ids:  # the (usually blank) file-type icon column sits right after the status column
            ids.insert(ids.index("is_missing") + 1, "type")
        return SEP.join(ids)
    return SEP.join(FIXED_PREFIX + ids)


def backup_library(dest: Path) -> List[str]:
    dest.mkdir(parents=True, exist_ok=True)
    copied = []
    for f in LIBRARY_DIR.iterdir():
        if f.is_file() and f.name.startswith(("master.sqlite", "root.sqlite")):
            shutil.copy2(f, dest / f.name)
            copied.append(f.name)
    return copied


def set_crate_view_columns(db_path: Path, columns: Sequence[Tuple[str, str]], crate_names: Sequence[str], dry_run: bool = False) -> Dict[str, int]:
    """Set the library-view columns of the named crates (all crates if names is empty)."""
    names_value = view_column_names(columns)
    widths_value = SEP.join("0" for _ in names_value.split(SEP))
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute("SELECT id, name FROM container WHERE type = 1").fetchall()
        wanted = {n.lower() for n in crate_names}
        ids = [cid for cid, name in rows if not wanted or (name or "").lower() in wanted]
        changed = 0
        for cid in ids:
            cur = con.execute("SELECT column_names FROM container_asset_list_columns WHERE container_id = ? AND usage = ?", (cid, VIEW_USAGE)).fetchone()
            if cur and cur[0] == names_value:
                continue
            changed += 1
            if dry_run:
                continue
            if cur:
                con.execute(
                    "UPDATE container_asset_list_columns SET column_names = ?, column_widths = ?, primary_sort_column = COALESCE(primary_sort_column, 'list_order') WHERE container_id = ? AND usage = ?",
                    (names_value, widths_value, cid, VIEW_USAGE),
                )
            else:
                con.execute(
                    "INSERT INTO container_asset_list_columns (container_id, usage, column_names, column_widths, primary_sort_column, primary_sort_direction) VALUES (?, ?, ?, ?, 'list_order', 1)",
                    (cid, VIEW_USAGE, names_value, widths_value),
                )
        if not dry_run:
            con.commit()
        return {"crates": len(ids), "changed": changed}
    finally:
        con.close()


def apply_columns(columns: Sequence[Tuple[str, str]], crate_names: Sequence[str], backup_dir: Path, dry_run: bool = False) -> Dict[str, Dict[str, int]]:
    result = {}
    if not dry_run:
        backup_library(backup_dir)
    for name in ("master.sqlite", "root.sqlite"):
        path = LIBRARY_DIR / name
        if path.exists():
            result[name] = set_crate_view_columns(path, columns, crate_names, dry_run=dry_run)
    return result


def stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")

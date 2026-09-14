"""Command line: status, plan, tags, crates, columns, run, undo, verify."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import __version__
from . import convert, rbxml, rekordbox, seratodb, seratolib, tagwriter
from .model import Library, Track

log = logging.getLogger("rb2serato")
PROJECT = Path(__file__).resolve().parents[2]
STATE_DIR = PROJECT / "state"
OUT_DIR = PROJECT / "out"


def serato_is_running() -> bool:
    try:
        # -x matches the process name exactly; -f would also match any shell whose command line mentions Serato.
        out = subprocess.run(["pgrep", "-x", "Serato DJ Pro"], capture_output=True, text=True, timeout=5)
        return out.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# --------------------------------------------------------------------------- shared setup


def add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--xml", metavar="FILE", help="Rekordbox XML export to read instead of master.db (File > Export Collection in xml format)")
    p.add_argument("--db", help="path to Rekordbox master.db (default: ~/Library/Pioneer/rekordbox/master.db; needs the database key)")
    p.add_argument("--share", help="Rekordbox 'share' folder with the ANLZ analysis files (beatgrids; master.db input only)")
    p.add_argument("--serato-dir", help="Serato's _Serato_ folder for the boot volume (default: ~/Music/_Serato_)")
    p.add_argument("--memory-cues", choices=["fill", "skip", "first"], default="fill", help="memory cues: fill=take free cue slots (default), skip=leave out, first=before hot cues")
    p.add_argument("--keep-auto-names", action="store_true", help="keep Rekordbox's 'CUE(Auto)' label on automatic hot cues")
    p.add_argument("--cue-colors", choices=["rekordbox", "serato"], default="rekordbox", help="cue colours: nearest Serato colour (default) or Serato's per-slot defaults")
    p.add_argument("--no-lock-grid", action="store_true", help="do not lock the beatgrid in Serato (Serato's analysis may then replace it)")
    p.add_argument("--no-key", action="store_true", help="do not write the key to the files")
    p.add_argument("--no-bpm", action="store_true", help="do not write the BPM tag to the files")
    p.add_argument("--mp3-offset-ms", type=int, default=0, help="shift every position in MP3 files by this many milliseconds")
    p.add_argument("--unwrap", default="", metavar="FOLDER", help="lift the contents of this top-level Rekordbox folder to top-level crates")
    p.add_argument("--keep-empty", action="store_true", help="also create crates for empty playlists")
    p.add_argument("--columns", default=",".join(n for n, _ in seratolib.DEFAULT_COLUMNS),
                   help="columns of each crate, e.g. 'lock,color,number,song,artist,bpm,key,playCount,length' (name:width allowed)")
    p.add_argument("-v", "--verbose", action="store_true")


def options_from(args) -> convert.Options:
    return convert.Options(
        memory_cues=args.memory_cues,
        keep_auto_names=args.keep_auto_names,
        cue_colors=args.cue_colors,
        lock_grid=not args.no_lock_grid,
        write_key=not args.no_key,
        write_bpm=not args.no_bpm,
        mp3_offset_ms=args.mp3_offset_ms,
        unwrap=args.unwrap,
        keep_empty=args.keep_empty,
    )


def load(args) -> Library:
    if args.xml:
        return rbxml.load_xml(args.xml)
    con = rekordbox.open_db(Path(args.db) if args.db else None)
    try:
        return rekordbox.load_library(con, Path(args.share) if args.share else None)
    finally:
        con.close()


def file_tracks(lib: Library) -> List[Track]:
    return [t for t in lib.tracks.values() if t.is_file and os.path.exists(t.path)]


# --------------------------------------------------------------------------- planning


def build_plan(lib: Library, opts: convert.Options) -> Dict[str, object]:
    tracks = file_tracks(lib)
    missing = [t for t in lib.tracks.values() if t.is_file and not os.path.exists(t.path)]
    streaming = [t for t in lib.tracks.values() if not t.is_file]
    plans = {t.id: convert.plan_track(t, opts) for t in tracks}
    specs = convert.crate_specs(lib, opts)
    unsupported = [t for t in tracks if not tagwriter.file_kind(t.path)]
    stats = {
        "tracks_total": len(lib.tracks),
        "tracks_files": len(tracks),
        "tracks_missing": len(missing),
        "tracks_streaming": len(streaming),
        "tracks_unsupported_tags": len(unsupported),
        "cues": sum(p.n_cues for p in plans.values()),
        "loops": sum(p.n_loops for p in plans.values()),
        "tracks_with_cues": sum(1 for p in plans.values() if p.n_cues or p.n_loops),
        "tracks_with_grid": sum(1 for p in plans.values() if p.grid),
        "tracks_with_key": sum(1 for p in plans.values() if p.key),
        "dropped_hot": sum(p.dropped_hot for p in plans.values()),
        "dropped_memory": sum(p.dropped_memory for p in plans.values()),
        "dropped_loops": sum(p.dropped_loops for p in plans.values()),
        "crates": sum(1 for s in specs if not s.is_folder),
        "folders": sum(1 for s in specs if s.is_folder),
        "crate_entries": sum(len(s.track_ids) for s in specs),
        "by_ext": {},
    }
    for t in tracks:
        stats["by_ext"][t.ext] = stats["by_ext"].get(t.ext, 0) + 1  # type: ignore[index]
    return {"tracks": tracks, "missing": missing, "streaming": streaming, "plans": plans, "specs": specs, "stats": stats, "unsupported": unsupported}


def print_plan(plan: Dict[str, object], lib: Library) -> None:
    s = plan["stats"]
    print(f"Source: {lib.source}")
    print(f"Rekordbox: {s['tracks_total']} tracks, {s['tracks_files']} of them files on disk, "
          f"{s['tracks_missing']} missing, {s['tracks_streaming']} streaming links")
    print("File types: " + ", ".join(f"{k or '?'} {v}" for k, v in sorted(s["by_ext"].items(), key=lambda x: -x[1])))
    if s["tracks_unsupported_tags"]:
        print(f"  {s['tracks_unsupported_tags']} files cannot take Serato tags (unknown format); they still go into the crates")
    print(f"Cues: {s['cues']} cue points and {s['loops']} loops in {s['tracks_with_cues']} tracks; "
          f"beatgrid for {s['tracks_with_grid']} tracks; key for {s['tracks_with_key']} tracks")
    if s["dropped_hot"] or s["dropped_memory"] or s["dropped_loops"]:
        print(f"  No room (Serato has 8 cue slots and 8 loop slots): {s['dropped_hot']} hot cues, "
              f"{s['dropped_memory']} memory cues, {s['dropped_loops']} loops")
    print(f"Crates: {s['crates']} playlists in {s['folders']} folders, {s['crate_entries']} entries")
    print()
    for spec in plan["specs"]:
        depth = len(spec.parts) - 1
        if spec.is_folder:
            print("  " * depth + f"[{spec.parts[-1]}]")
        else:
            print("  " * depth + f"{spec.parts[-1]}  ({len(spec.track_ids)})")
    if plan["missing"]:
        print("\nMissing on disk (skipped):")
        for t in plan["missing"][:20]:
            print(f"  - {t.display}: {t.path}")
        if len(plan["missing"]) > 20:
            print(f"  ... and {len(plan['missing']) - 20} more")


def write_report(plan: Dict[str, object], opts: convert.Options, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "options": vars(opts),
        "stats": plan["stats"],
        "crates": [{"name": s.name, "folder": s.is_folder, "tracks": len(s.track_ids)} for s in plan["specs"]],
        "missing": [{"title": t.display, "path": t.path} for t in plan["missing"]],
        "notes": [{"track": p.track.display, "path": p.track.path, "notes": p.notes} for p in plan["plans"].values() if p.notes],
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- commands


def cmd_status(args) -> int:
    lib = load(args)
    opts = options_from(args)
    plan = build_plan(lib, opts)
    s = plan["stats"]
    print(f"rb2serato {__version__}")
    print(f"Rekordbox running: {'yes' if rekordbox.rekordbox_is_running() else 'no'}   Serato running: {'yes' if serato_is_running() else 'no'}")
    print(f"Tracks: {s['tracks_files']} files / {s['tracks_total']} total   Playlists: {s['crates']}   Cues: {s['cues']}   Beatgrids: {s['tracks_with_grid']}")
    sdir = Path(args.serato_dir) if args.serato_dir else seratolib.BOOT_SERATO_DIR
    sub = sdir / "Subcrates"
    n_crates = len(list(sub.glob("*.crate"))) if sub.is_dir() else 0
    db = sdir / "database V2"
    n_db = len(seratolib.parse_database(db.read_bytes())) if db.exists() else 0
    print(f"Serato ({sdir}): {n_crates} crates, {n_db} tracks in database V2")
    logs = sorted(STATE_DIR.glob("undo-*.jsonl"))
    if logs:
        print(f"Undo logs: {len(logs)}, latest {logs[-1].name}")
    return 0


def cmd_plan(args) -> int:
    lib = load(args)
    opts = options_from(args)
    plan = build_plan(lib, opts)
    print_plan(plan, lib)
    out = OUT_DIR / "plan.json"
    write_report(plan, opts, out)
    print(f"\nDetails written to {out}")
    return 0


def _payload_for(p: convert.TrackPlan, opts: convert.Options) -> tagwriter.TagPayload:
    return tagwriter.TagPayload(
        markers=p.markers,
        grid=p.grid,
        key=p.key if opts.write_key else "",
        bpm=p.bpm if (opts.write_bpm and p.bpm > 0) else 0.0,
    )


def cmd_tags(args) -> int:
    lib = load(args)
    opts = options_from(args)
    plan = build_plan(lib, opts)
    tracks: List[Track] = plan["tracks"]
    if args.match:
        needle = args.match.lower()
        tracks = [t for t in tracks if needle in t.path.lower() or needle in t.display.lower()]
    if args.limit:
        tracks = tracks[: args.limit]
    tracks = [t for t in tracks if tagwriter.file_kind(t.path)]
    print(f"{'Dry run: ' if args.dry_run else ''}writing Serato tags to {len(tracks)} files ...")
    undo = None
    if not args.dry_run:
        STATE_DIR.mkdir(exist_ok=True)
        undo = tagwriter.UndoLog(str(STATE_DIR / f"undo-{time.strftime('%Y%m%d-%H%M%S')}.jsonl")).open()
    counts = {"written": 0, "unchanged": 0, "skipped": 0, "error": 0}
    errors: List[Tuple[str, str]] = []
    t0 = time.time()
    for i, t in enumerate(tracks, 1):
        p = plan["plans"][t.id]
        try:
            result = tagwriter.apply(t.path, _payload_for(p, opts), undo=undo, dry_run=args.dry_run)
        except Exception as e:  # keep going; one odd file must not stop the run
            result = "error"
            errors.append((t.path, f"{type(e).__name__}: {e}"))
        counts[result] += 1
        if i % 250 == 0 or i == len(tracks):
            el = time.time() - t0
            print(f"  {i}/{len(tracks)}  written {counts['written']}  unchanged {counts['unchanged']}  errors {counts['error']}  ({el:.0f}s)")
    if undo:
        undo.close()
        print(f"Undo log: {undo.path}")
    if errors:
        print(f"\n{len(errors)} files failed:")
        for path, err in errors[:30]:
            print(f"  - {path}\n      {err}")
        OUT_DIR.mkdir(exist_ok=True)
        (OUT_DIR / "tag-errors.json").write_text(json.dumps(errors, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if not errors else 1


def _db_track(t: Track, p: convert.TrackPlan, opts: convert.Options) -> seratolib.DbTrack:
    real = seratolib.disk_path(t.path)
    _mount, rel = seratolib.split_volume(real)
    try:
        st_ = os.stat(real)
        mtime, size = int(st_.st_mtime), int(st_.st_size)
    except OSError:
        mtime, size = seratolib.now_epoch(), t.file_size
    added = seratolib.now_epoch()
    if t.date_added:
        try:
            added = int(dt.datetime.strptime(t.date_added[:10], "%Y-%m-%d").timestamp())
        except ValueError:
            pass
    bpm = p.bpm if p.bpm > 0 else t.bpm
    return seratolib.DbTrack(
        pfil=rel,
        ttyp=seratolib.TYPE_BY_EXT.get(t.ext, "mp3"),
        tsng=t.title,
        tart=t.artist,
        talb=t.album,
        tgen=t.genre,
        tcom=t.comment,
        tlen=seratolib.fmt_length(t.length_sec) if t.length_sec else "",
        tsiz=seratolib.fmt_size(size) if size else "",
        tbit=f"{t.bitrate:.1f}kbps" if t.bitrate else "",
        tsmp=f"{t.sample_rate / 1000:.1f}k" if t.sample_rate else "",
        tbpm=f"{bpm:.2f}" if bpm else "",
        tcmp=t.composer,
        ttyr=str(t.year) if t.year else "",
        tlbl=t.label,
        tadd=str(added),
        tkey=p.key or convert.normalize_key(t.key),
        uadd=added,
        utkn=t.track_no,
        utme=mtime,
        ufsb=size,
        udsc=t.disc_no,
        utpc=0,
        bbgl=bool(p.markers.bpm_locked),
    )


def cmd_crates(args) -> int:
    if serato_is_running() and not args.force:
        print("Serato DJ Pro is running. Quit Serato first (or use --force). Serato imports the crates on its next start.")
        return 2
    lib = load(args)
    opts = options_from(args)
    plan = build_plan(lib, opts)
    tracks: List[Track] = plan["tracks"]
    boot_dir = Path(args.serato_dir) if args.serato_dir else None

    # Group everything by volume: each volume gets its own _Serato_ folder.
    per_volume: Dict[str, Dict[str, object]] = {}
    portable: Dict[str, Tuple[str, str]] = {}  # track id -> (mount, relative path)
    for t in tracks:
        real = seratolib.disk_path(t.path)
        mount, rel = seratolib.split_volume(real)
        portable[t.id] = (mount, rel)
        vol = per_volume.setdefault(mount, {"db": [], "crates": [], "order": []})
        vol["db"].append(_db_track(t, plan["plans"][t.id], opts))  # type: ignore[union-attr]
    for spec in plan["specs"]:
        by_mount: Dict[str, List[str]] = {}
        for tid in spec.track_ids:
            mount, rel = portable[tid]
            by_mount.setdefault(mount, []).append(rel)
        for mount, vol in per_volume.items():
            vol["crates"].append((spec.name, by_mount.get(mount, [])))  # type: ignore[union-attr]
            vol["order"].append(spec.name)  # type: ignore[union-attr]

    print("Columns in each crate: " + ", ".join(n for n, _ in seratolib.parse_columns(args.columns)))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for mount, vol in per_volume.items():
        sdir = seratolib.serato_dir_for(mount, boot_dir)
        print(f"{'Dry run: ' if args.dry_run else ''}{sdir}: {len(vol['db'])} tracks in database V2, {len(vol['crates'])} crates")  # type: ignore[arg-type]
        if args.dry_run:
            continue
        backup = STATE_DIR / f"serato-backup-{stamp}" / (mount.strip('/').replace('/', '_') or "boot")
        copied = seratolib.backup_serato_dir(sdir, backup)
        print(f"  backed up {len(copied)} existing files to {backup}")
        res = seratolib.write_library(sdir, vol["db"], vol["crates"], vol["order"], seratolib.parse_columns(args.columns))  # type: ignore[arg-type]
        print(f"  written: database V2 ({res['tracks']} tracks), {res['crates']} crate files, neworder.pref")
    if not args.dry_run:
        print("\nNow start Serato DJ Pro. It notices that the library files changed and imports them automatically "
              "(the first import can take a few minutes).")
    return 0


def cmd_columns(args) -> int:
    """Set the library-view columns of the imported crates directly in Serato's SQLite."""
    if serato_is_running() and not args.force:
        print("Serato DJ Pro is running. Quit Serato first; its database must not be touched while it is open.")
        return 2
    lib = load(args)
    opts = options_from(args)
    specs = convert.crate_specs(lib, opts)
    names = [s.parts[-1] for s in specs]
    columns = seratolib.parse_columns(args.columns)
    print("Library view columns: " + ", ".join(seratodb.COLUMN_IDS.get(n, n) for n, _ in columns))
    backup = STATE_DIR / f"serato-sqlite-backup-{seratodb.stamp()}"
    res = seratodb.apply_columns(columns, names, backup, dry_run=args.dry_run)
    if not res:
        print(f"Serato's library database was not found in {seratodb.LIBRARY_DIR}")
        return 1
    for db, r in res.items():
        print(f"{'Dry run: ' if args.dry_run else ''}{db}: {r['crates']} crates found, {r['changed']} changed")
    if not args.dry_run:
        print(f"Serato database files backed up to {backup}")
    return 0


def cmd_run(args) -> int:
    rc = cmd_tags(args)
    if rc not in (0, 1):
        return rc
    rc2 = cmd_crates(args)
    return rc2 or rc


def cmd_undo(args) -> int:
    logs = [Path(args.log)] if args.log else sorted(STATE_DIR.glob("undo-*.jsonl"), reverse=True)
    if logs and not args.only_serato:
        # Newest log first, newest entry first: a file written twice ends up as it was originally.
        for log_path in logs:
            lines = log_path.read_text(encoding="utf-8").splitlines()
            print(f"Restoring tags in {len(lines)} files from {log_path.name} ...")
            done = errors = 0
            for line in reversed(lines):
                rec = json.loads(line)
                try:
                    tagwriter.undo_file(rec["path"], rec["before"])
                    done += 1
                except Exception as e:
                    errors += 1
                    print(f"  error: {rec['path']}: {e}")
            print(f"  restored {done}, errors {errors}")
            if not args.keep_log and errors == 0:
                log_path.rename(log_path.with_suffix(".jsonl.done"))
    backups = sorted(STATE_DIR.glob("serato-backup-*"))
    if backups and not args.only_tags:
        if serato_is_running():
            print("Serato is running; quit it before the _Serato_ folder is restored.")
            return 2
        b = backups[-1]
        for voldir in b.iterdir():
            mount = "/" if voldir.name == "boot" else "/" + voldir.name.replace("_", "/")
            sdir = seratolib.serato_dir_for(mount, Path(args.serato_dir) if args.serato_dir else None)
            ours = [p.stem for p in (sdir / "Subcrates").glob("*.crate")] if (sdir / "Subcrates").is_dir() else []
            keep = {p.stem for p in (voldir / "Subcrates").glob("*.crate")} if (voldir / "Subcrates").is_dir() else set()
            seratolib.restore_serato_dir(voldir, sdir, remove_crates=[n for n in ours if n not in keep])
            print(f"Restored {sdir} from {b.name}")
    return 0


def cmd_verify(args) -> int:
    lib = load(args)
    opts = options_from(args)
    plan = build_plan(lib, opts)
    tracks = [t for t in plan["tracks"] if tagwriter.file_kind(t.path)]
    if args.sample and args.sample < len(tracks):
        import random

        random.seed(args.seed)
        tracks = random.sample(tracks, args.sample)
    ok = bad = 0
    problems = []
    for t in tracks:
        p = plan["plans"][t.id]
        try:
            got = tagwriter.read_serato(t.path)
        except Exception as e:
            bad += 1
            problems.append((t.path, f"unreadable: {e}"))
            continue
        issues = []
        m2 = got["markers2"]
        if m2 is None:
            issues.append("no Markers2")
        else:
            want = {(c.index, c.position_ms, c.name, tuple(c.color)) for c in p.markers.cues}
            have = {(c.index, c.position_ms, c.name, tuple(c.color)) for c in m2.cues}
            if want != have:
                issues.append(f"cues differ ({len(want)} wanted, {len(have)} found)")
            wl = {(l.index, l.start_ms, l.end_ms) for l in p.markers.loops}
            hl = {(l.index, l.start_ms, l.end_ms) for l in m2.loops}
            if wl != hl:
                issues.append("loops differ")
            if m2.bpm_locked != p.markers.bpm_locked:
                issues.append("beatgrid lock differs")
        m1 = got["markers"]
        if m1 is not None:
            want5 = {(c.index, c.position_ms) for c in p.markers.cues if c.index < 5}
            have5 = {(c.index, c.position_ms) for c in m1.cues}
            if want5 != have5:
                issues.append("Markers_ (legacy) disagrees with Markers2")
        elif p.markers.cues:
            issues.append("no Markers_")
        if p.grid:
            g = got["grid"]
            if not g:
                issues.append("no beatgrid")
            elif len(g) != len(p.grid) or abs(g[-1].bpm - p.grid[-1].bpm) > 0.01 or abs(g[0].position_sec - p.grid[0].position_sec) > 0.001:
                issues.append("beatgrid differs")
        if p.key and opts.write_key and (got["key"] or "") != p.key:
            issues.append(f"key {got['key']!r} != {p.key!r}")
        if issues:
            bad += 1
            problems.append((t.path, "; ".join(issues)))
        else:
            ok += 1
    print(f"Verified {len(tracks)} files: {ok} ok, {bad} with differences")
    for path, issue in problems[:40]:
        print(f"  - {path}\n      {issue}")
    if args.serato_db:
        _verify_serato_sqlite(plan)
    return 0 if bad == 0 else 1


def _verify_serato_sqlite(plan) -> None:
    import shutil
    import sqlite3
    import tempfile

    src = seratodb.LIBRARY_DIR / "root.sqlite"
    if not src.exists():
        print("Serato's SQLite library (root.sqlite) does not exist yet.")
        return
    with tempfile.TemporaryDirectory() as td:
        for suffix in ("", "-wal", "-shm"):
            if (src.parent / (src.name + suffix)).exists():
                shutil.copy2(src.parent / (src.name + suffix), Path(td) / (src.name + suffix))
        con = sqlite3.connect(str(Path(td) / src.name))
        n_assets = con.execute("select count(*) from asset").fetchone()[0]
        n_containers = con.execute("select count(*) from container where type = 1").fetchone()[0]
        n_links = con.execute("select count(*) from container_asset").fetchone()[0]
        con.close()
    s = plan["stats"]
    print(f"Serato SQLite: {n_assets} tracks (expected at least {s['tracks_files']}), {n_containers} crates "
          f"(expected at least {s['crates'] + s['folders']}), {n_links} crate entries (expected {s['crate_entries']})")


# --------------------------------------------------------------------------- main


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="rb2serato", description="Move a Rekordbox library (playlists, cues, beatgrids) to Serato DJ Pro 4.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("status", help="key figures for Rekordbox and Serato")
    add_common(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("plan", help="show what would happen (changes nothing)")
    add_common(p)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("tags", help="write cues, loops, beatgrid, key and BPM into the audio files")
    add_common(p)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=0, help="only the first N files")
    p.add_argument("--match", default="", help="only files whose path or title contains this text")
    p.set_defaults(func=cmd_tags)

    p = sub.add_parser("crates", help="write database V2, crates and crate order to _Serato_ (Serato must be closed)")
    add_common(p)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="write even if Serato is running")
    p.set_defaults(func=cmd_crates)

    p = sub.add_parser("columns", help="set the library-view columns of all imported crates in Serato's database (Serato must be closed)")
    add_common(p)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_columns)

    p = sub.add_parser("run", help="tags + crates")
    add_common(p)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--match", default="")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("undo", help="restore the files' tags from the undo logs and _Serato_ from its backup")
    add_common(p)
    p.add_argument("log", nargs="?", help="a specific undo log")
    p.add_argument("--only-tags", action="store_true")
    p.add_argument("--only-serato", action="store_true")
    p.add_argument("--keep-log", action="store_true")
    p.set_defaults(func=cmd_undo)

    p = sub.add_parser("verify", help="read the tags back from the files and compare with Rekordbox")
    add_common(p)
    p.add_argument("--sample", type=int, default=0, help="random sample of N files")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--serato-db", action="store_true", help="also count what is in Serato's SQLite library")
    p.set_defaults(func=cmd_verify)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        return args.func(args)
    except rekordbox.RekordboxError as e:
        print(f"Rekordbox: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130

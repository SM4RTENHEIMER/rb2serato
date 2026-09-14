"""Read the Rekordbox 6/7 library straight out of master.db.

master.db is a SQLCipher-4 encrypted SQLite file. Given the key, we decrypt its pages in
pure Python (AES-256-CBC) into a private cache copy and read that with the stdlib sqlite3
module; the real database is only ever opened read-only, so Rekordbox can stay open.
Without the key, use the XML export instead (see rbxml.py).

Beatgrids are not in master.db - they live in the per-track analysis files (ANLZ*.DAT,
section PQTZ) under ~/Library/Pioneer/rekordbox/share, which we parse here as well.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import struct
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .model import Cue, GridMarker, Library, Node, Track

log = logging.getLogger(__name__)

DEFAULT_DB = Path.home() / "Library" / "Pioneer" / "rekordbox" / "master.db"
DEFAULT_SHARE = Path.home() / "Library" / "Pioneer" / "rekordbox" / "share"
CACHE_DIR = Path.home() / ".rb2serato" / "cache"

# SQLCipher 4 parameters (Rekordbox 6 and 7).
PAGE_SIZE = 4096
IV_SIZE = 16
RESERVE = 80  # IV + HMAC-SHA512, rounded up to the AES block size
KDF_ITER = 256_000
SQLITE_HEADER = b"SQLite format 3\x00"

# The master.db passphrase is a constant that belongs to AlphaTheta and is deliberately not
# shipped with rb2serato. Put it in ~/.rb2serato/key or the REKORDBOX_DB_KEY environment
# variable if you have it (see pyrekordbox's documentation), or simply use the XML export
# (`--xml`), which needs no key at all.
KEY_FILE = Path.home() / ".rb2serato" / "key"

# Rekordbox hot-cue colour table (djmdCue.ColorTableIndex -> RGB), as documented by the
# Deep Symmetry beat-link project. Index 0 is the default green.
CUE_COLOR_TABLE = [
    (0x28, 0xE2, 0x14), (0x30, 0x5A, 0xFF), (0x50, 0x73, 0xFF), (0x50, 0x8C, 0xFF),
    (0x50, 0xA0, 0xFF), (0x50, 0xB4, 0xFF), (0x50, 0xB0, 0xF2), (0x50, 0xAE, 0xE8),
    (0x45, 0xAC, 0xDB), (0x00, 0xE0, 0xFF), (0x19, 0xDA, 0xF0), (0x32, 0xD2, 0xE6),
    (0x21, 0xB4, 0xB9), (0x20, 0xAA, 0xA0), (0x1F, 0xA3, 0x92), (0x19, 0xA0, 0x8C),
    (0x14, 0xA5, 0x84), (0x14, 0xAA, 0x7D), (0x10, 0xB1, 0x76), (0x30, 0xD2, 0x6E),
    (0x37, 0xDE, 0x5A), (0x3C, 0xEB, 0x50), (0x28, 0xE2, 0x14), (0x7D, 0xC1, 0x3D),
    (0x8C, 0xC8, 0x32), (0x9B, 0xD7, 0x23), (0xA5, 0xE1, 0x16), (0xA5, 0xDC, 0x0A),
    (0xAA, 0xD2, 0x08), (0xB4, 0xC8, 0x05), (0xB4, 0xBE, 0x04), (0xBA, 0xB4, 0x04),
    (0xC3, 0xAF, 0x04), (0xE1, 0xAA, 0x00), (0xFF, 0xA0, 0x00), (0xFF, 0x96, 0x00),
    (0xFF, 0x8C, 0x00), (0xFF, 0x75, 0x00), (0xE0, 0x64, 0x1B), (0xE0, 0x46, 0x1E),
    (0xE0, 0x30, 0x1E), (0xE0, 0x28, 0x23), (0xE6, 0x28, 0x28), (0xFF, 0x37, 0x6F),
    (0xFF, 0x2D, 0x6F), (0xFF, 0x12, 0x7B), (0xF5, 0x1E, 0x8C), (0xEB, 0x2D, 0xA0),
    (0xE6, 0x37, 0xB4), (0xDE, 0x44, 0xCF), (0xDE, 0x44, 0x8D), (0xE6, 0x30, 0xB4),
    (0xE6, 0x19, 0xDC), (0xE6, 0x00, 0xFF), (0xDC, 0x00, 0xFF), (0xCC, 0x00, 0xFF),
    (0xB4, 0x32, 0xFF), (0xB9, 0x3C, 0xFF), (0xC5, 0x42, 0xFF), (0xAA, 0x5A, 0xFF),
    (0xAA, 0x72, 0xFF), (0x82, 0x72, 0xFF), (0x64, 0x73, 0xFF),
]

# Rekordbox track colours (djmdContent.ColorID 1..8).
TRACK_COLORS = {
    1: (0xFF, 0x00, 0x7F),  # Pink
    2: (0xFF, 0x00, 0x00),  # Red
    3: (0xFF, 0xA5, 0x00),  # Orange
    4: (0xFF, 0xFF, 0x00),  # Yellow
    5: (0x00, 0xFF, 0x00),  # Green
    6: (0x25, 0xFD, 0xE9),  # Aqua
    7: (0x00, 0x00, 0xFF),  # Blue
    8: (0x66, 0x00, 0x99),  # Purple
}


class RekordboxError(RuntimeError):
    pass


# --------------------------------------------------------------------------- decryption


def db_passphrase() -> str:
    key = os.environ.get("REKORDBOX_DB_KEY", "").strip()
    if not key and KEY_FILE.exists():
        key = KEY_FILE.read_text(encoding="utf-8").strip()
    if not key:
        raise RekordboxError(
            "No Rekordbox database key available. Either export your collection from Rekordbox "
            "(File > Export Collection in xml format) and run rb2serato with --xml <file>, or put "
            f"the key in {KEY_FILE} / REKORDBOX_DB_KEY."
        )
    return key


def rekordbox_is_running() -> bool:
    try:
        out = subprocess.run(["pgrep", "-x", "rekordbox"], capture_output=True, text=True, timeout=5)
        return out.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _wal_checksum(data: bytes, s0: int, s1: int, big_endian: bool) -> Tuple[int, int]:
    n = len(data) // 4
    vals = struct.unpack((">" if big_endian else "<") + "I" * n, data[: n * 4])
    for i in range(0, n - 1, 2):
        s0 = (s0 + vals[i] + s1) & 0xFFFFFFFF
        s1 = (s1 + vals[i + 1] + s0) & 0xFFFFFFFF
    return s0, s1


def _read_wal(wal_path: Path) -> Tuple[dict, int]:
    """Collect committed pages from master.db-wal (Rekordbox runs in WAL mode, so the
    newest edits - cues you set five minutes ago - live only here while it is open)."""
    blob = wal_path.read_bytes()
    if len(blob) < 32:
        return {}, 0
    magic, _fmt, page_size, _ckpt, salt1, salt2, ck1, ck2 = struct.unpack(">8I", blob[:32])
    if magic not in (0x377F0682, 0x377F0683) or page_size != PAGE_SIZE:
        log.warning("Ignoring %s: unexpected WAL header", wal_path.name)
        return {}, 0
    big_endian = bool(magic & 1)
    if _wal_checksum(blob[:24], 0, 0, big_endian) != (ck1, ck2):
        log.warning("Ignoring %s: bad WAL header checksum", wal_path.name)
        return {}, 0
    frame_size = 24 + page_size
    pages: Dict[int, bytes] = {}
    committed: Dict[int, bytes] = {}
    db_pages = 0
    s0, s1 = ck1, ck2
    for offset in range(32, len(blob) - frame_size + 1, frame_size):
        header = blob[offset : offset + 24]
        pgno, size_after, fs1, fs2, fc1, fc2 = struct.unpack(">6I", header)
        page = blob[offset + 24 : offset + frame_size]
        if (fs1, fs2) != (salt1, salt2):
            break
        s0, s1 = _wal_checksum(header[:8], s0, s1, big_endian)
        s0, s1 = _wal_checksum(page, s0, s1, big_endian)
        if (s0, s1) != (fc1, fc2):
            break
        pages[pgno] = page
        if size_after:
            committed.update(pages)
            pages.clear()
            db_pages = size_after
    return committed, db_pages


def decrypt_database(src: Path, dest: Path, wal: Optional[Path] = None) -> Path:
    raw = src.read_bytes()
    if raw[:16] == SQLITE_HEADER:
        dest.write_bytes(raw)
        return dest
    if len(raw) < PAGE_SIZE or len(raw) % PAGE_SIZE:
        raise RekordboxError(f"{src} is {len(raw)} bytes, not a whole number of {PAGE_SIZE}-byte pages")
    overlay, wal_pages = ({}, 0)
    if wal is not None and wal.exists():
        overlay, wal_pages = _read_wal(wal)
    salt = raw[:16]
    key = hashlib.pbkdf2_hmac("sha512", db_passphrase().encode(), salt, KDF_ITER, 32)
    aes = algorithms.AES(key)
    body_end = PAGE_SIZE - RESERVE
    total_pages = wal_pages or len(raw) // PAGE_SIZE
    out = bytearray(total_pages * PAGE_SIZE)
    for page_no in range(1, total_pages + 1):
        start = (page_no - 1) * PAGE_SIZE
        page = overlay.get(page_no) or raw[start : start + PAGE_SIZE]
        if len(page) != PAGE_SIZE or not any(page[:body_end]):
            out[start : start + PAGE_SIZE] = page.ljust(PAGE_SIZE, b"\x00")
            continue
        iv = page[body_end : body_end + IV_SIZE]
        offset = 16 if page_no == 1 else 0
        dec = Cipher(aes, modes.CBC(iv)).decryptor()
        plain = dec.update(page[offset:body_end]) + dec.finalize()
        if page_no == 1:
            plain = SQLITE_HEADER + plain
        out[start : start + PAGE_SIZE] = plain + b"\x00" * RESERVE
    if bytes(out[:16]) != SQLITE_HEADER or int.from_bytes(out[16:18], "big") != PAGE_SIZE:
        raise RekordboxError(
            "Decryption produced garbage - the Rekordbox database key may have changed "
            "in this version. Export the collection as XML from Rekordbox instead."
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    with open(tmp, "wb") as fh:
        fh.write(bytes(out))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, dest)
    con = sqlite3.connect(str(dest))
    try:
        con.execute("PRAGMA journal_mode=DELETE")
    finally:
        con.close()
    for sidecar in (dest.with_name(dest.name + "-wal"), dest.with_name(dest.name + "-shm")):
        sidecar.unlink(missing_ok=True)
    return dest


def open_db(db_path: Optional[Path] = None, refresh: bool = False) -> sqlite3.Connection:
    src = Path(db_path) if db_path else DEFAULT_DB
    if not src.exists():
        raise RekordboxError(f"Rekordbox database not found at {src}")
    wal = src.with_name(src.name + "-wal")
    stat = src.stat()
    fingerprint = [str(int(stat.st_mtime)), str(stat.st_size)]
    if wal.exists():
        wstat = wal.stat()
        fingerprint += [str(int(wstat.st_mtime)), str(wstat.st_size)]
    cached = CACHE_DIR / ("master-" + "-".join(fingerprint) + ".db")
    if refresh or not cached.exists():
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        for stale in CACHE_DIR.glob("master-*"):
            stale.unlink(missing_ok=True)
        log.info("Decrypting %s (%.0f MB)...", src.name, stat.st_size / 1e6)
        decrypt_database(src, cached, wal=wal)
    con = sqlite3.connect(f"file:{cached}?mode=ro", uri=True)
    return con


# --------------------------------------------------------------------------- ANLZ (beatgrid)


def read_pqtz(path: Path) -> List[Tuple[int, int, int]]:
    """Return the beat list [(beat_in_bar, bpm_x100, time_ms), ...] from an ANLZ .DAT file."""
    try:
        b = path.read_bytes()
    except OSError:
        return []
    if b[:4] != b"PMAI" or len(b) < 12:
        return []
    header_len = struct.unpack(">I", b[4:8])[0]
    off = header_len
    while off + 12 <= len(b):
        tag = b[off : off + 4]
        _shl, sl = struct.unpack(">II", b[off + 4 : off + 12])
        if sl <= 0:
            break
        if tag == b"PQTZ":
            data = b[off : off + sl]
            if len(data) < 24:
                return []
            n = struct.unpack(">I", data[20:24])[0]
            beats = []
            for i in range(n):
                chunk = data[24 + 8 * i : 32 + 8 * i]
                if len(chunk) < 8:
                    break
                beats.append(struct.unpack(">HHI", chunk))
            return beats
        off += sl
    return []


def beats_to_markers(beats: List[Tuple[int, int, int]]) -> List[GridMarker]:
    """Compress a per-beat list into tempo-change markers.

    Rekordbox stores every beat with its tempo; Serato wants one marker per constant-tempo
    region, with the number of beats until the next marker. The first marker is moved to the
    first downbeat (beat 1) of the first region so Serato's bar lines land where Rekordbox's do.
    """
    if not beats:
        return []
    segments: List[Tuple[int, int]] = []  # (start index, end index exclusive)
    start = 0
    for i in range(1, len(beats) + 1):
        if i == len(beats) or beats[i][1] != beats[start][1]:
            segments.append((start, i))
            start = i
    markers: List[GridMarker] = []
    for seg_no, (s, e) in enumerate(segments):
        if seg_no == 0:
            # Prefer the first downbeat of the region as the anchor.
            for j in range(s, e):
                if beats[j][0] == 1:
                    s = j
                    break
        beat_no, tempo, time_ms = beats[s]
        bpm = tempo / 100.0
        if bpm <= 0:
            continue
        markers.append(GridMarker(time_ms=float(time_ms), bpm=bpm, beat=int(beat_no), beats_to_next=e - s))
    if markers:
        markers[-1].beats_to_next = 0
    return markers


# --------------------------------------------------------------------------- loading


_CONTENT_SQL = """
SELECT c.ID, c.FolderPath, c.Title, c.BPM, c.Length, c.TrackNo, c.DiscNo, c.BitRate,
       c.SampleRate, c.FileSize, c.Commnt, c.Rating, c.ReleaseYear, c.ColorID, c.DJPlayCount,
       c.AnalysisDataPath, c.StockDate, c.DateCreated,
       ar.Name, al.Name, ge.Name, ky.ScaleName, lb.Name, rm.Name, cp.Name
  FROM djmdContent c
  LEFT JOIN djmdArtist ar ON ar.ID = c.ArtistID
  LEFT JOIN djmdAlbum  al ON al.ID = c.AlbumID
  LEFT JOIN djmdGenre  ge ON ge.ID = c.GenreID
  LEFT JOIN djmdKey    ky ON ky.ID = c.KeyID
  LEFT JOIN djmdLabel  lb ON lb.ID = c.LabelID
  LEFT JOIN djmdArtist rm ON rm.ID = c.RemixerID
  LEFT JOIN djmdArtist cp ON cp.ID = c.ComposerID
 WHERE c.rb_local_deleted = 0
"""


def kind_to_slot(kind: int) -> Optional[int]:
    """djmdCue.Kind -> 0-based hot cue slot. 0 = memory cue. Rekordbox numbers hot cues
    1,2,3,5,6,7,8,9,... (4 is never used), so A=1, B=2, C=3, D=5, E=6, F=7, G=8, H=9."""
    if kind <= 0:
        return None
    return kind - 1 if kind <= 3 else kind - 2


def _cue_color(color: Optional[int], table_index: Optional[int]):
    if color is None or color < 0 or table_index is None:
        return None
    if 0 <= table_index < len(CUE_COLOR_TABLE):
        return CUE_COLOR_TABLE[table_index]
    return None


def load_library(con: sqlite3.Connection, share: Optional[Path] = None, with_grid: bool = True) -> Library:
    share = Path(share) if share else DEFAULT_SHARE
    tracks: Dict[str, Track] = {}
    anlz: Dict[str, str] = {}
    for r in con.execute(_CONTENT_SQL):
        (cid, path, title, bpm, length, trackno, discno, bitrate, samplerate, filesize, comment,
         rating, year, color_id, playcount, anlz_path, stock, created,
         artist, album, genre, key, label, remixer, composer) = r
        t = Track(
            id=str(cid),
            path=(path or "").strip(),
            title=(title or "").strip(),
            artist=(artist or "").strip(),
            album=(album or "").strip(),
            genre=(genre or "").strip(),
            composer=(composer or "").strip(),
            comment=(comment or "").strip(),
            label=(label or "").strip(),
            remixer=(remixer or "").strip(),
            year=int(year or 0),
            track_no=int(trackno or 0),
            disc_no=int(discno or 0),
            length_sec=float(length or 0),
            bpm=round((bpm or 0) / 100.0, 2),
            key=(key or "").strip(),
            bitrate=int(bitrate or 0),
            sample_rate=int(samplerate or 0),
            file_size=int(filesize or 0),
            color_id=int(color_id or 0),
            rating=int(rating or 0),
            play_count=int(playcount or 0),
            date_added=str(stock or created or "")[:10],
        )
        tracks[t.id] = t
        if anlz_path:
            anlz[t.id] = anlz_path

    rows = con.execute(
        "SELECT ContentID, Kind, InMsec, OutMsec, Color, ColorTableIndex, Comment "
        "FROM djmdCue WHERE rb_local_deleted = 0 ORDER BY ContentID, Kind, InMsec"
    )
    for cid, kind, in_ms, out_ms, color, table_index, comment in rows:
        t = tracks.get(str(cid))
        if t is None or in_ms is None or in_ms < 0:
            continue
        end = int(out_ms) if out_ms is not None and out_ms > in_ms else None
        t.cues.append(
            Cue(
                start_ms=int(in_ms),
                end_ms=end,
                slot=kind_to_slot(int(kind or 0)),
                name=(comment or "").strip(),
                rgb=_cue_color(color, table_index),
            )
        )

    if with_grid:
        for tid, rel in anlz.items():
            dat = share / rel.lstrip("/")
            tracks[tid].grid = beats_to_markers(read_pqtz(dat))

    root = _playlist_tree(con)
    return Library(tracks=tracks, root=root, source="rekordbox master.db")


def _playlist_tree(con: sqlite3.Connection) -> Node:
    nodes: Dict[str, Node] = {}
    parent_of: Dict[str, str] = {}
    seq_of: Dict[str, int] = {}
    for pid, seq, name, attr, parent in con.execute(
        "SELECT ID, Seq, Name, Attribute, ParentID FROM djmdPlaylist WHERE rb_local_deleted = 0"
    ):
        pid = str(pid)
        nodes[pid] = Node(id=pid, name=(name or "").strip() or "Untitled", is_folder=int(attr or 0) == 1)
        parent_of[pid] = str(parent or "root")
        seq_of[pid] = int(seq or 0)
    root = Node(id="root", name="", is_folder=True)
    for pid, node in nodes.items():
        parent = nodes.get(parent_of[pid], root) if parent_of[pid] != "root" else root
        if parent is node:
            parent = root
        parent.children.append(node)
    for node in list(nodes.values()) + [root]:
        node.children.sort(key=lambda n: (seq_of.get(n.id, 0), n.name.lower()))
    for pid, cid, _trackno in con.execute(
        "SELECT PlaylistID, ContentID, TrackNo FROM djmdSongPlaylist WHERE rb_local_deleted = 0 "
        "ORDER BY PlaylistID, TrackNo"
    ):
        node = nodes.get(str(pid))
        if node is not None:
            node.track_ids.append(str(cid))
    return root

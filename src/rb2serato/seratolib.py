"""Serato's legacy library files ("DBv2"): database V2, Subcrates/*.crate, neworder.pref.

Serato DJ Pro 4 keeps its real library in SQLite, but it still exports this format into
~/Music/_Serato_ and - crucially - re-imports it automatically when it notices the files
changed since it last saw them ("RunningDjLibraryAutomaticImport"). Writing these files
while Serato is closed is therefore the supported way in for third-party tools.

Formats: a stream of chunks <4-byte tag><u32 BE length><payload>. Tags starting with
't'/'p' hold UTF-16BE text, 'u' a u32, 'b' a boolean byte, 'o' a nested chunk list.
"""

from __future__ import annotations

import os
import shutil
import struct
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

DB_VERSION = "2.0/Serato Scratch LIVE Database"
CRATE_VERSION = "1.0/Serato ScratchLive Crate"  # Serato 4 rejects any other header text
BOOT_SERATO_DIR = Path.home() / "Music" / "_Serato_"

# ttyp values Serato DJ Pro 4's importer accepts (from its binary): mp3, quicktime, wave, aiff, flac, oggvorbis...
TYPE_BY_EXT = {
    ".mp3": "mp3", ".m4a": "quicktime", ".mp4": "quicktime", ".aac": "quicktime", ".m4b": "quicktime",
    ".wav": "wave", ".aif": "aiff", ".aiff": "aiff", ".flac": "flac", ".ogg": "oggvorbis",
}


def _chunk(tag: str, payload: bytes) -> bytes:
    return tag.encode("ascii") + struct.pack(">I", len(payload)) + payload


def _text(tag: str, value: str) -> bytes:
    return _chunk(tag, value.encode("utf-16-be"))


def _u32(tag: str, value: int) -> bytes:
    return _chunk(tag, struct.pack(">I", max(0, min(0xFFFFFFFF, int(value)))))


def _bool(tag: str, value: bool) -> bytes:
    return _chunk(tag, b"\x01" if value else b"\x00")


def iter_chunks(data: bytes) -> Iterable[Tuple[str, bytes]]:
    off = 0
    while off + 8 <= len(data):
        tag = data[off : off + 4].decode("latin1")
        (length,) = struct.unpack(">I", data[off + 4 : off + 8])
        yield tag, data[off + 8 : off + 8 + length]
        off += 8 + length


def decode_value(tag: str, payload: bytes):
    kind = tag[0]
    if kind in "tp" or tag == "vrsn":
        return payload.decode("utf-16-be", "replace")
    if kind == "u" and len(payload) == 4:
        return struct.unpack(">I", payload)[0]
    if kind == "s" and len(payload) == 2:
        return struct.unpack(">H", payload)[0]
    if kind == "b" and len(payload) == 1:
        return bool(payload[0])
    if kind == "o":
        return [(t, decode_value(t, p)) for t, p in iter_chunks(payload)]
    return payload


# --------------------------------------------------------------------------- database V2


@dataclass
class DbTrack:
    """One `otrk` record, fields in the order Serato DJ Pro 4 writes them."""

    pfil: str  # path relative to the volume root, no leading slash
    ttyp: str = "mp3"
    tsng: str = ""
    tart: str = ""
    talb: str = ""
    tgen: str = ""
    tcom: str = ""  # comment
    tlen: str = ""  # "MM:SS.hh"
    tsiz: str = ""  # "5.8MB"
    tbit: str = ""  # "320.0kbps"
    tsmp: str = ""  # "44.1k"
    tbpm: str = ""  # "124.02"
    tcmp: str = ""  # composer
    ttyr: str = ""  # year
    tlbl: str = ""  # label
    tadd: str = ""  # added, epoch seconds as text
    tkey: str = ""
    uadd: int = 0
    utkn: int = 0
    utme: int = 0  # file mtime
    ufsb: int = 0  # file size in bytes
    udsc: int = 0
    utpc: int = 0
    bply: bool = False  # played
    bbgl: bool = False  # beatgrid locked

    def encode(self) -> bytes:
        parts = [_text("ttyp", self.ttyp), _text("pfil", self.pfil)]
        for tag in ("tsng", "tart", "talb", "tgen", "tcom", "tlen", "tsiz", "tbit", "tsmp", "tbpm", "tcmp", "ttyr", "tlbl", "tadd", "tkey"):
            val = getattr(self, tag)
            if val:
                parts.append(_text(tag, val))
        parts.append(_u32("uadd", self.uadd))
        if self.utkn:
            parts.append(_u32("utkn", self.utkn))
        parts.append(_u32("utme", self.utme))
        parts.append(_u32("ufsb", self.ufsb))
        if self.udsc:
            parts.append(_u32("udsc", self.udsc))
        parts.append(_u32("utpc", self.utpc))
        for tag, val in (
            ("bhrt", False), ("bmis", False), ("bply", self.bply), ("blop", False), ("bitu", False),
            ("bovc", True), ("bcrt", False), ("biro", False), ("bwlb", False), ("bwll", False),
            ("buns", False), ("bbgl", self.bbgl), ("bkrk", False),
        ):
            parts.append(_bool(tag, val))
        return _chunk("otrk", b"".join(parts))


def encode_database(tracks: Sequence[DbTrack]) -> bytes:
    return _text("vrsn", DB_VERSION) + b"".join(t.encode() for t in tracks)


def parse_database(data: bytes) -> List[Dict[str, object]]:
    out = []
    for tag, payload in iter_chunks(data):
        if tag == "otrk":
            out.append({t: decode_value(t, p) for t, p in iter_chunks(payload)})
    return out


def fmt_length(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    m = int(seconds // 60)
    s = seconds - m * 60
    return f"{m:02d}:{s:05.2f}"


def fmt_size(nbytes: int) -> str:
    mb = nbytes / 1048576.0
    return f"{mb:.1f}MB" if mb >= 1 else f"{nbytes / 1024.0:.1f}KB"


# --------------------------------------------------------------------------- crates


# Columns Serato DJ Pro 4 shows in a new crate, as legacy DBv2 column names with the
# widths Serato itself exports. Without `ovct` entries an imported crate shows only the title.
DEFAULT_COLUMNS: List[Tuple[str, str]] = [
    ("song", "250"), ("artist", "250"), ("bpm", "30"), ("key", "30"), ("playCount", "30"),
    ("album", "250"), ("length", "250"), ("comment", "250"),
]
KNOWN_COLUMNS = ["song", "artist", "bpm", "key", "playCount", "album", "length", "comment", "added",
                 "genre", "year", "label", "grouping", "remixer", "composer", "track", "bitrate", "filename"]


def parse_columns(spec: str) -> List[Tuple[str, str]]:
    """'song,artist,bpm:40,key' -> [(name, width), ...]; missing widths use Serato's defaults."""
    defaults = dict(DEFAULT_COLUMNS)
    out = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        name, _, width = item.partition(":")
        out.append((name.strip(), width.strip() or defaults.get(name.strip(), "250")))
    return out


def encode_crate(paths: Sequence[str], columns: Optional[Sequence[Tuple[str, str]]] = None) -> bytes:
    out = [_text("vrsn", CRATE_VERSION)]
    cols = DEFAULT_COLUMNS if columns is None else columns
    for name, width in cols:
        out.append(_chunk("ovct", _text("tvcn", name) + _text("tvcw", str(width))))
    for p in paths:
        out.append(_chunk("otrk", _text("ptrk", p)))
    return b"".join(out)


def parse_crate_columns(data: bytes) -> List[Tuple[str, str]]:
    cols = []
    for tag, payload in iter_chunks(data):
        if tag == "ovct":
            fields = {t: p.decode("utf-16-be", "replace") for t, p in iter_chunks(payload)}
            cols.append((fields.get("tvcn", ""), fields.get("tvcw", "")))
    return cols


def parse_crate(data: bytes) -> List[str]:
    paths = []
    for tag, payload in iter_chunks(data):
        if tag == "otrk":
            for t, p in iter_chunks(payload):
                if t == "ptrk":
                    paths.append(p.decode("utf-16-be", "replace"))
    return paths


def encode_neworder(crate_names: Sequence[str]) -> bytes:
    lines = ["[begin record]"] + [f"[crate]{n}" for n in crate_names] + ["[end record]"]
    return ("\n".join(lines) + "\n").encode("utf-16-be")


def parse_neworder(data: bytes) -> List[str]:
    text = data.decode("utf-16-be", "replace") if not data.startswith((b"\xff\xfe", b"\xfe\xff")) else data.decode("utf-16", "replace")
    return [line[len("[crate]") :] for line in text.splitlines() if line.startswith("[crate]")]


# --------------------------------------------------------------------------- paths


_dir_cache: Dict[str, Dict[str, str]] = {}


def disk_path(path: str) -> str:
    """Return the path spelled exactly as the file system stores it.

    APFS looks names up normalization-insensitively, so Rekordbox's string may use
    precomposed characters (NFC) while the directory entry is decomposed (NFD), or vice
    versa. Serato matches crates to files by the exact string, so we use the on-disk one.
    """
    parts = path.split("/")
    cur = "/"
    resolved = [""]
    for part in parts[1:]:
        if not part:
            continue
        listing = _dir_cache.get(cur)
        if listing is None:
            try:
                listing = {unicodedata.normalize("NFC", n): n for n in os.listdir(cur)}
            except OSError:
                listing = {}
            _dir_cache[cur] = listing
        actual = listing.get(unicodedata.normalize("NFC", part), part)
        resolved.append(actual)
        cur = "/".join(resolved) or "/"
    return "/".join(resolved)


def split_volume(path: str) -> Tuple[str, str]:
    """('/', 'Users/x/track.mp3') or ('/Volumes/DJ', 'Music/track.mp3')."""
    if path.startswith("/Volumes/"):
        rest = path[len("/Volumes/") :]
        name, _, rel = rest.partition("/")
        return "/Volumes/" + name, rel
    return "/", path.lstrip("/")


def serato_dir_for(mount: str, boot_dir: Optional[Path] = None) -> Path:
    if mount == "/":
        return Path(boot_dir) if boot_dir else BOOT_SERATO_DIR
    return Path(mount) / "_Serato_"


# --------------------------------------------------------------------------- writing


def backup_serato_dir(serato_dir: Path, dest: Path) -> List[str]:
    """Copy database V2, neworder.pref and Subcrates/* to dest. Returns copied names."""
    copied = []
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("database V2", "neworder.pref"):
        src = serato_dir / name
        if src.exists():
            shutil.copy2(src, dest / name)
            copied.append(name)
    sub = serato_dir / "Subcrates"
    if sub.is_dir():
        (dest / "Subcrates").mkdir(exist_ok=True)
        for f in sub.iterdir():
            if f.is_file():
                shutil.copy2(f, dest / "Subcrates" / f.name)
                copied.append("Subcrates/" + f.name)
    return copied


def restore_serato_dir(backup: Path, serato_dir: Path, remove_crates: Iterable[str] = ()) -> None:
    for name in remove_crates:
        p = serato_dir / "Subcrates" / (name + ".crate")
        if p.exists():
            p.unlink()
    for name in ("database V2", "neworder.pref"):
        src = backup / name
        dst = serato_dir / name
        if src.exists():
            shutil.copy2(src, dst)
        elif dst.exists():
            dst.unlink()
    sub = backup / "Subcrates"
    if sub.is_dir():
        (serato_dir / "Subcrates").mkdir(exist_ok=True)
        for f in sub.iterdir():
            shutil.copy2(f, serato_dir / "Subcrates" / f.name)


def write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".rb2serato-tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def write_library(
    serato_dir: Path,
    db_tracks: Sequence[DbTrack],
    crates: Sequence[Tuple[str, Sequence[str]]],
    order: Sequence[str],
    columns: Optional[Sequence[Tuple[str, str]]] = None,
) -> Dict[str, int]:
    """Write database V2 + crates + neworder.pref into one _Serato_ folder.

    Existing crates that we do not know about are left alone (and kept in neworder.pref).
    """
    serato_dir.mkdir(parents=True, exist_ok=True)
    sub = serato_dir / "Subcrates"
    sub.mkdir(exist_ok=True)
    write_atomic(serato_dir / "database V2", encode_database(db_tracks))
    written = 0
    ours = set()
    for name, paths in crates:
        write_atomic(sub / (name + ".crate"), encode_crate(paths, columns))
        ours.add(name)
        written += 1
    existing = []
    old = serato_dir / "neworder.pref"
    if old.exists():
        try:
            existing = [n for n in parse_neworder(old.read_bytes()) if n not in ours]
        except Exception:
            existing = []
    write_atomic(old, encode_neworder(list(order) + existing))
    return {"tracks": len(db_tracks), "crates": written}


def now_epoch() -> int:
    return int(time.time())

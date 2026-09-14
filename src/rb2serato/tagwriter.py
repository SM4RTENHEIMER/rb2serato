"""Write (and undo) Serato tags in audio files with mutagen.

Every file is modified on an APFS clone that is then atomically renamed over the
original, so a crash can never leave a half-written track. Before touching a file we
snapshot the tags we are about to replace into an undo log (JSON lines), which
`rb2serato undo` can replay.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import struct
import subprocess
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from mutagen.aiff import AIFF
from mutagen.id3 import GEOB, TBPM, TKEY
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, AtomDataType, MP4FreeForm
from mutagen.wave import WAVE

from . import seratotags as st

ID3_EXTS = {".mp3", ".aif", ".aiff", ".wav"}
MP4_EXTS = {".m4a", ".mp4", ".aac", ".m4b"}
SUPPORTED_EXTS = ID3_EXTS | MP4_EXTS

GEOB_MARKERS2 = "GEOB:Serato Markers2"
GEOB_MARKERS = "GEOB:Serato Markers_"
GEOB_BEATGRID = "GEOB:Serato BeatGrid"
GEOB_AUTOTAGS = "GEOB:Serato Autotags"
ID3_MANAGED = [GEOB_MARKERS2, GEOB_MARKERS, GEOB_BEATGRID, GEOB_AUTOTAGS, "TKEY", "TBPM"]

ATOM_MARKERS2 = "----:com.serato.dj:markersv2"
ATOM_MARKERS = "----:com.serato.dj:markers"
ATOM_BEATGRID = "----:com.serato.dj:beatgrid"
ATOM_AUTOTAGS = "----:com.serato.dj:autgain"
ATOM_KEY = "----:com.apple.iTunes:initialkey"
ATOM_BPM = "tmpo"
MP4_MANAGED = [ATOM_MARKERS2, ATOM_MARKERS, ATOM_BEATGRID, ATOM_AUTOTAGS, ATOM_KEY, ATOM_BPM]


class TagError(RuntimeError):
    pass


@dataclass
class TagPayload:
    markers: st.SeratoMarkers
    grid: List[st.BeatGridMarker]
    key: str = ""  # "" = leave the key alone
    bpm: float = 0.0  # 0 = leave BPM alone


def file_kind(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in ID3_EXTS:
        return "id3"
    if ext in MP4_EXTS:
        return "mp4"
    return ""


def _open(path: str):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".mp3":
        return MP3(path)
    if ext in (".aif", ".aiff"):
        return AIFF(path)
    if ext == ".wav":
        return WAVE(path)
    if ext in MP4_EXTS:
        return MP4(path)
    raise TagError(f"unsupported file type: {path}")


def _bpm_text(bpm: float) -> str:
    return f"{bpm:.2f}".rstrip("0").rstrip(".") if bpm else ""


# --------------------------------------------------------------------------- snapshots


def snapshot(path: str) -> Dict[str, object]:
    """Current values of every tag we manage, as JSON-safe data (None = absent)."""
    f = _open(path)
    kind = file_kind(path)
    out: Dict[str, object] = {}
    tags = f.tags
    if kind == "id3":
        for key in ID3_MANAGED:
            frames = tags.getall(key) if tags is not None else []
            if not frames:
                out[key] = None
            elif key.startswith("GEOB"):
                fr = frames[0]
                out[key] = {"enc": int(fr.encoding), "mime": fr.mime, "data": base64.b64encode(bytes(fr.data)).decode()}
            else:
                out[key] = {"enc": int(frames[0].encoding), "text": list(frames[0].text)}
    else:
        for key in MP4_MANAGED:
            vals = tags.get(key) if tags is not None else None
            if not vals:
                out[key] = None
            elif key == ATOM_BPM:
                out[key] = int(vals[0])
            else:
                v = vals[0]
                out[key] = {"fmt": int(getattr(v, "dataformat", 1)), "data": base64.b64encode(bytes(v)).decode()}
    return out


def intended(path: str, payload: TagPayload, current: Optional[Dict[str, object]] = None) -> Dict[str, object]:
    """What snapshot() would return after apply(); used to skip files that are already done."""
    kind = file_kind(path)
    cur = current if current is not None else snapshot(path)
    out: Dict[str, object] = dict(cur)
    if kind == "id3":
        out[GEOB_MARKERS2] = {"enc": 0, "mime": "application/octet-stream", "data": base64.b64encode(st.encode_markers2_id3(payload.markers)).decode()}
        out[GEOB_MARKERS] = {"enc": 0, "mime": "application/octet-stream", "data": base64.b64encode(st.encode_markers_id3(payload.markers)).decode()}
        out[GEOB_BEATGRID] = {"enc": 0, "mime": "application/octet-stream", "data": base64.b64encode(st.encode_beatgrid(payload.grid)).decode()} if payload.grid else None
        at = cur.get(GEOB_AUTOTAGS)
        if at and payload.bpm:
            new = st.autotags_with_bpm(base64.b64decode(at["data"]), payload.bpm)  # type: ignore[index]
            if new is not None:
                out[GEOB_AUTOTAGS] = {"enc": at["enc"], "mime": at["mime"], "data": base64.b64encode(new).decode()}  # type: ignore[index]
        if payload.key:
            out["TKEY"] = {"enc": 0 if payload.key.isascii() else 3, "text": [payload.key]}
        if payload.bpm:
            out["TBPM"] = {"enc": 0, "text": [_bpm_text(payload.bpm)]}
    else:
        out[ATOM_MARKERS2] = {"fmt": 1, "data": base64.b64encode(st.encode_markers2_mp4(payload.markers)).decode()}
        out[ATOM_MARKERS] = {"fmt": 1, "data": base64.b64encode(st.encode_markers_mp4(payload.markers)).decode()}
        out[ATOM_BEATGRID] = {"fmt": 1, "data": base64.b64encode(st.encode_beatgrid_mp4(payload.grid)).decode()} if payload.grid else None
        at = cur.get(ATOM_AUTOTAGS)
        if at and payload.bpm:
            try:
                inner = st.mp4_unwrap(base64.b64decode(at["data"]), "Serato Autotags")  # type: ignore[index]
                new = st.autotags_with_bpm(inner, payload.bpm)
                if new is not None:
                    out[ATOM_AUTOTAGS] = {"fmt": 1, "data": base64.b64encode(st.mp4_wrap("Serato Autotags", new)).decode()}
            except ValueError:
                pass
        if payload.key:
            out[ATOM_KEY] = {"fmt": 1, "data": base64.b64encode(payload.key.encode("utf-8")).decode()}
        if payload.bpm:
            out[ATOM_BPM] = int(round(payload.bpm))
    return out


def _apply_state(tags, kind: str, state: Dict[str, object], file_obj) -> None:
    """Make the managed tags look exactly like `state` (None deletes)."""
    if kind == "id3":
        for key in ID3_MANAGED:
            tags.delall(key)
            val = state.get(key)
            if val is None:
                continue
            if key.startswith("GEOB"):
                tags.add(GEOB(encoding=val["enc"], mime=val["mime"], desc=key[5:], data=base64.b64decode(val["data"])))  # type: ignore[index]
            elif key == "TKEY":
                tags.add(TKEY(encoding=val["enc"], text=list(val["text"])))  # type: ignore[index]
            elif key == "TBPM":
                tags.add(TBPM(encoding=val["enc"], text=list(val["text"])))  # type: ignore[index]
    else:
        for key in MP4_MANAGED:
            val = state.get(key)
            if val is None:
                if key in tags:
                    del tags[key]
                continue
            if key == ATOM_BPM:
                tags[key] = [int(val)]  # type: ignore[arg-type]
            else:
                tags[key] = [MP4FreeForm(base64.b64decode(val["data"]), dataformat=AtomDataType(val["fmt"]))]  # type: ignore[index]


def _save_atomically(path: str, mutate) -> None:
    """Clone the file, let `mutate(tmp_path)` rewrite the clone, prove the audio data is
    byte-identical, then rename the clone into place."""
    st_ = os.stat(path)
    flags = getattr(st_, "st_flags", 0)
    tmp = path + ".rb2serato-tmp" + os.path.splitext(path)[1]  # keep the extension for mutagen
    if os.path.exists(tmp):
        _unlock(tmp)
        os.unlink(tmp)
    r = subprocess.run(["cp", "-c", path, tmp], capture_output=True)
    if r.returncode != 0:
        shutil.copyfile(path, tmp)
    try:
        _unlock(tmp)  # a Finder-locked (uchg) original clones as locked; we own the clone
        os.chmod(tmp, (st_.st_mode & 0o7777) | 0o600)
        digest_before = audio_digest(path)
        mutate(tmp)
        if audio_digest(tmp) != digest_before:
            raise TagError("audio payload changed while writing tags - original left untouched")
        os.chmod(tmp, st_.st_mode & 0o7777)
        if flags:
            _unlock(path)  # cannot rename over a locked file
        os.replace(tmp, path)
        if flags:
            try:
                os.chflags(path, flags)  # put the user's lock back
            except OSError:
                pass
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _unlock(path: str) -> None:
    """Clear macOS immutable flags (Finder 'Locked') so the file can be written/replaced."""
    try:
        fl = os.stat(path).st_flags
    except (OSError, AttributeError):
        return
    if fl:
        try:
            os.chflags(path, 0)
        except OSError:
            pass


def write_state(path: str, state: Dict[str, object]) -> None:
    kind = file_kind(path)

    def mutate(tmp: str) -> None:
        f = _open(tmp)
        if f.tags is None:
            f.add_tags()
        _apply_state(f.tags, kind, state, f)
        if kind == "id3":
            version = getattr(f.tags, "version", (2, 4, 0))
            v2 = 3 if version[:2] == (2, 3) else 4
            f.save(v2_version=v2)
        else:
            f.save()

    _save_atomically(path, mutate)


@dataclass
class UndoLog:
    path: str
    fh: object = None
    count: int = 0

    def open(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fh = open(self.path, "a", encoding="utf-8")
        return self

    def record(self, file_path: str, before: Dict[str, object], after: Dict[str, object]) -> None:
        self.fh.write(json.dumps({"path": file_path, "before": before, "after": after, "time": time.time()}, ensure_ascii=False) + "\n")  # type: ignore[union-attr]
        self.fh.flush()  # type: ignore[union-attr]
        self.count += 1

    def close(self):
        if self.fh:
            self.fh.close()


def apply(path: str, payload: TagPayload, undo: Optional[UndoLog] = None, dry_run: bool = False) -> str:
    """Write the payload. Returns 'written', 'unchanged' or 'skipped'."""
    if not file_kind(path):
        return "skipped"
    before = snapshot(path)
    after = intended(path, payload, before)
    if after == before:
        return "unchanged"
    if dry_run:
        return "written"
    write_state(path, after)
    if undo is not None:
        undo.record(path, before, after)
    return "written"


def undo_file(path: str, before: Dict[str, object]) -> None:
    write_state(path, before)


# --------------------------------------------------------------------------- audio integrity


def audio_digest(path: str) -> str:
    """SHA-256 of the audio payload only (tags excluded), to prove tagging left it intact."""
    ext = os.path.splitext(path)[1].lower()
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        if ext == ".mp3":
            head = fh.read(10)
            start = 0
            if head[:3] == b"ID3":
                size = ((head[6] & 0x7F) << 21) | ((head[7] & 0x7F) << 14) | ((head[8] & 0x7F) << 7) | (head[9] & 0x7F)
                start = 10 + size + (10 if head[5] & 0x10 else 0)
            fh.seek(start)
            data = fh.read()
            if data[-128:-125] == b"TAG":
                data = data[:-128]
            # skip any padding zeros after the tag
            h.update(data.lstrip(b"\x00"))
            return h.hexdigest()
        data = fh.read()
    if ext in MP4_EXTS:
        return hashlib.sha256(_mp4_atom(data, b"mdat")).hexdigest()
    if ext == ".wav":
        return hashlib.sha256(_riff_chunk(data, b"data", 12)).hexdigest()
    if ext in (".aif", ".aiff"):
        return hashlib.sha256(_iff_chunk(data, b"SSND", 12)).hexdigest()
    return hashlib.sha256(data).hexdigest()


def _mp4_atom(data: bytes, name: bytes) -> bytes:
    off = 0
    while off + 8 <= len(data):
        size, atom = struct.unpack(">I4s", data[off : off + 8])
        header = 8
        if size == 1:
            size = struct.unpack(">Q", data[off + 8 : off + 16])[0]
            header = 16
        elif size == 0:
            size = len(data) - off
        if atom == name:
            return data[off + header : off + size]
        off += max(size, 8)
    return b""


def _riff_chunk(data: bytes, name: bytes, start: int) -> bytes:
    off = start
    while off + 8 <= len(data):
        cid, size = struct.unpack("<4sI", data[off : off + 8])
        if cid == name:
            return data[off + 8 : off + 8 + size]
        off += 8 + size + (size & 1)
    return b""


def _iff_chunk(data: bytes, name: bytes, start: int) -> bytes:
    off = start
    while off + 8 <= len(data):
        cid, size = struct.unpack(">4sI", data[off : off + 8])
        if cid == name:
            return data[off + 8 : off + 8 + size]
        off += 8 + size + (size & 1)
    return b""


def read_serato(path: str) -> Dict[str, object]:
    """Decode the Serato markers/beatgrid currently in a file (for verification)."""
    kind = file_kind(path)
    f = _open(path)
    out: Dict[str, object] = {"markers2": None, "markers": None, "grid": None, "key": None, "bpm": None}
    tags = f.tags
    if tags is None:
        return out
    if kind == "id3":
        fr = tags.getall(GEOB_MARKERS2)
        if fr:
            out["markers2"] = st.decode_markers2(fr[0].data)
        fr = tags.getall(GEOB_MARKERS)
        if fr:
            out["markers"] = st.decode_markers_id3(fr[0].data)
        fr = tags.getall(GEOB_BEATGRID)
        if fr:
            out["grid"] = st.decode_beatgrid(fr[0].data)
        if tags.getall("TKEY"):
            out["key"] = str(tags.getall("TKEY")[0].text[0])
        if tags.getall("TBPM"):
            out["bpm"] = str(tags.getall("TBPM")[0].text[0])
    else:
        if tags.get(ATOM_MARKERS2):
            out["markers2"] = st.decode_markers2(st.mp4_unwrap(bytes(tags[ATOM_MARKERS2][0]), "Serato Markers2"))
        if tags.get(ATOM_MARKERS):
            out["markers"] = st.decode_markers_mp4_payload(st.mp4_unwrap(bytes(tags[ATOM_MARKERS][0]), "Serato Markers_"))
        if tags.get(ATOM_BEATGRID):
            out["grid"] = st.decode_beatgrid(st.mp4_unwrap(bytes(tags[ATOM_BEATGRID][0]), "Serato BeatGrid"))
        if tags.get(ATOM_KEY):
            out["key"] = bytes(tags[ATOM_KEY][0]).decode("utf-8", "replace")
        if tags.get(ATOM_BPM):
            out["bpm"] = str(tags[ATOM_BPM][0])
    return out

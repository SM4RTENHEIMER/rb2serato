"""Encoders and decoders for the Serato tag payloads stored inside audio files.

Formats follow Serato's own output (verified byte-for-byte against files written by
Serato DJ Pro 2.x-4.x), the documentation in Holzhaus/serato-tags and Mixxx's
implementation in src/track/serato/.

Three payloads matter for cue points and beatgrids:

  Serato Markers2  - cues, saved loops, track colour, beatgrid lock (all file types)
  Serato Markers_  - legacy copy of the first 5 cues / 9 loops. Serato DJ Pro prefers
                     this tag over Markers2 for those slots, so it must be written too
                     (MP3/AIFF/WAV: ID3 layout; M4A: its own 19-byte layout).
  Serato BeatGrid  - tempo markers.

MP3, AIFF and WAV keep them in ID3v2 GEOB frames; M4A in "----:com.serato.dj:*" freeform
atoms whose value is base64 of "application/octet-stream\\0\\0<name>\\0" + payload.
"""

from __future__ import annotations

import base64
import struct
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

RGB = Tuple[int, int, int]

# Serato DJ Pro's 18 cue colours as *stored* in the tags (Serato brightens them on screen).
CUE_PALETTE: List[RGB] = [
    (0xCC, 0x00, 0x00), (0xCC, 0x44, 0x00), (0xCC, 0x88, 0x00), (0xCC, 0xCC, 0x00),
    (0x88, 0xCC, 0x00), (0x44, 0xCC, 0x00), (0x00, 0xCC, 0x00), (0x00, 0xCC, 0x44),
    (0x00, 0xCC, 0x88), (0x00, 0xCC, 0xCC), (0x00, 0x88, 0xCC), (0x00, 0x44, 0xCC),
    (0x00, 0x00, 0xCC), (0x44, 0x00, 0xCC), (0x88, 0x00, 0xCC), (0xCC, 0x00, 0xCC),
    (0xCC, 0x00, 0x88), (0xCC, 0x00, 0x44),
]
# Serato's default colour for cue slots 1..8.
DEFAULT_SLOT_COLORS: List[RGB] = [CUE_PALETTE[i] for i in (0, 2, 12, 3, 6, 15, 9, 14)]
LOOP_COLOR: RGB = (0x27, 0xAA, 0xE1)
NO_TRACK_COLOR: RGB = (0xFF, 0xFF, 0xFF)

MARKERS2_MIN_SIZE = 470  # Serato pre-allocates at least this much for Markers2

PREFIX = b"application/octet-stream\x00\x00"


def nearest_palette_color(rgb: RGB, palette: Sequence[RGB] = CUE_PALETTE) -> RGB:
    r, g, b = rgb
    return min(palette, key=lambda c: (c[0] - r) ** 2 + (c[1] - g) ** 2 + (c[2] - b) ** 2)


@dataclass
class SeratoCue:
    index: int  # 0..7
    position_ms: int
    color: RGB
    name: str = ""


@dataclass
class SeratoLoop:
    index: int  # 0..7
    start_ms: int
    end_ms: int
    name: str = ""
    locked: bool = False
    color: RGB = LOOP_COLOR


@dataclass
class SeratoMarkers:
    cues: List[SeratoCue]
    loops: List[SeratoLoop]
    track_color: RGB = NO_TRACK_COLOR
    bpm_locked: bool = False


# --------------------------------------------------------------------------- serato32


def serato32_encode(value: int) -> bytes:
    """24-bit value -> Serato's 4-byte format (7 payload bits per byte)."""
    a, b, c = (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF
    z = c & 0x7F
    y = ((c >> 7) | (b << 1)) & 0x7F
    x = ((b >> 6) | (a << 2)) & 0x7F
    w = (a >> 5) & 0x7F
    return bytes((w, x, y, z))


def serato32_decode(data: bytes) -> int:
    w, x, y, z = data
    c = (z & 0x7F) | ((y & 0x01) << 7)
    b = ((y & 0x7F) >> 1) | ((x & 0x03) << 6)
    a = ((x & 0x7F) >> 2) | ((w & 0x07) << 5)
    return (a << 16) | (b << 8) | c


# --------------------------------------------------------------------------- base64 helpers


def _wrap72(text: bytes) -> bytes:
    return b"\n".join(text[i : i + 72] for i in range(0, len(text), 72))


def _b64_unpadded(data: bytes) -> bytes:
    """Serato-style inner base64: no '=' padding, newline every 72 chars."""
    if len(data) % 3:
        data = data + b"\x00" * (3 - len(data) % 3)  # avoid padding altogether
    return _wrap72(base64.b64encode(data))


def mp4_wrap(name: str, payload: bytes) -> bytes:
    """Value of a ----:com.serato.dj:* atom (Serato DJ Pro 4 style: padded base64,
    72-char lines, trailing newline)."""
    data = PREFIX + name.encode("ascii") + b"\x00" + payload
    return _wrap72(base64.b64encode(data)) + b"\n"


def mp4_unwrap(value: bytes, name: str) -> bytes:
    s = bytes(value).replace(b"\n", b"").replace(b"\r", b"")
    rem = len(s) % 4
    if rem == 1:  # Serato sometimes emits one extra base64 character
        s = s[:-1]
        rem = 0
    if rem:
        s += b"=" * (4 - rem)
    data = base64.b64decode(s)
    marker = PREFIX + name.encode("ascii") + b"\x00"
    if not data.startswith(marker):
        raise ValueError(f"not a Serato {name} atom")
    return data[len(marker) :]


# --------------------------------------------------------------------------- Markers2


def _entry(name: bytes, data: bytes) -> bytes:
    return name + b"\x00" + struct.pack(">I", len(data)) + data


def markers2_inner(m: SeratoMarkers) -> bytes:
    """The entry list: 01 01, entries, terminating NUL (before base64 encoding)."""
    out = [b"\x01\x01", _entry(b"COLOR", b"\x00" + bytes(m.track_color))]
    for c in sorted(m.cues, key=lambda c: c.index):
        out.append(
            _entry(
                b"CUE",
                b"\x00"
                + struct.pack(">BI", c.index, c.position_ms)
                + b"\x00"
                + bytes(c.color)
                + b"\x00\x00"
                + c.name.encode("utf-8")
                + b"\x00",
            )
        )
    for l in sorted(m.loops, key=lambda l: l.index):
        out.append(
            _entry(
                b"LOOP",
                b"\x00"
                + struct.pack(">BII", l.index, l.start_ms, l.end_ms)
                + b"\xff\xff\xff\xff\x00"
                + bytes(l.color)
                + b"\x00"
                + (b"\x01" if l.locked else b"\x00")
                + l.name.encode("utf-8")
                + b"\x00",
            )
        )
    out.append(_entry(b"BPMLOCK", b"\x01" if m.bpm_locked else b"\x00"))
    out.append(b"\x00")
    return b"".join(out)


def markers2_body(m: SeratoMarkers) -> bytes:
    """01 01 + base64(inner); this is what sits after the GEOB/atom prefix."""
    return b"\x01\x01" + _b64_unpadded(markers2_inner(m))


def encode_markers2_id3(m: SeratoMarkers, min_size: int = MARKERS2_MIN_SIZE) -> bytes:
    body = markers2_body(m)
    size = max(min_size, len(body) + 1)
    return body.ljust(size, b"\x00")


def encode_markers2_mp4(m: SeratoMarkers, min_size: int = MARKERS2_MIN_SIZE) -> bytes:
    body = markers2_body(m)
    size = max(min_size, len(body) + 1)
    total = len(PREFIX) + len(b"Serato Markers2\x00") + size
    if total % 3:
        size += 3 - total % 3  # keep the outer base64 free of '=' padding
    return mp4_wrap("Serato Markers2", body.ljust(size, b"\x00"))


def decode_markers2(data: bytes) -> SeratoMarkers:
    """Parse a Markers2 body (01 01 + base64...). Returns cues/loops/colour/lock."""
    data = bytes(data)
    if data[:2] != b"\x01\x01":
        raise ValueError("bad Markers2 header")
    b64 = data[2:].split(b"\x00", 1)[0].replace(b"\n", b"").replace(b"\r", b"")
    if len(b64) % 4 == 1:
        b64 += b"A=="
    else:
        b64 += b"=" * (-len(b64) % 4)
    inner = base64.b64decode(b64)
    if inner[:2] != b"\x01\x01":
        raise ValueError("bad Markers2 inner header")
    pos = 2
    m = SeratoMarkers(cues=[], loops=[])
    while pos < len(inner):
        end = inner.find(b"\x00", pos)
        if end < 0 or end == pos:
            break
        name = inner[pos:end].decode("ascii", "replace")
        pos = end + 1
        (length,) = struct.unpack(">I", inner[pos : pos + 4])
        pos += 4
        payload = inner[pos : pos + length]
        pos += length
        if name == "COLOR":
            m.track_color = tuple(payload[1:4])  # type: ignore[assignment]
        elif name == "BPMLOCK":
            m.bpm_locked = bool(payload[0])
        elif name == "CUE":
            index, position = struct.unpack(">BI", payload[1:6])
            color = tuple(payload[7:10])
            label = payload[12:].split(b"\x00", 1)[0].decode("utf-8", "replace")
            m.cues.append(SeratoCue(index, position, color, label))  # type: ignore[arg-type]
        elif name == "LOOP":
            index, start, endp = struct.unpack(">BII", payload[1:10])
            color = tuple(payload[15:18])
            locked = bool(payload[19])
            label = payload[20:].split(b"\x00", 1)[0].decode("utf-8", "replace")
            m.loops.append(SeratoLoop(index, start, endp, label, locked, color))  # type: ignore[arg-type]
    return m


# --------------------------------------------------------------------------- Markers_ (legacy)

_NO_POS = b"\x7f\x7f\x7f\x7f"
NUM_LEGACY_CUES = 5
NUM_LEGACY_LOOPS = 9


def _legacy_slots(m: SeratoMarkers):
    cues = {c.index: c for c in m.cues if 0 <= c.index < NUM_LEGACY_CUES}
    loops = {l.index: l for l in m.loops if 0 <= l.index < NUM_LEGACY_LOOPS}
    return cues, loops


def encode_markers_id3(m: SeratoMarkers) -> bytes:
    cues, loops = _legacy_slots(m)
    out = [struct.pack(">HI", 0x0205, NUM_LEGACY_CUES + NUM_LEGACY_LOOPS)]
    for i in range(NUM_LEGACY_CUES):
        c = cues.get(i)
        if c is None:
            out.append(b"\x7f" + _NO_POS + b"\x7f" + _NO_POS + b"\x00\x7f\x7f\x7f\x7f\x7f" + b"\x00\x00\x00\x00" + b"\x00\x00")
        else:
            rgb = (c.color[0] << 16) | (c.color[1] << 8) | c.color[2]
            out.append(
                b"\x00" + serato32_encode(c.position_ms & 0xFFFFFF) + b"\x7f" + _NO_POS
                + b"\x00\x7f\x7f\x7f\x7f\x7f" + serato32_encode(rgb) + b"\x01\x00"
            )
    for i in range(NUM_LEGACY_LOOPS):
        l = loops.get(i)
        if l is None:
            out.append(b"\x7f" + _NO_POS + b"\x7f" + _NO_POS + b"\x00\x7f\x7f\x7f\x7f\x7f" + b"\x00\x00\x00\x00" + b"\x03\x00")
        else:
            out.append(
                b"\x00" + serato32_encode(l.start_ms & 0xFFFFFF) + b"\x00" + serato32_encode(l.end_ms & 0xFFFFFF)
                + b"\x00\x7f\x7f\x7f\x7f\x7f" + b"\x00\x00\x00\x00" + b"\x03" + (b"\x01" if l.locked else b"\x00")
            )
    tc = (m.track_color[0] << 16) | (m.track_color[1] << 8) | m.track_color[2]
    out.append(serato32_encode(tc))
    return b"".join(out)


def decode_markers_id3(data: bytes) -> SeratoMarkers:
    data = bytes(data)
    version, n = struct.unpack(">HI", data[:6])
    if version != 0x0205:
        raise ValueError("bad Markers_ version")
    m = SeratoMarkers(cues=[], loops=[])
    pos = 6
    for i in range(n):
        e = data[pos : pos + 22]
        pos += 22
        has_start = e[0] == 0x00
        has_end = e[5] == 0x00
        start = serato32_decode(e[1:5]) if has_start else None
        end = serato32_decode(e[6:10]) if has_end else None
        rgb = serato32_decode(e[16:20])
        color = ((rgb >> 16) & 0xFF, (rgb >> 8) & 0xFF, rgb & 0xFF)
        etype, locked = e[20], e[21]
        if i < NUM_LEGACY_CUES:
            if has_start and etype == 1:
                m.cues.append(SeratoCue(i, start, color))
        else:
            if has_start and has_end and etype == 3:
                m.loops.append(SeratoLoop(i - NUM_LEGACY_CUES, start, end, "", bool(locked)))
    tc = serato32_decode(data[pos : pos + 4])
    m.track_color = ((tc >> 16) & 0xFF, (tc >> 8) & 0xFF, tc & 0xFF)
    return m


def markers_mp4_payload(m: SeratoMarkers) -> bytes:
    cues, loops = _legacy_slots(m)
    out = [struct.pack(">HI", 0x0205, NUM_LEGACY_CUES + NUM_LEGACY_LOOPS)]
    unset = b"\xff\xff\xff\xff\xff\xff\xff\xff" + b"\x00\xff\xff\xff\xff\x00" + b"\x00\x00\x00"
    for i in range(NUM_LEGACY_CUES):
        c = cues.get(i)
        if c is None:
            out.append(unset + b"\x00\x00")
        else:
            out.append(struct.pack(">II", c.position_ms, 0xFFFFFFFF) + b"\x00\xff\xff\xff\xff\x00" + bytes(c.color) + b"\x01\x00")
    for i in range(NUM_LEGACY_LOOPS):
        l = loops.get(i)
        if l is None:
            out.append(unset + b"\x03\x00")
        else:
            out.append(struct.pack(">II", l.start_ms, l.end_ms) + b"\x00\xff\xff\xff\xff\x00" + b"\x00\x00\x00" + b"\x03" + (b"\x01" if l.locked else b"\x00"))
    out.append(b"\x00" + bytes(m.track_color))
    return b"".join(out)


def encode_markers_mp4(m: SeratoMarkers) -> bytes:
    return mp4_wrap("Serato Markers_", markers_mp4_payload(m))


def decode_markers_mp4_payload(data: bytes) -> SeratoMarkers:
    version, n = struct.unpack(">HI", data[:6])
    if version != 0x0205:
        raise ValueError("bad Markers_ (MP4) version")
    m = SeratoMarkers(cues=[], loops=[])
    pos = 6
    for i in range(n):
        e = data[pos : pos + 19]
        pos += 19
        start, end = struct.unpack(">II", e[:8])
        color = tuple(e[14:17])
        etype, locked = e[17], e[18]
        if i < NUM_LEGACY_CUES:
            if start != 0xFFFFFFFF and etype == 1:
                m.cues.append(SeratoCue(i, start, color))  # type: ignore[arg-type]
        else:
            if start != 0xFFFFFFFF and end != 0xFFFFFFFF and etype == 3:
                m.loops.append(SeratoLoop(i - NUM_LEGACY_CUES, start, end, "", bool(locked)))
    m.track_color = tuple(data[pos + 1 : pos + 4])  # type: ignore[assignment]
    return m


# --------------------------------------------------------------------------- BeatGrid


@dataclass
class BeatGridMarker:
    position_sec: float
    beats_to_next: int = 0  # non-terminal markers
    bpm: float = 0.0  # terminal marker only


def encode_beatgrid(markers: Sequence[BeatGridMarker]) -> bytes:
    """01 00, count, non-terminal (pos f32, beats u32)..., terminal (pos f32, bpm f32)."""
    if not markers:
        return b""
    out = [struct.pack(">HI", 0x0100, len(markers))]
    for mk in markers[:-1]:
        out.append(struct.pack(">fI", mk.position_sec, max(1, int(mk.beats_to_next))))
    last = markers[-1]
    out.append(struct.pack(">ff", last.position_sec, last.bpm))
    return b"".join(out)


def encode_beatgrid_mp4(markers: Sequence[BeatGridMarker]) -> bytes:
    return mp4_wrap("Serato BeatGrid", encode_beatgrid(markers))


def decode_beatgrid(data: bytes) -> List[BeatGridMarker]:
    data = bytes(data)
    version, n = struct.unpack(">HI", data[:6])
    if version != 0x0100:
        raise ValueError("bad BeatGrid version")
    out = []
    for i in range(n):
        chunk = data[6 + 8 * i : 14 + 8 * i]
        if i == n - 1:
            pos, bpm = struct.unpack(">ff", chunk)
            out.append(BeatGridMarker(pos, 0, bpm))
        else:
            pos, beats = struct.unpack(">fI", chunk)
            out.append(BeatGridMarker(pos, beats, 0.0))
    return out


# --------------------------------------------------------------------------- Autotags


def autotags_with_bpm(data: bytes, bpm: float) -> Optional[bytes]:
    """Replace the BPM string in an existing 'Serato Autotags' payload (keeps auto gain)."""
    data = bytes(data)
    if data[:2] != b"\x01\x01":
        return None
    parts = data[2:].split(b"\x00")
    if len(parts) < 3:
        return None
    parts[0] = f"{bpm:.2f}".encode("ascii")
    return b"\x01\x01" + b"\x00".join(parts)

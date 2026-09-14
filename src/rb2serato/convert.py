"""Turn Rekordbox data into what Serato understands: cue slots, loops, beatgrid markers,
key names and the crate tree."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .model import Cue, Library, Node, Track
from .seratotags import (
    DEFAULT_SLOT_COLORS,
    NO_TRACK_COLOR,
    BeatGridMarker,
    SeratoCue,
    SeratoLoop,
    SeratoMarkers,
    nearest_palette_color,
)

AUTO_CUE_NAMES = {"CUE(Auto)"}
NUM_CUE_SLOTS = 8
NUM_LOOP_SLOTS = 8


@dataclass
class Options:
    memory_cues: str = "fill"  # fill: memory cues take free slots | skip | first: memory cues before hot cues
    keep_auto_names: bool = False  # keep Rekordbox's "CUE(Auto)" labels
    cue_colors: str = "rekordbox"  # rekordbox: nearest Serato colour | serato: Serato's slot defaults
    lock_grid: bool = True  # write BPMLOCK so Serato's analysis keeps the Rekordbox grid
    write_key: bool = True
    write_bpm: bool = True
    mp3_offset_ms: int = 0  # added to every position in MP3 files (see README)
    unwrap: str = ""  # name of a top-level Rekordbox folder whose children become top-level crates
    keep_empty: bool = False  # keep playlists without any existing track


@dataclass
class TrackPlan:
    track: Track
    markers: SeratoMarkers
    grid: List[BeatGridMarker]
    key: str
    bpm: float
    dropped_hot: int = 0
    dropped_memory: int = 0
    dropped_loops: int = 0
    notes: List[str] = field(default_factory=list)

    @property
    def n_cues(self) -> int:
        return len(self.markers.cues)

    @property
    def n_loops(self) -> int:
        return len(self.markers.loops)


# --------------------------------------------------------------------------- keys

_CAMELOT = {
    "1A": "Abm", "2A": "Ebm", "3A": "Bbm", "4A": "Fm", "5A": "Cm", "6A": "Gm",
    "7A": "Dm", "8A": "Am", "9A": "Em", "10A": "Bm", "11A": "F#m", "12A": "Dbm",
    "1B": "B", "2B": "F#", "3B": "Db", "4B": "Ab", "5B": "Eb", "6B": "Bb",
    "7B": "F", "8B": "C", "9B": "G", "10B": "D", "11B": "A", "12B": "E",
}
# Open Key starts its wheel at C major (1d = 8B), so its numbers run 5 ahead of Camelot's.
_OPEN_KEY = {f"{(int(k[:-1]) + 4) % 12 + 1}{'m' if k.endswith('A') else 'd'}": v for k, v in _CAMELOT.items()}
_STANDARD = re.compile(r"^([A-Ga-g])([#b♯♭]?)\s*(m|min|minor|maj|major|dur|mol|moll)?$")


def normalize_key(name: str) -> str:
    """Rekordbox key text -> standard notation Serato reads natively ("F#m", "Eb").
    Camelot ("8A") and Open Key ("1m") are translated; unknown text is passed through."""
    s = (name or "").strip()
    if not s:
        return ""
    upper = s.upper()
    if upper in _CAMELOT:
        return _CAMELOT[upper]
    ok = s.lower()
    if ok in _OPEN_KEY:
        return _OPEN_KEY[ok]
    m = _STANDARD.match(s)
    if m:
        note = m.group(1).upper()
        acc = m.group(2).replace("♯", "#").replace("♭", "b")
        mode = (m.group(3) or "").lower()
        minor = mode in ("m", "min", "minor", "mol", "moll")
        return f"{note}{acc}{'m' if minor else ''}"
    return s


# --------------------------------------------------------------------------- cues


def _cue_name(cue: Cue, opts: Options) -> str:
    n = cue.name.strip()
    if not opts.keep_auto_names and n in AUTO_CUE_NAMES:
        return ""
    return n[:100]


def _cue_color(cue: Cue, index: int, opts: Options):
    if opts.cue_colors == "rekordbox" and cue.rgb is not None:
        return nearest_palette_color(cue.rgb)
    return DEFAULT_SLOT_COLORS[index % len(DEFAULT_SLOT_COLORS)]


def _next_free(used: Dict[int, object], limit: int) -> Optional[int]:
    for i in range(limit):
        if i not in used:
            return i
    return None


def plan_track(track: Track, opts: Options) -> TrackPlan:
    offset = opts.mp3_offset_ms if track.ext == ".mp3" else 0

    def pos(ms: int) -> int:
        return max(0, int(round(ms + offset)))

    cues: Dict[int, SeratoCue] = {}
    loops: Dict[int, SeratoLoop] = {}
    plan = TrackPlan(track=track, markers=SeratoMarkers(cues=[], loops=[]), grid=[], key="", bpm=track.bpm)

    hot = sorted((c for c in track.cues if c.is_hot), key=lambda c: (c.slot, c.start_ms))
    memory = sorted((c for c in track.cues if not c.is_hot), key=lambda c: c.start_ms)

    def place(cue: Cue, preferred: Optional[int], is_hot: bool) -> None:
        if cue.is_loop:
            idx = preferred if preferred is not None and preferred < NUM_LOOP_SLOTS and preferred not in loops else _next_free(loops, NUM_LOOP_SLOTS)
            if idx is None:
                plan.dropped_loops += 1
                return
            loops[idx] = SeratoLoop(idx, pos(cue.start_ms), pos(cue.end_ms), _cue_name(cue, opts))
            return
        idx = preferred if preferred is not None and preferred < NUM_CUE_SLOTS and preferred not in cues else _next_free(cues, NUM_CUE_SLOTS)
        if idx is None:
            if is_hot:
                plan.dropped_hot += 1
            else:
                plan.dropped_memory += 1
            return
        cues[idx] = SeratoCue(idx, pos(cue.start_ms), _cue_color(cue, idx, opts), _cue_name(cue, opts))

    if opts.memory_cues == "first":
        for c in memory:
            place(c, None, False)
        for c in hot:
            place(c, c.slot, True)
    else:
        for c in hot:
            place(c, c.slot, True)
        if opts.memory_cues == "skip":
            plan.dropped_memory += len(memory)
        else:
            for c in memory:
                place(c, None, False)

    plan.markers = SeratoMarkers(
        cues=[cues[i] for i in sorted(cues)],
        loops=[loops[i] for i in sorted(loops)],
        track_color=NO_TRACK_COLOR,
        bpm_locked=bool(opts.lock_grid and track.grid),
    )
    plan.grid = serato_grid(track, offset)
    if plan.grid:
        plan.bpm = round(plan.grid[-1].bpm, 2)
    plan.key = normalize_key(track.key) if opts.write_key else ""
    if not track.grid:
        plan.notes.append("no beatgrid in Rekordbox")
    if plan.dropped_hot:
        plan.notes.append(f"{plan.dropped_hot} hot cue(s) beyond 8 dropped")
    if plan.dropped_memory and opts.memory_cues != "skip":
        plan.notes.append(f"{plan.dropped_memory} memory cue(s) did not fit")
    if plan.dropped_loops:
        plan.notes.append(f"{plan.dropped_loops} loop(s) did not fit")
    return plan


# --------------------------------------------------------------------------- beatgrid


def serato_grid(track: Track, offset_ms: float = 0.0) -> List[BeatGridMarker]:
    markers = [m for m in track.grid if m.bpm > 0]
    if not markers:
        return []
    out: List[BeatGridMarker] = []
    for i, m in enumerate(markers):
        time_ms = m.time_ms + offset_ms
        beats = m.beats_to_next
        if i == 0 and m.beat in (2, 3, 4):
            # Anchor the grid on a downbeat so Serato's bar lines match Rekordbox's.
            shift = (5 - m.beat) % 4
            beat_ms = 60000.0 / m.bpm
            if i == len(markers) - 1 or beats - shift >= 1:
                time_ms += shift * beat_ms
                beats = beats - shift if beats else 0
        if i < len(markers) - 1:
            if beats <= 0:
                beats = int(round((markers[i + 1].time_ms - m.time_ms) * m.bpm / 60000.0))
            if beats <= 0:
                continue
            out.append(BeatGridMarker(position_sec=max(0.0, time_ms) / 1000.0, beats_to_next=beats))
        else:
            out.append(BeatGridMarker(position_sec=max(0.0, time_ms) / 1000.0, bpm=m.bpm))
    return out


# --------------------------------------------------------------------------- crates


@dataclass
class CrateSpec:
    parts: List[str]  # crate names from the top level down to this crate
    track_ids: List[str]
    is_folder: bool

    @property
    def name(self) -> str:
        return "%%".join(self.parts)


_BAD_CHARS = re.compile(r"[/\\:%]+")


def crate_safe_name(name: str) -> str:
    s = _BAD_CHARS.sub("-", name).strip().strip(".")
    s = re.sub(r"\s+", " ", s)
    return s or "Untitled"


def crate_specs(lib: Library, opts: Options) -> List[CrateSpec]:
    """Flatten the Rekordbox playlist tree into Serato crates, in Rekordbox's order."""
    tops = list(lib.root.children)
    if opts.unwrap:
        expanded: List[Node] = []
        for n in tops:
            if n.is_folder and n.name.strip().lower() == opts.unwrap.strip().lower():
                expanded.extend(n.children)
            else:
                expanded.append(n)
        tops = expanded

    specs: List[CrateSpec] = []

    def visit(node: Node, parts: List[str]) -> bool:
        """Returns True if the node produced a crate."""
        if node.is_folder:
            used: Dict[str, int] = {}
            children_specs_start = len(specs)
            placeholder = CrateSpec(parts=parts, track_ids=[], is_folder=True)
            specs.append(placeholder)
            produced_any = False
            for child in node.children:
                name = crate_safe_name(child.name)
                key = name.lower()
                if key in used:
                    used[key] += 1
                    name = f"{name} {used[key]}"
                else:
                    used[key] = 1
                if visit(child, parts + [name]):
                    produced_any = True
            if not produced_any and not opts.keep_empty:
                del specs[children_specs_start:]
                return False
            return True
        ids = [tid for tid in node.track_ids if tid in lib.tracks and lib.tracks[tid].is_file]
        if not ids and not opts.keep_empty:
            return False
        specs.append(CrateSpec(parts=parts, track_ids=ids, is_folder=False))
        return True

    used_top: Dict[str, int] = {}
    for n in tops:
        name = crate_safe_name(n.name)
        key = name.lower()
        if key in used_top:
            used_top[key] += 1
            name = f"{name} {used_top[key]}"
        else:
            used_top[key] = 1
        visit(n, [name])
    return specs

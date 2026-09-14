"""Source-agnostic data model: what we know about a track, and the playlist tree."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

RGB = Tuple[int, int, int]


@dataclass
class Cue:
    """A Rekordbox cue point. Hot cues have a 0-based slot (A=0); memory cues have slot None."""

    start_ms: int
    end_ms: Optional[int] = None  # set for loops
    slot: Optional[int] = None
    name: str = ""
    rgb: Optional[RGB] = None

    @property
    def is_hot(self) -> bool:
        return self.slot is not None

    @property
    def is_loop(self) -> bool:
        return self.end_ms is not None and self.end_ms > self.start_ms


@dataclass
class GridMarker:
    """Start of a constant-tempo region of the beatgrid."""

    time_ms: float
    bpm: float
    beat: int = 0  # 1..4 = beat within the bar (1 = downbeat), 0 = unknown
    beats_to_next: int = 0  # number of beats until the next marker (0 for the last one)


@dataclass
class Track:
    id: str
    path: str  # absolute path as Rekordbox stores it ("" for streaming tracks)
    title: str = ""
    artist: str = ""
    album: str = ""
    genre: str = ""
    composer: str = ""
    comment: str = ""
    label: str = ""
    remixer: str = ""
    grouping: str = ""
    year: int = 0
    track_no: int = 0
    disc_no: int = 0
    length_sec: float = 0.0
    bpm: float = 0.0
    key: str = ""  # as Rekordbox names it ("F#m", "10B", ...)
    bitrate: int = 0  # kbps
    sample_rate: int = 0
    file_size: int = 0
    color_id: int = 0  # Rekordbox track colour 1..8, 0 = none
    rating: int = 0
    play_count: int = 0
    date_added: str = ""  # "YYYY-MM-DD"
    cues: List[Cue] = field(default_factory=list)
    grid: List[GridMarker] = field(default_factory=list)

    @property
    def ext(self) -> str:
        import os

        return os.path.splitext(self.path)[1].lower()

    @property
    def is_file(self) -> bool:
        return self.path.startswith("/")

    @property
    def display(self) -> str:
        return f"{self.artist or '?'} - {self.title or '?'}"


@dataclass
class Node:
    """Playlist tree node: a folder (children) or a playlist (track_ids, in order)."""

    id: str
    name: str
    is_folder: bool
    children: List["Node"] = field(default_factory=list)
    track_ids: List[str] = field(default_factory=list)

    def walk(self, depth: int = 0):
        yield self, depth
        for child in self.children:
            yield from child.walk(depth + 1)


@dataclass
class Library:
    tracks: "dict[str, Track]"
    root: Node  # synthetic root folder; its children are the top-level folders/playlists
    source: str = ""

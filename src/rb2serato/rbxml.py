"""Read a Rekordbox XML export (File > Export Collection in xml format).

This is the input path that needs nothing but Rekordbox itself: no database key, no
decryption. The XML carries everything rb2serato uses - file locations, hot/memory cues
with names and colours, loops, tempo markers, key, and the folder/playlist tree.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional
from urllib.parse import unquote, urlparse

from .model import Cue, GridMarker, Library, Node, Track

_KIND_LOOP = "4"


def location_to_path(location: str) -> str:
    """'file://localhost/Users/x/My%20Track.mp3' -> '/Users/x/My Track.mp3'."""
    if not location:
        return ""
    if not location.startswith("file:"):
        return location  # streaming links and the like
    parsed = urlparse(location)
    path = unquote(parsed.path)
    if os.name == "nt" and re.match(r"^/[A-Za-z]:", path):
        path = path[1:]
    return path


def _float(value: Optional[str], default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except ValueError:
        return default


def _int(value: Optional[str], default: int = 0) -> int:
    try:
        return int(float(value)) if value not in (None, "") else default
    except ValueError:
        return default


def _rgb(el: ET.Element):
    if el.get("Red") is None:
        return None
    return (_int(el.get("Red")), _int(el.get("Green")), _int(el.get("Blue")))


def _tempo_markers(track_el: ET.Element) -> List[GridMarker]:
    tempos = []
    for t in track_el.findall("TEMPO"):
        bpm = _float(t.get("Bpm"))
        if bpm <= 0:
            continue
        tempos.append(GridMarker(time_ms=_float(t.get("Inizio")) * 1000.0, bpm=bpm, beat=_int(t.get("Battito"), 0)))
    tempos.sort(key=lambda m: m.time_ms)
    for i, m in enumerate(tempos):
        if i < len(tempos) - 1:
            m.beats_to_next = max(1, int(round((tempos[i + 1].time_ms - m.time_ms) * m.bpm / 60000.0)))
    return tempos


def _cues(track_el: ET.Element) -> List[Cue]:
    cues = []
    for pm in track_el.findall("POSITION_MARK"):
        start = _float(pm.get("Start"), -1.0)
        if start < 0:
            continue
        num = _int(pm.get("Num"), -1)
        end = None
        if pm.get("Type") == _KIND_LOOP or pm.get("End") not in (None, ""):
            end_s = _float(pm.get("End"), -1.0)
            if end_s > start:
                end = int(round(end_s * 1000))
        cues.append(
            Cue(
                start_ms=int(round(start * 1000)),
                end_ms=end,
                slot=num if num >= 0 else None,
                name=(pm.get("Name") or "").strip(),
                rgb=_rgb(pm),
            )
        )
    return cues


def load_xml(path: str) -> Library:
    tree = ET.parse(path)
    root_el = tree.getroot()
    tracks: Dict[str, Track] = {}
    for el in root_el.iterfind("./COLLECTION/TRACK"):
        tid = el.get("TrackID") or ""
        if not tid:
            continue
        t = Track(
            id=tid,
            path=location_to_path(el.get("Location") or ""),
            title=(el.get("Name") or "").strip(),
            artist=(el.get("Artist") or "").strip(),
            album=(el.get("Album") or "").strip(),
            genre=(el.get("Genre") or "").strip(),
            composer=(el.get("Composer") or "").strip(),
            comment=(el.get("Comments") or "").strip(),
            label=(el.get("Label") or "").strip(),
            remixer=(el.get("Remixer") or "").strip(),
            grouping=(el.get("Grouping") or "").strip(),
            year=_int(el.get("Year")),
            track_no=_int(el.get("TrackNumber")),
            disc_no=_int(el.get("DiscNumber")),
            length_sec=_float(el.get("TotalTime")),
            bpm=round(_float(el.get("AverageBpm")), 2),
            key=(el.get("Tonality") or "").strip(),
            bitrate=_int(el.get("BitRate")),
            sample_rate=_int(el.get("SampleRate")),
            file_size=_int(el.get("Size")),
            rating=_int(el.get("Rating")),
            play_count=_int(el.get("PlayCount")),
            date_added=(el.get("DateAdded") or "")[:10],
            cues=_cues(el),
            grid=_tempo_markers(el),
        )
        tracks[t.id] = t

    root = Node(id="root", name="", is_folder=True)
    playlists_root = root_el.find("./PLAYLISTS/NODE")
    if playlists_root is not None:
        _build_tree(playlists_root, root, counter=[0])
    return Library(tracks=tracks, root=root, source=f"rekordbox xml ({os.path.basename(path)})")


def _build_tree(el: ET.Element, parent: Node, counter: List[int]) -> None:
    for child in el.findall("NODE"):
        counter[0] += 1
        node = Node(id=f"xml{counter[0]}", name=(child.get("Name") or "").strip() or "Untitled", is_folder=child.get("Type") == "0")
        if node.is_folder:
            _build_tree(child, node, counter)
        else:
            node.track_ids = [t.get("Key") or "" for t in child.findall("TRACK") if t.get("Key")]
        parent.children.append(node)

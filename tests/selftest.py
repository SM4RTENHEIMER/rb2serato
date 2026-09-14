"""Self-test for rb2serato.

1. Encoders round-trip through our own decoders AND through the independent reference
   parsers from Holzhaus/serato-tags.
2. Tags that Serato itself wrote into files in this library are decoded and re-encoded
   byte-for-byte (structure fidelity).
3. Serato's own `database V2` record is re-encoded byte-for-byte.
4. Conversion logic (beatgrid compression, cue slot mapping, key names, crates).
5. Optional end-to-end run on *copies* of real files from your own library: write tags,
   verify, check the audio payload is untouched, undo, check again. Needs a readable
   Rekordbox library (master.db + key, or RB2SERATO_TEST_XML=<export.xml>).

Run: ./rb2serato-selftest [--files]
"""

from __future__ import annotations

import io
import os
import shutil
import struct
import sys
import tempfile
from pathlib import Path

from rb2serato import convert, rbxml, rekordbox, seratodb, seratolib, seratotags as st, tagwriter
from rb2serato.model import Cue, GridMarker, Library, Node, Track

sys.path.insert(0, str(Path(__file__).resolve().parent / "vendor"))
import serato_markers2 as ref_m2  # noqa: E402
import serato_markers_ as ref_m1  # noqa: E402

FAILS = []
PASSES = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(f"{name}: {detail}")
        print(f"  FAIL {name} {detail}")


def sample_markers() -> st.SeratoMarkers:
    cues = [st.SeratoCue(i, 1000 * (i + 1) + 7, st.CUE_PALETTE[i], name) for i, name in enumerate(["Intro", "", "Drop æøå", "Break", "", "Vocal", "Outro", "x"])]
    loops = [st.SeratoLoop(0, 12000, 16000, "Loop 1"), st.SeratoLoop(3, 32000, 33000, "", True)]
    return st.SeratoMarkers(cues=cues, loops=loops, track_color=(0xFF, 0xFF, 0xFF), bpm_locked=True)


def test_serato32():
    for v in [0, 1, 0x7F, 0x80, 0xCC0000, 0x123456, 0xFFFFFF, 1066, 234266]:
        check("serato32", st.serato32_decode(st.serato32_encode(v)) == v, hex(v))
    check("serato32 red", st.serato32_encode(0xCC0000) == b"\x06\x30\x00\x00")


def test_markers2_roundtrip():
    m = sample_markers()
    data = st.encode_markers2_id3(m)
    check("markers2 size", len(data) >= 470 and data[:2] == b"\x01\x01")
    d = st.decode_markers2(data)
    check("markers2 cues", [(c.index, c.position_ms, c.name, c.color) for c in d.cues] == [(c.index, c.position_ms, c.name, c.color) for c in m.cues])
    check("markers2 loops", [(l.index, l.start_ms, l.end_ms, l.name, l.locked) for l in d.loops] == [(l.index, l.start_ms, l.end_ms, l.name, l.locked) for l in m.loops])
    check("markers2 lock/color", d.bpm_locked is True and d.track_color == (255, 255, 255))
    # independent parser
    ref = list(ref_m2.parse(data))
    ref_cues = [(e.index, e.position, e.name) for e in ref if isinstance(e, ref_m2.CueEntry)]
    check("markers2 ref-parser cues", ref_cues == [(c.index, c.position_ms, c.name) for c in m.cues])
    ref_loops = [(e.index, e.startposition, e.endposition, e.name, e.locked) for e in ref if isinstance(e, ref_m2.LoopEntry)]
    check("markers2 ref-parser loops", ref_loops == [(l.index, l.start_ms, l.end_ms, l.name, l.locked) for l in m.loops])
    # MP4 variant
    mp4 = st.encode_markers2_mp4(m)
    check("markers2 mp4 no padding chars", b"=" not in mp4 and mp4.endswith(b"\n"))
    inner = st.mp4_unwrap(mp4, "Serato Markers2")
    d2 = st.decode_markers2(inner)
    check("markers2 mp4 roundtrip", [(c.index, c.position_ms) for c in d2.cues] == [(c.index, c.position_ms) for c in m.cues])
    # empty markers still produce a valid tag
    e = st.decode_markers2(st.encode_markers2_id3(st.SeratoMarkers(cues=[], loops=[])))
    check("markers2 empty", e.cues == [] and e.loops == [])


def test_markers_legacy_roundtrip():
    m = sample_markers()
    data = st.encode_markers_id3(m)
    check("markers_ size", len(data) == 318)
    d = st.decode_markers_id3(data)
    check("markers_ cues (first 5)", [(c.index, c.position_ms, c.color) for c in d.cues] == [(c.index, c.position_ms, c.color) for c in m.cues[:5]])
    check("markers_ loops", [(l.index, l.start_ms, l.end_ms, l.locked) for l in d.loops] == [(l.index, l.start_ms, l.end_ms, l.locked) for l in m.loops])
    ref = list(ref_m1.parse(io.BytesIO(data)))
    ref_cues = [(i, e.start_position) for i, e in enumerate(ref[:5]) if e.start_position_set]
    check("markers_ ref-parser cues", ref_cues == [(c.index, c.position_ms) for c in m.cues[:5]])
    ref_loops = [(i - 5, e.start_position, e.end_position) for i, e in enumerate(ref[5:14], start=5) if e.start_position_set]
    check("markers_ ref-parser loops", ref_loops == [(l.index, l.start_ms, l.end_ms) for l in m.loops])
    mp4 = st.encode_markers_mp4(m)
    d2 = st.decode_markers_mp4_payload(st.mp4_unwrap(mp4, "Serato Markers_"))
    check("markers_ mp4 roundtrip", [(c.index, c.position_ms, c.color) for c in d2.cues] == [(c.index, c.position_ms, c.color) for c in m.cues[:5]] and d2.track_color == (255, 255, 255))


def test_beatgrid_roundtrip():
    g = [st.BeatGridMarker(0.5, 64), st.BeatGridMarker(31.5, 16), st.BeatGridMarker(39.0, 0, 128.0)]
    data = st.encode_beatgrid(g)
    check("beatgrid size", len(data) == 6 + 8 * 3)
    d = st.decode_beatgrid(data)
    check("beatgrid roundtrip", [(round(m.position_sec, 4), m.beats_to_next, round(m.bpm, 2)) for m in d] == [(0.5, 64, 0.0), (31.5, 16, 0.0), (39.0, 0, 128.0)])
    d2 = st.decode_beatgrid(st.mp4_unwrap(st.encode_beatgrid_mp4(g), "Serato BeatGrid"))
    check("beatgrid mp4 roundtrip", len(d2) == 3 and abs(d2[-1].bpm - 128.0) < 1e-4)


def test_real_serato_files():
    """Re-encode tags Serato wrote itself (saved as fixtures before we overwrote the files)."""
    import base64
    import json

    fx = Path(__file__).resolve().parent / "fixtures"
    mp3 = fx / "serato2-mp3-tags.json"
    if mp3.exists():
        d = json.loads(mp3.read_text())
        m2 = base64.b64decode(d["GEOB:Serato Markers2"])
        m1 = base64.b64decode(d["GEOB:Serato Markers_"])
        bg = base64.b64decode(d["GEOB:Serato BeatGrid"])
        dec = st.decode_markers2(m2)
        check("real mp3 markers2 byte-identical", st.encode_markers2_id3(dec, min_size=len(m2)) == m2)
        dec1 = st.decode_markers_id3(m1)
        check("real mp3 markers_ byte-identical", st.encode_markers_id3(dec1) == m1)
        check("real mp3 markers_/markers2 agree", [(c.index, c.position_ms, c.color) for c in dec1.cues] == [(c.index, c.position_ms, c.color) for c in dec.cues if c.index < 5])
        check("real mp3 beatgrid byte-identical", st.encode_beatgrid(st.decode_beatgrid(bg)) == bg)
        check("real mp3 has a cue", len(dec.cues) == 1)
    else:
        print("  (skip: fixture serato2-mp3-tags.json not found)")
    m4a = fx / "serato2-m4a-tags.json"
    if m4a.exists():
        d = json.loads(m4a.read_text())
        raw1 = st.mp4_unwrap(base64.b64decode(d["----:com.serato.dj:markers"]), "Serato Markers_")
        dec1 = st.decode_markers_mp4_payload(raw1)
        check("real m4a markers_ payload identical", st.markers_mp4_payload(dec1) == raw1[: 6 + 19 * 14 + 4])
        raw2 = st.mp4_unwrap(base64.b64decode(d["----:com.serato.dj:markersv2"]), "Serato Markers2")
        dec2 = st.decode_markers2(raw2)
        check("real m4a markers2 decodes", dec2.track_color == (255, 255, 255) and dec2.cues == [])
        redo = st.decode_markers2(st.mp4_unwrap(st.encode_markers2_mp4(dec2), "Serato Markers2"))
        check("real m4a markers2 semantic roundtrip", redo.track_color == dec2.track_color and redo.cues == dec2.cues and redo.loops == dec2.loops)
    else:
        print("  (skip: fixture serato2-m4a-tags.json not found)")
    s4 = fx / "serato4-m4a-tags.json"
    if s4.exists():
        d = json.loads(s4.read_text())
        raw = base64.b64decode(d["----:com.serato.dj:beatgrid"])
        grid = st.decode_beatgrid(st.mp4_unwrap(raw, "Serato BeatGrid"))
        check("serato4 m4a beatgrid re-encode identical", st.encode_beatgrid_mp4(grid) == raw, repr(raw[-12:]))
    else:
        print("  (skip: fixture serato4-m4a-tags.json not found)")


def test_database_v2():
    db = seratolib.BOOT_SERATO_DIR / "database V2"
    if db.exists():
        raw = db.read_bytes()
        records = seratolib.parse_database(raw)
        check("database V2 parses", len(records) >= 1 and all("pfil" in r for r in records))
        # Re-encode the first record from parsed fields and compare bytes
        chunks = list(seratolib.iter_chunks(raw))
        otrk_raw = [p for t, p in chunks if t == "otrk"][0]
        fields = {t: seratolib.decode_value(t, p) for t, p in seratolib.iter_chunks(otrk_raw)}
        if "ulbl" not in fields and "tlbl" not in fields:
            dbt = seratolib.DbTrack(
                pfil=fields["pfil"], ttyp=fields["ttyp"], tsng=fields.get("tsng", ""), tart=fields.get("tart", ""),
                talb=fields.get("talb", ""), tgen=fields.get("tgen", ""), tcom=fields.get("tcom", ""), tlen=fields.get("tlen", ""),
                tsiz=fields.get("tsiz", ""), tbit=fields.get("tbit", ""), tsmp=fields.get("tsmp", ""), tbpm=fields.get("tbpm", ""),
                tcmp=fields.get("tcmp", ""), ttyr=fields.get("ttyr", ""), tadd=fields.get("tadd", ""), tkey=fields.get("tkey", ""),
                uadd=fields.get("uadd", 0), utkn=fields.get("utkn", 0), utme=fields.get("utme", 0), ufsb=fields.get("ufsb", 0),
                udsc=fields.get("udsc", 0), utpc=fields.get("utpc", 0), bply=bool(fields.get("bply", False)), bbgl=bool(fields.get("bbgl", False)),
            )
            enc = dbt.encode()
            check("database V2 record re-encoded byte-identical", enc == seratolib._chunk("otrk", otrk_raw), f"{len(enc)} vs {len(otrk_raw) + 8}")
    else:
        print("  (skip: no database V2 yet)")
    t = seratolib.DbTrack(pfil="Users/x/a.mp3", tsng="Æble", tbpm="124.02")
    parsed = seratolib.parse_database(seratolib.encode_database([t]))
    check("database V2 roundtrip", parsed[0]["pfil"] == "Users/x/a.mp3" and parsed[0]["tsng"] == "Æble" and parsed[0]["uadd"] == 0)
    check("fmt_length", seratolib.fmt_length(167.167) == "02:47.17" and seratolib.fmt_length(3661) == "61:01.00")
    check("fmt_size", seratolib.fmt_size(6102999) == "5.8MB")


def test_crates():
    paths = ["Users/x/Music/Å.mp3", "Users/x/b.m4a"]
    data = seratolib.encode_crate(paths)
    check("crate header", data[:4] == b"vrsn" and struct.unpack(">I", data[4:8])[0] == 2 * len(seratolib.CRATE_VERSION))
    check("crate roundtrip", seratolib.parse_crate(data) == paths)
    check("crate default columns", [n for n, _ in seratolib.parse_crate_columns(data)] == [n for n, _ in seratolib.DEFAULT_COLUMNS])
    check("parse_columns", seratolib.parse_columns("song, bpm:40,added") == [("song", "250"), ("bpm", "40"), ("added", "250")])
    fx = Path(__file__).resolve().parent / "fixtures" / "serato4-export.crate"
    if fx.exists():
        raw = fx.read_bytes()
        cols = seratolib.parse_crate_columns(raw)
        tracks = seratolib.parse_crate(raw)
        check("serato4 crate re-encoded byte-identical", seratolib.encode_crate(tracks, cols) == raw, f"{len(tracks)} tracks, cols {cols}")
    order = ["A", "A%%B", "C"]
    check("neworder roundtrip", seratolib.parse_neworder(seratolib.encode_neworder(order)) == order)
    real = seratolib.BOOT_SERATO_DIR / "neworder.pref"
    if real.exists():
        check("neworder real file parses", isinstance(seratolib.parse_neworder(real.read_bytes()), list))
    check("split_volume boot", seratolib.split_volume("/Users/x/a.mp3") == ("/", "Users/x/a.mp3"))
    check("split_volume ext", seratolib.split_volume("/Volumes/DJ/M/a.mp3") == ("/Volumes/DJ", "M/a.mp3"))
    check("crate_safe_name", convert.crate_safe_name(" HH & R'N'B / SOUL ") == "HH & R'N'B - SOUL")


def test_conversion():
    beats = [(1 + i % 4, 12402, 311 + i * 484) for i in range(16)]  # starts on a downbeat
    m = rekordbox.beats_to_markers(beats)
    check("beats_to_markers single tempo", len(m) == 1 and m[0].time_ms == 311 and abs(m[0].bpm - 124.02) < 1e-9 and m[0].beats_to_next == 0)
    beats = [((3 + i - 1) % 4 + 1, 12000, 100 + i * 500) for i in range(8)]  # first beat is beat 3
    m = rekordbox.beats_to_markers(beats)
    check("beats_to_markers downbeat anchor", m[0].beat == 1 and m[0].time_ms == 100 + 2 * 500)
    beats = [(1 + i % 4, 12000, i * 500) for i in range(8)] + [(1 + i % 4, 14000, 4000 + i * 428) for i in range(8)]
    m = rekordbox.beats_to_markers(beats)
    check("beats_to_markers two tempos", len(m) == 2 and m[0].beats_to_next == 8 and m[1].beats_to_next == 0 and abs(m[1].bpm - 140) < 1e-9)
    tr = Track(id="1", path="/x/a.mp3", grid=m)
    g = convert.serato_grid(tr)
    check("serato_grid", len(g) == 2 and g[0].beats_to_next == 8 and abs(g[1].bpm - 140) < 1e-6 and abs(g[1].position_sec - 4.0) < 1e-6)
    tr2 = Track(id="2", path="/x/b.mp3", grid=[GridMarker(1000.0, 120.0, beat=3, beats_to_next=0)])
    g2 = convert.serato_grid(tr2)
    check("serato_grid shifts to downbeat", abs(g2[0].position_sec - 2.0) < 1e-6)

    check("kind_to_slot", [rekordbox.kind_to_slot(k) for k in (0, 1, 2, 3, 5, 6, 7, 8, 9, 10)] == [None, 0, 1, 2, 3, 4, 5, 6, 7, 8])
    for src, want in [("8A", "Am"), ("5B", "Eb"), ("F#m", "F#m"), ("Bbm", "Bbm"), ("1m", "Am"), ("12d", "F"), ("Amin", "Am"), ("Db", "Db"), ("", ""), ("weird", "weird"), ("10a", "Bm")]:
        check(f"normalize_key {src}", convert.normalize_key(src) == want, convert.normalize_key(src))

    cues = [Cue(311, slot=0, name="CUE(Auto)", rgb=(0xFF, 0x37, 0x6F))] + [Cue(1000 * s, slot=s, name="CUE(Auto)") for s in range(1, 9)] + [Cue(500), Cue(700, end_ms=1200), Cue(100)]
    tr3 = Track(id="3", path="/x/c.mp3", bpm=124.0, cues=cues, grid=[GridMarker(311.0, 124.0, 1, 0)])
    p = convert.plan_track(tr3, convert.Options())
    check("plan 8 cue slots filled", [c.index for c in p.markers.cues] == list(range(8)))
    check("plan overflow hot cue dropped", p.dropped_hot == 1 and p.dropped_memory == 2)
    check("plan memory loop -> loop 0", len(p.markers.loops) == 1 and p.markers.loops[0].index == 0 and p.markers.loops[0].end_ms == 1200)
    check("plan auto names stripped", all(c.name == "" for c in p.markers.cues))
    check("plan colour mapped", p.markers.cues[0].color == st.nearest_palette_color((0xFF, 0x37, 0x6F)))
    check("plan lock", p.markers.bpm_locked is True and p.bpm == 124.0)
    p2 = convert.plan_track(tr3, convert.Options(memory_cues="first"))
    check("plan memory first", p2.markers.cues[0].position_ms == 100 and p2.markers.cues[1].position_ms == 500 and p2.dropped_hot == 3)
    p3 = convert.plan_track(tr3, convert.Options(memory_cues="skip", keep_auto_names=True, lock_grid=False))
    check("plan skip memory", p3.dropped_memory == 3 and p3.markers.loops == [] and p3.markers.cues[0].name == "CUE(Auto)" and not p3.markers.bpm_locked)
    tr4 = Track(id="4", path="/x/d.mp3", cues=[Cue(1000, slot=1), Cue(2000, slot=9)])
    p4 = convert.plan_track(tr4, convert.Options())
    check("plan hot cue beyond H takes a free slot", [c.index for c in p4.markers.cues] == [0, 1] and p4.markers.cues[0].position_ms == 2000)

    tracks = {"a": Track(id="a", path="/tmp/a.mp3"), "b": Track(id="b", path="/tmp/b.mp3"), "s": Track(id="s", path="spotify:track:x")}
    root = Node("root", "", True, [
        Node("f", "Imported Playlists", True, [
            Node("p1", "MAIN & POP", False, track_ids=["a", "b", "s"]),
            Node("f2", "HOUSE", True, [Node("p2", "DEEP/HOUSE", False, track_ids=["b"]), Node("p3", "Empty", False)]),
            Node("f3", "Empty folder", True, []),
        ]),
        Node("p0", "CUE Analysis Playlist", False),
    ])
    lib = Library(tracks=tracks, root=root)
    specs = convert.crate_specs(lib, convert.Options())
    names = [(s.name, len(s.track_ids)) for s in specs]
    check("crate_specs tree", names == [("Imported Playlists", 0), ("Imported Playlists%%MAIN & POP", 2), ("Imported Playlists%%HOUSE", 0), ("Imported Playlists%%HOUSE%%DEEP-HOUSE", 1)], str(names))
    specs2 = convert.crate_specs(lib, convert.Options(unwrap="Imported Playlists"))
    check("crate_specs unwrap", [s.name for s in specs2] == ["MAIN & POP", "HOUSE", "HOUSE%%DEEP-HOUSE"], str([s.name for s in specs2]))


def test_xml():
    fx = Path(__file__).resolve().parent / "fixtures" / "rekordbox-export.xml"
    lib = rbxml.load_xml(str(fx))
    check("xml tracks", sorted(lib.tracks) == ["1", "2", "3"])
    t1 = lib.tracks["1"]
    check("xml location decoded", t1.path == "/Users/dj/Music/Test Tracks/Track One (Dirty).mp3", t1.path)
    check("xml unicode location", lib.tracks["2"].path == "/Users/dj/Music/Test Tracks/Æbler øg ål.m4a", lib.tracks["2"].path)
    check("xml streaming track", not lib.tracks["3"].is_file)
    check("xml fields", t1.bpm == 124.02 and t1.key == "8A" and t1.length_sec == 241 and t1.track_no == 3 and t1.year == 2019 and t1.comment == "cmt")
    cues = t1.cues
    check("xml cues", [(c.slot, c.start_ms, c.end_ms, c.name) for c in cues] == [(0, 311, None, "CUE(Auto)"), (1, 4181, None, ""), (2, 8000, 9935, "Loop"), (None, 15517, None, ""), (None, 21323, None, "")], str([(c.slot, c.start_ms, c.end_ms) for c in cues]))
    check("xml cue colour", cues[0].rgb == (255, 55, 111) and cues[3].rgb is None)
    g = t1.grid
    check("xml tempo", len(g) == 1 and g[0].beat == 3 and abs(g[0].time_ms - 311) < 1e-6)
    sg = convert.serato_grid(t1)
    check("xml grid downbeat shift", abs(sg[0].position_sec - (0.311 + 2 * 60 / 124.02)) < 1e-6 and abs(sg[0].bpm - 124.02) < 1e-9)
    g2 = lib.tracks["2"].grid
    check("xml two tempos", len(g2) == 2 and g2[0].beats_to_next == 64 and g2[1].bpm == 128.0, str([(m.time_ms, m.bpm, m.beats_to_next) for m in g2]))
    p = convert.plan_track(t1, convert.Options())
    check("xml plan", [c.index for c in p.markers.cues] == [0, 1, 2, 3] and len(p.markers.loops) == 1 and p.markers.loops[0].index == 2 and p.key == "Am")
    specs = convert.crate_specs(lib, convert.Options())
    check("xml playlist tree", [(s.name, len(s.track_ids)) for s in specs] == [("Imported Playlists", 0), ("Imported Playlists%%MAIN", 2), ("Imported Playlists%%HOUSE", 0), ("Imported Playlists%%HOUSE%%DEEP", 1)], str([s.name for s in specs]))
    check("view columns", seratodb.view_column_names(seratolib.parse_columns("lock,color,number,song,artist,bpm")) == "\x1e".join(["is_missing", "type", "color", "list_order", "name", "artist", "bpm"]))


def test_files():
    """End-to-end on copies of real files from the collection."""
    xml = os.environ.get("RB2SERATO_TEST_XML")
    if xml:
        lib = rbxml.load_xml(xml)
    else:
        try:
            con = rekordbox.open_db()
        except rekordbox.RekordboxError as e:
            print(f"  (skip: {e})")
            return
        lib = rekordbox.load_library(con)
        con.close()
    tracks = [t for t in lib.tracks.values() if t.is_file and os.path.exists(t.path)]
    picks = {}
    for t in sorted(tracks, key=lambda t: t.path):
        if not t.cues or not t.grid:
            continue
        key = t.ext
        if key in (".mp3", ".m4a", ".wav", ".aif") and key not in picks:
            picks[key] = t
        if len(picks) == 4:
            break
    # also a WAV without any tags, if there is one
    for t in tracks:
        if t.ext == ".wav" and t.cues:
            import mutagen

            f = mutagen.File(t.path)
            if f is not None and f.tags is None:
                picks[".wav-notags"] = t
                break
    tmp = Path(tempfile.mkdtemp(prefix="rb2serato-test-"))
    opts = convert.Options()
    try:
        for label, t in picks.items():
            dst = tmp / (label.strip(".") + t.ext)
            shutil.copyfile(t.path, dst)
            before_digest = tagwriter.audio_digest(str(dst))
            plan = convert.plan_track(t, opts)
            payload = tagwriter.TagPayload(markers=plan.markers, grid=plan.grid, key=plan.key, bpm=plan.bpm)
            undo = tagwriter.UndoLog(str(tmp / "undo.jsonl")).open()
            before = tagwriter.snapshot(str(dst))
            r = tagwriter.apply(str(dst), payload, undo=undo)
            undo.close()
            check(f"e2e {label} written", r in ("written", "unchanged"), r)  # unchanged: the file already carries these tags
            check(f"e2e {label} audio untouched", tagwriter.audio_digest(str(dst)) == before_digest)
            got = tagwriter.read_serato(str(dst))
            want = [(c.index, c.position_ms, c.name, c.color) for c in plan.markers.cues]
            have = [(c.index, c.position_ms, c.name, c.color) for c in got["markers2"].cues] if got["markers2"] else None
            check(f"e2e {label} cues", have == want, f"{have} != {want}")
            have1 = [(c.index, c.position_ms) for c in got["markers"].cues] if got["markers"] else None
            check(f"e2e {label} legacy cues", have1 == [(c.index, c.position_ms) for c in plan.markers.cues if c.index < 5])
            check(f"e2e {label} grid", got["grid"] is not None and abs(got["grid"][-1].bpm - plan.grid[-1].bpm) < 0.01)
            check(f"e2e {label} key", (got["key"] or "") == plan.key, f"{got['key']} != {plan.key}")
            check(f"e2e {label} idempotent", tagwriter.apply(str(dst), payload) == "unchanged")
            # independent parsers on the written file
            if label in (".mp3", ".wav", ".aif", ".wav-notags"):
                import mutagen

                f = mutagen.File(str(dst))
                refc = [(e.index, e.position) for e in ref_m2.parse(f.tags.getall("GEOB:Serato Markers2")[0].data) if isinstance(e, ref_m2.CueEntry)]
                check(f"e2e {label} ref-parser", refc == [(c.index, c.position_ms) for c in plan.markers.cues])
            tagwriter.undo_file(str(dst), before)
            check(f"e2e {label} undo restores tags", tagwriter.snapshot(str(dst)) == before)
            check(f"e2e {label} undo audio untouched", tagwriter.audio_digest(str(dst)) == before_digest)
            print(f"  e2e {label}: {t.display} - {len(plan.markers.cues)} cues, {len(plan.markers.loops)} loops, grid {len(plan.grid)} marker(s), key {plan.key!r}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    tests = [test_serato32, test_markers2_roundtrip, test_markers_legacy_roundtrip, test_beatgrid_roundtrip, test_real_serato_files, test_database_v2, test_crates, test_conversion, test_xml]
    if "--files" in argv:
        tests.append(test_files)
    for t in tests:
        print(f"{t.__name__}")
        try:
            t()
        except Exception as e:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            FAILS.append(f"{t.__name__}: crashed: {e}")
    print(f"\n{PASSES} checks ok, {len(FAILS)} failed")
    for f in FAILS:
        print("  - " + f)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())

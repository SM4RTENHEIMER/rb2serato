# rb2serato

Move a Rekordbox 6/7 library to **Serato DJ Pro 4**: the folder and playlist tree as
crates and subcrates, and hot cues, memory cues, loops, beatgrids, keys and BPM written
into the audio files where Serato keeps them.

No paid converter, no XML fiddling in Serato, no Serato-side "Analyze" step to lose
your grids. Built and verified on a 9,600-track library (MP3, M4A, WAV, AIFF), macOS,
Serato DJ Pro 4.0.

```
git clone https://github.com/SM4RTENHEIMER/rb2serato.git
cd rb2serato
./setup.sh
./rb2serato plan --xml Collection.xml
```

`setup.sh` is a one-time step that creates a virtualenv with the two dependencies. Then:

| Command | What it does |
|---|---|
| `./rb2serato plan --xml Collection.xml` | Shows what would happen; changes nothing |
| `./rb2serato tags --xml Collection.xml` | Writes cues, loops, beatgrid, key and BPM into the files |
| `./rb2serato crates --xml Collection.xml` | Writes crates and the database into `~/Music/_Serato_` (Serato closed) |
| `./rb2serato columns --xml Collection.xml` | Optional: column layout of the new crates (Serato closed) |
| `./rb2serato verify --xml Collection.xml` | Reads everything back and compares |
| `./rb2serato status --xml Collection.xml` | Key figures for Rekordbox and Serato, and the latest undo log |
| `./rb2serato undo` | Puts the files' tags and `_Serato_` back |

`./rb2serato run` does `tags` and `crates` in one go.

> **This writes into your music files and into Serato's library.** Every change is
> reversible with `undo` (the old tag bytes are logged before a file is touched, and the
> `_Serato_` folder and Serato's SQLite files are backed up), and each file is rewritten on
> a clone that is only swapped in after the audio data has been proven byte-identical.
> Still: have a backup, run `plan` first, and try `tags --limit 20` before the whole library.

## Step by step

The order matters: Serato has to be closed for some steps and has to have imported the
crates before others.

1. **Back up** your music and `~/Music/_Serato_` (a Time Machine backup is fine).
2. In Rekordbox: **File → Export Collection in xml format**, e.g. to
   `~/Desktop/Collection.xml`.
3. Clone this repository and run `./setup.sh`. The first time `/usr/bin/python3` runs,
   macOS may offer to install the Xcode Command Line Tools; accept.
4. `./rb2serato plan --xml ~/Desktop/Collection.xml`. Read what it says about missing
   files, cues that do not fit in Serato's 8 slots, and the crate tree. The full report is
   in `out/plan.json`.
5. Try 20 files: `./rb2serato tags --xml ~/Desktop/Collection.xml --limit 20`. Check a few of
   them in Serato (drag them in if Serato does not have them yet). If something looks wrong,
   `./rb2serato undo` puts them back.
6. All files: the same command without `--limit`. Progress is printed every 250 files.
7. **Quit Serato**, then `./rb2serato crates --xml ~/Desktop/Collection.xml`.
8. Start Serato and let it import the library; the first import can take a few minutes.
   The Rekordbox playlists are now crates.
9. Optional, for the column layout: **quit Serato** again, run
   `./rb2serato columns --xml ~/Desktop/Collection.xml`, and start Serato. This only works
   after step 8, because it edits the crates Serato has imported.
10. `./rb2serato verify --xml ~/Desktop/Collection.xml` reads every file back and compares.
    Add `--serato-db` to also count what ended up in Serato's library.

Keep the rb2serato folder afterwards: the undo logs and the backups of `_Serato_` and
Serato's database live in its `state/` folder, and `undo` needs them.

Tracks on an external drive get their crates in that drive's own `_Serato_` folder, which
is where Serato looks for them. The drive must be connected during step 7.

## Input: XML export or master.db

`--xml` takes Rekordbox's own export (**File → Export Collection in xml format**). That is
the recommended path; it needs nothing else.

Without `--xml`, rb2serato reads `master.db` directly (Rekordbox can stay open, the WAL is
replayed, beatgrids come from the ANLZ analysis files). That database is SQLCipher-encrypted
with a key that belongs to AlphaTheta and is **not** shipped here. If you have it, put it in
`~/.rb2serato/key` or the `REKORDBOX_DB_KEY` environment variable; pyrekordbox's
documentation explains where it comes from.

## What goes where

| Rekordbox | Serato | Notes |
|---|---|---|
| Hot cues A–H | Cues 1–8 | Same slot. In `master.db` Rekordbox numbers hot cues 1,2,3,5,6,7,8,9 (4 is never used); handled. |
| Hot cues I–P | First free cue slot | Serato has 8; the rest are dropped and counted by `plan`. |
| Memory cues | Free cue slots, in time order | `--memory-cues first` lets memory cues win over hot cues; `skip` leaves them out. |
| Loops | Saved loops 1–8 | |
| Cue name `CUE(Auto)` | empty | Rekordbox's automatic label; `--keep-auto-names` keeps it. |
| Cue colour | Nearest of Serato's 18 cue colours | `--cue-colors serato` uses Serato's per-slot defaults instead. |
| Beatgrid (static or dynamic) | Beatgrid markers, **locked** | The lock stops Serato's analysis from replacing the grid; `--no-lock-grid` disables it. The first marker is put on a downbeat so bar lines match. |
| Key | Standard notation (`F#m`, `Eb`) | Camelot (`8A`) and Open Key (`1m`) are translated; Serato displays whatever notation you choose in Serato. `--no-key` skips it. |
| BPM | Beatgrid BPM + `TBPM`/`tmpo` | `--no-bpm` skips the tag. |
| Folders | Crates with subcrates | `--unwrap "Imported Playlists"` lifts one top-level folder's contents to the top. |
| Empty playlists | Skipped | `--keep-empty` creates them anyway. |
| Track colour, rating, My Tag, intelligent playlists | Not converted | |

Positions are transferred unchanged. On 1,563 MP3s analysed by both apps, Serato's grid
sat a median 8 ms before Rekordbox's, with wide spread; that is under 2 % of a beat, so no
correction is applied. `--mp3-offset-ms` exists if you want one.

## How it works (and what Serato 4 requires)

**Files.** Serato stores cues, loops and the beatgrid inside the audio files: ID3 `GEOB`
frames in MP3/AIFF/WAV (`Serato Markers2`, the legacy `Serato Markers_`, `Serato BeatGrid`)
and `----:com.serato.dj:*` atoms in M4A. rb2serato writes those three, plus key and BPM.
The encoders reproduce tags written by Serato itself byte for byte (see the self-test) and
were cross-checked against the reference parsers from [Holzhaus/serato-tags] and Mixxx.
`Serato Markers_` matters: Serato prefers it over `Markers2` for cues 1–5, so both are written.

**Library.** Serato DJ Pro 4 keeps its library in SQLite, but it still exports the old
format (`database V2`, `Subcrates/*.crate`, `neworder.pref` in `~/Music/_Serato_`) and
**imports it automatically at start-up when those files changed** since it last saw them.
`crates` writes exactly that. Things the Serato 4 importer is strict about, all learned
the hard way:

- the crate header must be exactly `1.0/Serato ScratchLive Crate`; anything else creates
  the crate (from the file name) but silently skips its contents,
- WAV files must be typed `wave` in `database V2`, not `wav`, or they are dropped,
- Serato must be **closed** while the files are written, because on quit it exports its
  own state over them,
- the `ovct` column entries in a crate file only reach the "DJ" column set; the library
  view of an imported crate stays on the title column alone. `columns` fixes that directly
  in Serato's SQLite (`container_asset_list_columns`, usage 1, fields separated by ASCII
  0x1e), with backups.

Serato matches crate contents to tracks by path, so re-running `crates` after changes is
safe: existing tracks just get their crates attached.

**Safety.** A file is modified on an APFS clone; the audio payload of the clone is
compared with the original; only then is the clone renamed into place. Finder-locked
files are handled and re-locked. The tags replaced are logged to `state/undo-*.jsonl`.

## After the import

Serato analyses each track the first time it is loaded (waveform, auto gain). Untick
**Set Key** under *Analysis* if you want to keep Rekordbox's keys; the beatgrid is locked and
untouched. Cue colours look a little brighter in Serato than the stored values; that is
normal.

## Layout

```
rb2serato              run it
rb2serato-selftest     self-test; add --files for an end-to-end run on copies of your own files
src/rb2serato/
  rbxml.py             Rekordbox XML export reader
  rekordbox.py         master.db decryption + WAL, collection, cues, playlists, ANLZ beatgrids
  seratotags.py        Markers2 / Markers_ / BeatGrid encoders and decoders
  tagwriter.py         MP3/AIFF/WAV/M4A writing, undo log, integrity check
  seratolib.py         database V2, .crate, neworder.pref, paths, backups
  seratodb.py          the one edit made in Serato's SQLite: crate column layout
  convert.py           Rekordbox -> Serato: slots, loops, grid, key names, crate tree
  cli.py               command line
tests/                 self-test, fixtures (tags and a crate written by Serato, a small XML export)
```

Requires Python 3.9+ (macOS ships one), `mutagen` and `cryptography`. macOS only for now:
the Serato paths, APFS cloning and file flags are Mac-specific.

## Credits

The tag formats rest on Jan Holthuis's [serato-tags] documentation and on Mixxx's Serato
code; the Rekordbox database handling on [pyrekordbox]; the Rekordbox cue colour table on
[beat-link]. Reference parsers from serato-tags are vendored under `tests/vendor` (MIT).

## Licence

MIT, see [LICENSE](LICENSE). The vendored test parsers keep their own MIT licence in
`tests/vendor/LICENSE`.

rb2serato is an independent project and is not affiliated with or endorsed by Serato or
AlphaTheta (Rekordbox). It ships no code, keys or assets from either.

[Holzhaus/serato-tags]: https://github.com/Holzhaus/serato-tags
[serato-tags]: https://github.com/Holzhaus/serato-tags
[pyrekordbox]: https://github.com/dylanljones/pyrekordbox
[beat-link]: https://github.com/Deep-Symmetry/beat-link

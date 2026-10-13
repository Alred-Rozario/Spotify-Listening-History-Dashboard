# Member 1 - Spotify Data Ingestion

Turns the Spotify export zip into one clean, analysis-ready file:
**`data/processed/master_tracks.parquet`** (one row per play).

Teammates (Members 2-4) do **not** need Spotify credentials. They either use the committed
parquet directly, or re-run `python ingest.py` (offline) to rebuild it.

```
spotify-ingestion/
  ingest.py              CLI entry point
  ingestion/             loader.py -> clean.py -> enrich.py -> pipeline.py
  tests/                 pytest suite (python -m pytest)
  data/raw/              put the Spotify .zip here (git-ignored)
  data/processed/        master_tracks.parquet, .csv, ingest_report.json
  data/cache/            enrichment.sqlite (API results; safe to commit, makes runs resumable)
  .env.example           copy to .env (only needed for enrichment)
```

## Setup (one time)

**macOS / Linux**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Windows (PowerShell)**
```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1      # if blocked: Set-ExecutionPolicy -Scope Process Bypass
pip install -r requirements.txt
```
Written for Python 3.9+ (tested here on the version above; if you hit an error on an older Mac Python, install 3.11). `tzdata` in requirements.txt is what makes timezones work on Windows.

## Run

```bash
python ingest.py                        # offline, reads data/raw/*.zip -> data/processed/
python ingest.py --export "C:\path\my_spotify_data.zip"
python ingest.py --tz Asia/Kolkata      # timezone for hour / day-of-week (default Asia/Kolkata)
python ingest.py --enrich               # online enrichment (Member 1 only, see below)
python ingest.py --enrich --enrich-limit 20   # smoke-test the APIs on 20 items first
python -m pytest                        # run tests
```
(On Windows, use `py ingest.py ...` if `python` is not found.)

Re-running is safe: `play_id` is a hash of (time, track, artist, ms_played), so ids never change
between runs. Enrichment results are cached in `data/cache/enrichment.sqlite`; an interrupted run
resumes where it stopped, and offline runs still apply whatever is cached.

## Enrichment - what changed vs. the original plan

The plan said "pull audio features and genres from the Spotify API". Spotify has since removed these
for new apps (audio features Nov 2024; artist genres and batch artist lookups Feb 2026), so:

| Data | Source | Needs |
|---|---|---|
| Spotify track id, album, duration, release date | Spotify search (spotipy) | Spotify dev app; app owner needs Premium |
| Audio features (danceability, energy, valence, tempo, ...) | ReccoBeats, looked up by Spotify id | nothing (no key) |
| Genres | Last.fm `artist.getTopTags` | free API key (optional) |

Setup for Spotify: https://developer.spotify.com/dashboard -> Create app -> select **Web API** ->
add redirect URI `http://127.0.0.1:8888/callback`. Copy `.env.example` to `.env` and fill in
`SPOTIPY_CLIENT_ID` / `SPOTIPY_CLIENT_SECRET`. First run opens a browser to log in once.

Notes:
- The export has no track ids, so tracks are matched by name + artist. Unmatched tracks keep null ids.
- Tracks you saved in your library already come with ids and need no search.
- ReccoBeats values are estimates that mimic Spotify's old numbers, not Spotify's originals.
- Expect **partial coverage** (some tracks, especially regional music, will have no features).
  Check `data/processed/ingest_report.json` for the exact percentages and tell Member 3 so the radar
  chart can handle missing data.
- Third-party endpoints change; if a source fails the pipeline logs it and carries on.

## `master_tracks.parquet` columns

| Column | Meaning |
|---|---|
| `play_id` | stable unique id per play (fact table key) |
| `track_id`, `artist_id` | stable surrogate ids (hash of normalised names) - use these for dim tables |
| `spotify_track_id`, `spotify_artist_id` | real Spotify ids, null if not matched |
| `played_at_utc` | when the play ended, UTC (Spotify's native time) |
| `played_at_local`, `played_date`, `hour`, `dow_num` (0=Mon), `day_of_week`, `month`, `year_month`, `is_weekend` | derived in `--tz` timezone; use these for the heatmap |
| `ms_played` | milliseconds listened |
| `is_zero_play` | `ms_played == 0` |
| `is_skip` | `ms_played < 30000` (under Spotify's 30s stream threshold) - rows are flagged, never deleted |
| `track_name`, `artist_name`, `album_name`, `duration_ms`, `release_date`, `genres` | descriptive (`genres` is `\|`-separated; album/duration/release/genres null if not enriched) |
| `danceability`, `energy`, `valence`, `tempo`, `acousticness`, `instrumentalness`, `liveness`, `loudness`, `speechiness`, `has_audio_features` | audio features, null when unavailable |

Every column has a fixed type even when empty (floats / strings), so DuckDB loads it the same way
before and after enrichment.

## Handoffs

- **Member 2 (warehouse):** load the parquet with `read_parquet('data/processed/master_tracks.parquet')`.
  Suggested mapping: `fact_plays` = play_id, track_id, artist_id, played_at_utc, ms_played (+ flags);
  `dim_tracks` = track_id, track_name, album_name, duration_ms + audio features (one row per track_id);
  `dim_artists` = artist_id, artist_name, genres; `dim_time` from `played_at_local`.
- **Member 3 (dashboard):** filter `is_skip = false` for "real listens", and always handle null audio features.
- **Member 4 (pipeline):** call it from `run.py` / a scheduler with
  `from ingestion import run; run("data/raw/my_spotify_data.zip", enrich_data=False)`.
  Built-in checks (unique play_id, no null keys, non-negative ms_played, hour in 0-23, audio features in 0-1)
  print `[OK]/[FAIL]` and are saved in `ingest_report.json`.

## Privacy - important before pushing to GitHub

The raw Spotify export also contains your address, payment info and device identifiers.
**Never commit the zip.** `.gitignore` already excludes `data/raw/`, `*.zip` and `.env`, and the loader
only ever reads the streaming-history and library files from the zip. The processed parquet contains your
listening history (and nothing else), which is what the team needs.

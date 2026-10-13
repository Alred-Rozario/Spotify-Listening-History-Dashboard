"""End-to-end ingestion. Import `run` from Member 4's run.py / scheduler, or use ingest.py on the CLI."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd

from . import enrich
from .clean import clean_history
from .loader import load_history, load_library_tracks

MASTER_COLUMNS = [
    # keys
    "play_id", "track_id", "artist_id", "spotify_track_id", "spotify_artist_id",
    # play facts
    "played_at_utc", "played_at_local", "played_date", "hour", "dow_num", "day_of_week",
    "month", "year_month", "is_weekend", "ms_played", "is_zero_play", "is_skip",
    # descriptive
    "track_name", "artist_name", "album_name", "duration_ms", "release_date", "genres",
    # audio features (null when not available)
    *enrich.AUDIO_COLS, "has_audio_features",
]


TEXT_COLUMNS = ["track_name", "artist_name", "album_name", "genres", "spotify_track_id",
                "spotify_artist_id", "release_date", "day_of_week", "year_month"]


def _load_env():
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass


def quality_checks(master: pd.DataFrame) -> dict:
    """Light data-quality assertions. Returns {check: passed}; failures are warnings, not crashes."""
    checks = {
        "play_id_unique": bool(master["play_id"].is_unique),
        "no_nulls_in_key_columns": bool(master[["play_id", "track_id", "artist_id", "played_at_utc",
                                                "track_name", "artist_name", "ms_played"]].notna().all().all()),
        "ms_played_non_negative": bool((master["ms_played"] >= 0).all()),
        "hour_in_range": bool(master["hour"].between(0, 23).all()),
        "audio_features_in_range": bool(
            master.loc[master["has_audio_features"], ["danceability", "energy", "valence"]]
            .stack().between(0, 1).all()) if master["has_audio_features"].any() else True,
    }
    return checks


def run(export_path: str | Path, out_dir: str | Path = "data/processed", cache_dir: str | Path = "data/cache",
        tz: str | None = None, enrich_data: bool = False, enrich_limit: int | None = None,
        write_csv: bool = True, log=print) -> pd.DataFrame:
    """Build master_tracks.parquet from a Spotify export. Returns the master DataFrame.

    Idempotent: play_id is a hash of (time, track, artist, ms_played), so re-running gives the
    same ids. enrich_data=False makes NO network calls (cached enrichment is still applied).
    """
    _load_env()
    tz = tz or os.getenv("LOCAL_TZ", "Asia/Kolkata")
    out_dir, cache_dir = Path(out_dir), Path(cache_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    log(f"[1/4] Reading export: {export_path}")
    raw, files = load_history(export_path)
    log(f"      {len(raw):,} rows from {len(files)} history file(s)")

    log("[2/4] Cleaning")
    plays, stats = clean_history(raw, tz=tz)
    log(f"      {stats['rows_clean']:,} clean plays | dropped {stats['dropped_missing_key_fields']} missing, "
        f"{stats['dropped_exact_duplicates']} duplicates | {stats['skips_under_30s']:,} skips (<30s)")

    log("[3/4] Enrichment " + ("(online)" if enrich_data else "(offline: cache only)"))
    cache = enrich.Cache(cache_dir / "enrichment.sqlite")
    try:
        if enrich_data:
            library = load_library_tracks(export_path)
            sp = None
            try:
                sp = enrich.make_spotify()
            except Exception as exc:  # noqa: BLE001
                log(f"      Spotify disabled: {exc}")
            enrich.resolve_tracks(plays, cache, library=library, sp=sp, limit=enrich_limit, log=log)
            enrich.fetch_audio_features(cache, limit=enrich_limit, log=log)
            enrich.fetch_genres(plays, cache, os.getenv("LASTFM_API_KEY"), limit=enrich_limit, log=log)
        master = enrich.apply_cache(plays, cache)
    finally:
        cache.close()

    for col in MASTER_COLUMNS:
        if col not in master:
            master[col] = pd.NA
    master = master[MASTER_COLUMNS].copy()
    # Explicit dtypes: an all-empty column (e.g. audio features before enrichment) must still be
    # written as DOUBLE / VARCHAR, otherwise DuckDB sees it as INTEGER/NULL and downstream SQL breaks.
    master["duration_ms"] = pd.to_numeric(master["duration_ms"], errors="coerce").astype("Int64")
    for col in TEXT_COLUMNS:
        master[col] = master[col].astype("string")
    for col in enrich.AUDIO_COLS:
        master[col] = pd.to_numeric(master[col], errors="coerce").astype("float64")
    master["has_audio_features"] = master["has_audio_features"].fillna(False).astype(bool)

    log("[4/4] Quality checks + writing files")
    checks = quality_checks(master)
    for name, ok in checks.items():
        log(f"      [{'OK' if ok else 'FAIL'}] {name}")

    parquet = out_dir / "master_tracks.parquet"
    master.to_parquet(parquet, index=False)
    if write_csv:
        master.to_csv(out_dir / "master_tracks.csv", index=False, encoding="utf-8-sig")  # utf-8-sig: Excel-safe

    report = {**stats, "history_files": files, "quality_checks": checks,
              "tracks_with_spotify_id": int(master.loc[master["spotify_track_id"].notna(), "track_id"].nunique()),
              "tracks_with_audio_features": int(master.loc[master["has_audio_features"], "track_id"].nunique()),
              "plays_with_audio_features_pct": round(100 * float(master["has_audio_features"].mean()), 1),
              "plays_with_genres_pct": round(100 * float(master["genres"].notna().mean()), 1)}
    (out_dir / "ingest_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    log(f"      wrote {parquet}  ({len(master):,} rows, {len(MASTER_COLUMNS)} columns)")
    log(f"      audio-feature coverage: {report['plays_with_audio_features_pct']}% of plays | "
        f"genre coverage: {report['plays_with_genres_pct']}% of plays")
    if not all(checks.values()):
        log("      WARNING: some quality checks failed - see above")
    return master

"""Clean raw plays and derive the columns the rest of the team needs."""
from __future__ import annotations

import hashlib
import re

import pandas as pd

SKIP_THRESHOLD_MS = 30_000  # Spotify counts a stream after 30 seconds


def norm(text) -> str:
    """Normalised key text: casefolded, whitespace collapsed. Empty string for nulls."""
    if text is None or (isinstance(text, float) and pd.isna(text)):
        return ""
    return re.sub(r"\s+", " ", str(text)).strip().casefold()


def _hash(*parts, n: int = 12) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:n]


def make_track_key(track_name, artist_name) -> str:
    return f"{norm(track_name)}||{norm(artist_name)}"


def clean_history(raw: pd.DataFrame, tz: str = "Asia/Kolkata") -> tuple[pd.DataFrame, dict]:
    """Return (clean DataFrame, stats dict describing what was dropped)."""
    stats = {"rows_read": int(len(raw))}
    df = raw.copy()

    for col in ("track_name", "artist_name", "album_name"):
        df[col] = df[col].astype("string").str.strip().replace("", pd.NA)

    # 1) drop rows that can't be used (null time / track / artist, e.g. podcasts in extended exports)
    bad = (df["played_at_utc"].isna() | df["track_name"].isna()
           | df["artist_name"].isna() | df["ms_played"].isna())
    stats["dropped_missing_key_fields"] = int(bad.sum())
    df = df[~bad].copy()

    # 2) drop exact duplicate plays (same minute, track, artist, ms_played)
    before = len(df)
    df = df.drop_duplicates(subset=["played_at_utc", "track_name", "artist_name", "ms_played"])
    stats["dropped_exact_duplicates"] = int(before - len(df))

    df["ms_played"] = df["ms_played"].astype("int64")
    df = df.sort_values(["played_at_utc", "track_name"]).reset_index(drop=True)

    # 3) keys / ids (stable across re-runs, so incremental loads don't create duplicates)
    df["track_key"] = [make_track_key(t, a) for t, a in zip(df["track_name"], df["artist_name"])]
    df["artist_key"] = [norm(a) for a in df["artist_name"]]
    df["track_id"] = [_hash(k) for k in df["track_key"]]
    df["artist_id"] = [_hash(k) for k in df["artist_key"]]
    ts = df["played_at_utc"].dt.strftime("%Y-%m-%dT%H:%M:%S")
    df["play_id"] = [_hash(t, k, str(ms), n=16)
                     for t, k, ms in zip(ts, df["track_key"], df["ms_played"])]

    # 4) time columns. Spotify exports are UTC; hour/day-of-week must use the listener's timezone.
    local = df["played_at_utc"].dt.tz_convert(tz)
    df["played_at_local"] = local.dt.tz_localize(None)
    df["played_at_utc"] = df["played_at_utc"].dt.tz_localize(None)  # naive UTC: DuckDB-friendly
    df["played_date"] = df["played_at_local"].dt.date
    df["hour"] = df["played_at_local"].dt.hour.astype("int8")
    df["dow_num"] = df["played_at_local"].dt.dayofweek.astype("int8")  # 0 = Monday
    df["day_of_week"] = df["played_at_local"].dt.day_name()
    df["month"] = df["played_at_local"].dt.month.astype("int8")
    df["year_month"] = df["played_at_local"].dt.strftime("%Y-%m")
    df["is_weekend"] = df["dow_num"] >= 5

    # 5) flags (rows are kept, never silently removed)
    df["is_zero_play"] = df["ms_played"] == 0
    df["is_skip"] = df["ms_played"] < SKIP_THRESHOLD_MS

    stats["rows_clean"] = int(len(df))
    stats["zero_ms_plays"] = int(df["is_zero_play"].sum())
    stats["skips_under_30s"] = int(df["is_skip"].sum())
    stats["unique_tracks"] = int(df["track_key"].nunique())
    stats["unique_artists"] = int(df["artist_key"].nunique())
    stats["first_play_local"] = str(df["played_at_local"].min())
    stats["last_play_local"] = str(df["played_at_local"].max())
    stats["timezone"] = tz
    return df, stats

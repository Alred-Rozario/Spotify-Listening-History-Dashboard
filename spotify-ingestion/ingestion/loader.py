"""Read play history from a Spotify data export (.zip or an unzipped folder).

Only the files we need are read (streaming history + library). Other files in the
export (address, payments, identifiers...) are never opened.

Supports both export types Spotify offers:
  * "Account data"      -> StreamingHistory_music_N.json   (endTime, artistName, trackName, msPlayed)
  * "Extended history"  -> Streaming_History_Audio_*.json  (ts, master_metadata_*, ms_played, ...)
"""
from __future__ import annotations

import fnmatch
import json
import zipfile
from pathlib import Path

import pandas as pd

HISTORY_PATTERNS = [
    "StreamingHistory_music_*.json",
    "StreamingHistory[0-9]*.json",
    "Streaming_History_Audio_*.json",
]
LIBRARY_NAME = "YourLibrary.json"


def _iter_members(path: Path):
    """Yield (basename, read_function) for every file in a zip or folder."""
    if path.is_file() and path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if not info.is_dir():
                    yield Path(info.filename).name, (lambda i=info, z=zf: z.read(i))
    elif path.is_dir():
        for p in sorted(path.rglob("*.json")):
            yield p.name, (lambda q=p: q.read_bytes())
    else:
        raise FileNotFoundError(f"Not a .zip file or folder: {path}")


def _matches(name: str, patterns) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in patterns)


def _read_json(raw: bytes):
    return json.loads(raw.decode("utf-8-sig"))


def _col(df: pd.DataFrame, name: str) -> pd.Series:
    return df[name] if name in df else pd.Series([None] * len(df), index=df.index)


def _normalise_basic(records: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(records)
    return pd.DataFrame({
        "played_at_utc": pd.to_datetime(df["endTime"], errors="coerce", utc=True),
        "track_name": df["trackName"],
        "artist_name": df["artistName"],
        "ms_played": pd.to_numeric(df["msPlayed"], errors="coerce"),
        "album_name": None,
        "spotify_track_uri": None,
    })


def _normalise_extended(records: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(records)
    return pd.DataFrame({
        "played_at_utc": pd.to_datetime(_col(df, "ts"), errors="coerce", utc=True),
        "track_name": _col(df, "master_metadata_track_name"),  # null for podcasts
        "artist_name": _col(df, "master_metadata_album_artist_name"),
        "ms_played": pd.to_numeric(_col(df, "ms_played"), errors="coerce"),
        "album_name": _col(df, "master_metadata_album_album_name"),
        "spotify_track_uri": _col(df, "spotify_track_uri"),
    })


def load_history(export_path: str | Path) -> tuple[pd.DataFrame, list[str]]:
    """Return (raw plays DataFrame, list of history files that were read)."""
    export_path = Path(export_path)
    frames, files = [], []
    for name, reader in _iter_members(export_path):
        if not _matches(name, HISTORY_PATTERNS):
            continue
        records = _read_json(reader())
        if not records:
            continue
        files.append(name)
        if "endTime" in records[0]:
            frames.append(_normalise_basic(records))
        elif "ts" in records[0]:
            frames.append(_normalise_extended(records))
    if not frames:
        raise ValueError(
            f"No streaming history files found in {export_path}. Expected "
            "StreamingHistory_music_*.json or Streaming_History_Audio_*.json"
        )
    return pd.concat(frames, ignore_index=True), files


def load_library_tracks(export_path: str | Path) -> pd.DataFrame:
    """Saved tracks from YourLibrary.json (they carry real Spotify URIs). May be empty."""
    cols = ["artist_name", "album_name", "track_name", "spotify_track_uri"]
    for name, reader in _iter_members(Path(export_path)):
        if name == LIBRARY_NAME:
            tracks = _read_json(reader()).get("tracks", [])
            df = pd.DataFrame(tracks, columns=["artist", "album", "track", "uri"])
            return df.rename(columns={"artist": "artist_name", "album": "album_name",
                                      "track": "track_name", "uri": "spotify_track_uri"})[cols]
    return pd.DataFrame(columns=cols)

"""Optional enrichment. Every step is cached in SQLite so runs are resumable and re-runs are free.

Why three sources? Spotify removed /audio-features for new apps (Nov 2024) and artist genres plus
batch artist lookups in Feb 2026, so:
  * Spotify Web API  -> track search: spotify_track_id, album, duration, release date
  * ReccoBeats       -> audio features, looked up by Spotify track id (free, no key)
  * Last.fm          -> artist genres/tags (free key, optional)
Anything that can't be fetched is left null; downstream code must tolerate nulls.
"""
from __future__ import annotations

import difflib
import os
import re
import sqlite3
import time
from pathlib import Path

import pandas as pd
import requests

from .clean import norm, make_track_key

AUDIO_COLS = ["danceability", "energy", "valence", "tempo", "acousticness",
              "instrumentalness", "liveness", "loudness", "speechiness"]
RECCO_URL = "https://api.reccobeats.com/v1/audio-features"
LASTFM_URL = "https://ws.audioscrobbler.com/2.0/"
GENERIC_TAGS = {"seen live", "favorites", "favourite", "favorite", "male vocalists", "female vocalists",
                "male vocalist", "female vocalist", "all", "under 2000 listeners"}

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS tracks(
  track_key TEXT PRIMARY KEY, spotify_track_id TEXT, spotify_artist_id TEXT, album_name TEXT,
  duration_ms INTEGER, release_date TEXT, source TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS features(
  spotify_track_id TEXT PRIMARY KEY, status TEXT, {", ".join(c + " REAL" for c in AUDIO_COLS)});
CREATE TABLE IF NOT EXISTS artists(
  artist_key TEXT PRIMARY KEY, genres TEXT, status TEXT);
"""


class Cache:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def commit(self):
        self.db.commit()

    def close(self):
        self.db.commit()
        self.db.close()

    def known(self, table: str, col: str) -> set:
        return {r[0] for r in self.db.execute(f"SELECT {col} FROM {table}")}

    def put_track(self, key, sid=None, aid=None, album=None, dur=None, rel=None, source="search", status="ok"):
        self.db.execute("INSERT OR REPLACE INTO tracks VALUES (?,?,?,?,?,?,?,?)",
                        (key, sid, aid, album, dur, rel, source, status))

    def put_features(self, sid, feats: dict | None):
        if feats:
            vals = [feats.get(c) for c in AUDIO_COLS]
            self.db.execute(f"INSERT OR REPLACE INTO features VALUES (?,?,{','.join('?' * len(AUDIO_COLS))})",
                            (sid, "ok", *vals))
        else:
            self.db.execute(f"INSERT OR REPLACE INTO features VALUES (?,?,{','.join('?' * len(AUDIO_COLS))})",
                            (sid, "missing", *([None] * len(AUDIO_COLS))))

    def put_artist(self, key, genres: list[str] | None):
        self.db.execute("INSERT OR REPLACE INTO artists VALUES (?,?,?)",
                        (key, "|".join(genres) if genres else None, "ok" if genres else "missing"))


# --------------------------------------------------------------------------- matching
def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


def pick_match(items: list[dict], track_name: str, artist_name: str, min_title: float = 0.80):
    """Choose the best Spotify search result, or None. Requires matching artist AND similar title."""
    best, best_score = None, 0.0
    want_artist = norm(artist_name)
    for it in items or []:
        artists = [norm(a.get("name", "")) for a in it.get("artists", [])]
        artist_ok = any(want_artist == a or want_artist in a or a in want_artist for a in artists if a)
        if not artist_ok:
            continue
        score = _ratio(it.get("name", ""), track_name)
        if score > best_score:
            best, best_score = it, score
    return best if best_score >= min_title else None


def uri_to_id(uri) -> str | None:
    m = re.match(r"spotify:track:([A-Za-z0-9]{22})$", str(uri or ""))
    return m.group(1) if m else None


# --------------------------------------------------------------------------- spotify
def make_spotify(mode: str | None = None):
    """Create a spotipy client from env vars. Raises RuntimeError with a helpful message."""
    if not (os.getenv("SPOTIPY_CLIENT_ID") and os.getenv("SPOTIPY_CLIENT_SECRET")):
        raise RuntimeError("SPOTIPY_CLIENT_ID / SPOTIPY_CLIENT_SECRET not set (see .env.example)")
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials, SpotifyOAuth
    mode = (mode or os.getenv("SPOTIFY_AUTH", "user")).lower()
    if mode == "client":
        auth = SpotifyClientCredentials()
    else:
        auth = SpotifyOAuth(
            redirect_uri=os.getenv("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:8888/callback"),
            scope=None, cache_path=".cache-spotify", open_browser=True)
    return spotipy.Spotify(auth_manager=auth, requests_timeout=20, retries=3)


def resolve_tracks(plays: pd.DataFrame, cache: Cache, library: pd.DataFrame | None = None,
                   sp=None, limit: int | None = None, log=print) -> None:
    """Fill the `tracks` cache table for every unique (track, artist) in plays."""
    uniq = plays.drop_duplicates("track_key")[["track_key", "track_name", "artist_name"]]
    done = cache.known("tracks", "track_key")

    # 1) free ids: extended-export URIs, then YourLibrary.json saved tracks
    uri_map = {}
    if "spotify_track_uri" in plays:
        for k, u in plays.dropna(subset=["spotify_track_uri"]).drop_duplicates("track_key")[
                ["track_key", "spotify_track_uri"]].itertuples(index=False):
            if uri_to_id(u):
                uri_map[k] = (uri_to_id(u), None)
    if library is not None and len(library):
        for r in library.itertuples(index=False):
            sid = uri_to_id(r.spotify_track_uri)
            if sid:
                uri_map.setdefault(make_track_key(r.track_name, r.artist_name), (sid, r.album_name))
    free = 0
    for key in uniq["track_key"]:
        if key not in done and key in uri_map:
            sid, album = uri_map[key]
            cache.put_track(key, sid=sid, album=album, source="export")
            done.add(key)
            free += 1
    cache.commit()
    if free:
        log(f"  track ids from the export itself: {free}")

    # 2) Spotify search for the rest
    todo = uniq[~uniq["track_key"].isin(done)]
    if sp is None or todo.empty:
        if sp is None and len(todo):
            log(f"  {len(todo)} tracks have no Spotify id (no Spotify credentials -> skipped)")
        return
    if limit:
        todo = todo.head(limit)
    log(f"  searching Spotify for {len(todo)} tracks (cached, safe to interrupt and re-run)...")
    errors_in_row = 0
    for i, (key, name, artist) in enumerate(todo.itertuples(index=False), 1):
        try:
            res = sp.search(q=f'track:"{name}" artist:"{artist}"', type="track", limit=5)
            items = res.get("tracks", {}).get("items", [])
            if not items:
                res = sp.search(q=f"{name} {artist}", type="track", limit=5)
                items = res.get("tracks", {}).get("items", [])
            errors_in_row = 0
        except Exception as exc:  # noqa: BLE001
            errors_in_row += 1
            log(f"  ! Spotify error on '{name}': {type(exc).__name__}: {str(exc)[:120]}")
            if errors_in_row >= 3:
                log("  ! 3 errors in a row - stopping Spotify lookups. Check credentials / Premium / auth mode.")
                break
            continue
        m = pick_match(items, name, artist)
        if m:
            cache.put_track(key, sid=m["id"], aid=(m["artists"][0].get("id") if m.get("artists") else None),
                            album=m.get("album", {}).get("name"), dur=m.get("duration_ms"),
                            rel=m.get("album", {}).get("release_date"))
        else:
            cache.put_track(key, status="not_found")
        if i % 25 == 0:
            cache.commit()
            log(f"    {i}/{len(todo)}")
        time.sleep(0.05)
    cache.commit()


# --------------------------------------------------------------------------- reccobeats
def _spotify_id_from(item: dict) -> str | None:
    """ReccoBeats items carry the Spotify track URL in `href`; be tolerant of other field names."""
    for field in ("href", "spotifyId", "spotify_id", "trackId", "id"):
        v = item.get(field)
        if not isinstance(v, str):
            continue
        m = re.search(r"track/([A-Za-z0-9]{22})", v)
        if m:
            return m.group(1)
        if re.fullmatch(r"[A-Za-z0-9]{22}", v):
            return v
    return None


def fetch_audio_features(cache: Cache, session: requests.Session | None = None,
                         limit: int | None = None, log=print) -> None:
    session = session or requests.Session()
    ids = [r[0] for r in cache.db.execute(
        "SELECT DISTINCT spotify_track_id FROM tracks WHERE spotify_track_id IS NOT NULL")]
    done = cache.known("features", "spotify_track_id")
    todo = [i for i in ids if i not in done]
    if limit:
        todo = todo[:limit]
    if not todo:
        return
    log(f"  fetching audio features for {len(todo)} tracks from ReccoBeats...")
    for start in range(0, len(todo), 40):
        batch = todo[start:start + 40]
        try:
            r = session.get(RECCO_URL, params={"ids": ",".join(batch)}, timeout=30,
                            headers={"Accept": "application/json"})
            if r.status_code == 429:
                time.sleep(float(r.headers.get("Retry-After", 5)))
                r = session.get(RECCO_URL, params={"ids": ",".join(batch)}, timeout=30)
            r.raise_for_status()
            payload = r.json()
        except Exception as exc:  # noqa: BLE001
            log(f"  ! ReccoBeats batch failed ({type(exc).__name__}: {str(exc)[:100]}); will retry next run")
            continue
        content = payload.get("content", payload) if isinstance(payload, dict) else payload
        found = {}
        for item in content or []:
            sid = _spotify_id_from(item)
            if sid:
                found[sid] = {c: item.get(c) for c in AUDIO_COLS}
        for sid in batch:
            cache.put_features(sid, found.get(sid))
        cache.commit()
        time.sleep(0.3)


# --------------------------------------------------------------------------- last.fm
def fetch_genres(plays: pd.DataFrame, cache: Cache, api_key: str | None,
                 session: requests.Session | None = None, limit: int | None = None, log=print) -> None:
    if not api_key:
        return
    session = session or requests.Session()
    uniq = plays.drop_duplicates("artist_key")[["artist_key", "artist_name"]]
    done = cache.known("artists", "artist_key")
    todo = uniq[~uniq["artist_key"].isin(done)]
    if limit:
        todo = todo.head(limit)
    if todo.empty:
        return
    log(f"  fetching genre tags for {len(todo)} artists from Last.fm...")
    for i, (key, name) in enumerate(todo.itertuples(index=False), 1):
        try:
            r = session.get(LASTFM_URL, timeout=20, params={
                "method": "artist.getTopTags", "artist": name, "api_key": api_key,
                "format": "json", "autocorrect": 1})
            tags = r.json().get("toptags", {}).get("tag", [])
        except Exception as exc:  # noqa: BLE001
            log(f"  ! Last.fm error for '{name}': {type(exc).__name__}")
            continue
        genres = [t["name"].lower() for t in tags
                  if int(t.get("count", 0)) >= 20 and t["name"].lower() not in GENERIC_TAGS
                  and norm(t["name"]) != norm(name)][:5]
        cache.put_artist(key, genres)
        if i % 25 == 0:
            cache.commit()
        time.sleep(0.25)
    cache.commit()


# --------------------------------------------------------------------------- merge
def apply_cache(plays: pd.DataFrame, cache: Cache) -> pd.DataFrame:
    """Left-join whatever is in the cache onto plays. Pure offline; never calls an API."""
    out = plays.copy()
    t = pd.read_sql_query(
        "SELECT track_key, spotify_track_id, spotify_artist_id, album_name AS sp_album, duration_ms, "
        "release_date FROM tracks WHERE status='ok'", cache.db)
    f = pd.read_sql_query(f"SELECT spotify_track_id, {', '.join(AUDIO_COLS)} FROM features WHERE status='ok'",
                          cache.db)
    a = pd.read_sql_query("SELECT artist_key, genres FROM artists WHERE status='ok'", cache.db)

    out = out.merge(t, on="track_key", how="left")
    out["album_name"] = out["album_name"].astype("object").where(out["album_name"].notna(), out["sp_album"])
    out = out.drop(columns=["sp_album"])
    out = out.merge(f, on="spotify_track_id", how="left")
    out = out.merge(a, on="artist_key", how="left")
    out["has_audio_features"] = out["energy"].notna()
    return out

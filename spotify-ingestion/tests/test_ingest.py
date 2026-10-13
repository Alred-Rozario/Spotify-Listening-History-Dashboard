import json
import zipfile

import pandas as pd
import pytest

from ingestion import enrich
from ingestion.clean import clean_history, make_track_key
from ingestion.loader import load_history, load_library_tracks
from ingestion.pipeline import MASTER_COLUMNS, run

BASIC = [
    {"endTime": "2026-03-01 18:30", "artistName": "Artist A", "trackName": "Song One", "msPlayed": 200000},
    {"endTime": "2026-03-01 18:30", "artistName": "Artist A", "trackName": "Song One", "msPlayed": 200000},  # exact dup
    {"endTime": "2026-03-01 23:00", "artistName": "Artist B", "trackName": "Song Two", "msPlayed": 0},
    {"endTime": "2026-03-02 02:10", "artistName": "artist a ", "trackName": "SONG  one", "msPlayed": 5000},
]
EXTENDED = [
    {"ts": "2026-03-01T10:00:00Z", "master_metadata_track_name": "Song One",
     "master_metadata_album_artist_name": "Artist A", "master_metadata_album_album_name": "Album X",
     "ms_played": 100000, "spotify_track_uri": "spotify:track:" + "a" * 22},
    {"ts": "2026-03-01T11:00:00Z", "master_metadata_track_name": None,  # podcast
     "master_metadata_album_artist_name": None, "ms_played": 5000, "spotify_track_uri": None},
]
LIBRARY = {"tracks": [{"artist": "Artist B", "album": "Alb", "track": "Song Two",
                       "uri": "spotify:track:" + "b" * 22}]}


@pytest.fixture
def export_zip(tmp_path):
    p = tmp_path / "export.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("Spotify Account Data/StreamingHistory_music_0.json", json.dumps(BASIC))
        z.writestr("Spotify Account Data/YourLibrary.json", json.dumps(LIBRARY))
        z.writestr("Spotify Account Data/Payments.json", '{"secret": "never read"}')
    return p


def test_loader_reads_zip_and_ignores_other_files(export_zip):
    raw, files = load_history(export_zip)
    assert files == ["StreamingHistory_music_0.json"]
    assert len(raw) == 4
    assert str(raw["played_at_utc"].dt.tz) == "UTC"


def test_loader_extended_format(tmp_path):
    (tmp_path / "Streaming_History_Audio_2026.json").write_text(json.dumps(EXTENDED), encoding="utf-8")
    raw, _ = load_history(tmp_path)
    assert raw["spotify_track_uri"].iloc[0].startswith("spotify:track:")


def test_loader_errors_when_no_history(tmp_path):
    (tmp_path / "other.json").write_text("[]")
    with pytest.raises(ValueError):
        load_history(tmp_path)


def test_clean_dedupes_flags_and_timezone(export_zip):
    raw, _ = load_history(export_zip)
    df, stats = clean_history(raw, tz="Asia/Kolkata")
    assert stats["dropped_exact_duplicates"] == 1
    assert len(df) == 3
    assert df["play_id"].is_unique
    # name variants collapse to the same track
    assert df["track_id"].nunique() == 2
    # 2026-03-01 18:30 UTC == 2026-03-02 00:00 IST -> Monday, hour 0
    first = df.iloc[0]
    assert first["hour"] == 0 and first["day_of_week"] == "Monday" and not first["is_weekend"]
    assert df["is_zero_play"].sum() == 1
    assert df["is_skip"].sum() == 2


def test_clean_drops_podcast_rows(tmp_path):
    (tmp_path / "Streaming_History_Audio_x.json").write_text(json.dumps(EXTENDED), encoding="utf-8")
    raw, _ = load_history(tmp_path)
    df, stats = clean_history(raw)
    assert len(df) == 1 and stats["dropped_missing_key_fields"] == 1


def test_play_ids_are_stable_between_runs(export_zip):
    raw, _ = load_history(export_zip)
    a, _ = clean_history(raw)
    b, _ = clean_history(raw.sample(frac=1, random_state=1))
    assert list(a["play_id"]) == list(b["play_id"])


def test_pick_match_requires_artist_and_title():
    items = [
        {"id": "1", "name": "Song One (Remix)", "artists": [{"name": "Someone Else"}]},
        {"id": "2", "name": "Song One", "artists": [{"name": "Artist A"}]},
        {"id": "3", "name": "Totally Different", "artists": [{"name": "Artist A"}]},
    ]
    assert enrich.pick_match(items, "song one", "Artist A")["id"] == "2"
    assert enrich.pick_match(items[:1], "Song One", "Artist A") is None
    assert enrich.pick_match([], "x", "y") is None


class FakeSpotify:
    def __init__(self):
        self.calls = 0

    def search(self, q, type, limit):
        self.calls += 1
        return {"tracks": {"items": [{"id": "s" * 22, "name": "Song One", "duration_ms": 123000,
                                      "artists": [{"name": "Artist A", "id": "art1"}],
                                      "album": {"name": "Album X", "release_date": "2020-01-01"}}]}}


class FakeResp:
    status_code = 200
    headers = {}

    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class FakeSession:
    def get(self, url, params=None, **kw):
        if "reccobeats" in url:
            ids = params["ids"].split(",")
            return FakeResp({"content": [
                {"id": "uuid-" + i, "href": f"https://open.spotify.com/track/{i}", "danceability": 0.5,
                 "energy": 0.7, "valence": 0.3, "tempo": 120.0, "acousticness": 0.1, "instrumentalness": 0.0,
                 "liveness": 0.2, "loudness": -6.0, "speechiness": 0.05} for i in ids]})
        return FakeResp({"toptags": {"tag": [{"name": "Bhangra", "count": 100}, {"name": "seen live", "count": 90},
                                             {"name": "rare", "count": 3}]}})


def test_full_enrichment_with_fakes_and_cache(export_zip, tmp_path):
    raw, _ = load_history(export_zip)
    plays, _ = clean_history(raw)
    library = load_library_tracks(export_zip)
    cache = enrich.Cache(tmp_path / "c.sqlite")
    sp = FakeSpotify()
    enrich.resolve_tracks(plays, cache, library=library, sp=sp, log=lambda *_: None)
    assert sp.calls == 1  # Song Two came from YourLibrary, only Song One needed a search
    enrich.fetch_audio_features(cache, session=FakeSession(), log=lambda *_: None)
    enrich.fetch_genres(plays, cache, "key", session=FakeSession(), log=lambda *_: None)
    master = enrich.apply_cache(plays, cache)
    one = master[master["track_key"] == make_track_key("Song One", "Artist A")]
    assert (one["spotify_track_id"] == "s" * 22).all() and one["has_audio_features"].all()
    assert (one["genres"] == "bhangra").all()
    assert (one["album_name"] == "Album X").all()
    # second pass makes no new calls
    enrich.resolve_tracks(plays, cache, library=library, sp=sp, log=lambda *_: None)
    assert sp.calls == 1
    cache.close()


def test_run_offline_writes_expected_schema(export_zip, tmp_path):
    master = run(export_zip, out_dir=tmp_path / "out", cache_dir=tmp_path / "cache", log=lambda *_: None)
    written = pd.read_parquet(tmp_path / "out" / "master_tracks.parquet")
    assert list(written.columns) == MASTER_COLUMNS
    assert len(written) == len(master) == 3
    assert (tmp_path / "out" / "ingest_report.json").exists()

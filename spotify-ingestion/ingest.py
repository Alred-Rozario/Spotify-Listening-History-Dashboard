#!/usr/bin/env python3
"""Command-line entry point for Member 1's ingestion step.

    python ingest.py                       # offline: uses data/raw/*.zip (or ./my_spotify_data.zip)
    python ingest.py --export path/to.zip  # explicit export
    python ingest.py --enrich              # + Spotify ids, ReccoBeats audio features, Last.fm genres
    python ingest.py --enrich --enrich-limit 20   # quick smoke test of the APIs

On Windows use `py` instead of `python3` if `python` isn't found.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Windows consoles default to cp1252 and crash on non-Latin track names; force UTF-8 output.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

from ingestion.pipeline import run  # noqa: E402


def find_export() -> Path | None:
    for folder in (Path("data/raw"), Path(".")):
        zips = sorted(folder.glob("*.zip"))
        if zips:
            return zips[0]
        dirs = [d for d in folder.glob("*") if d.is_dir() and (d / "StreamingHistory_music_0.json").exists()]
        if dirs:
            return dirs[0]
    return None


def main() -> int:
    p = argparse.ArgumentParser(description="Spotify export -> master_tracks.parquet")
    p.add_argument("--export", help="Path to the Spotify export .zip or unzipped folder")
    p.add_argument("--out", default="data/processed", help="Output folder (default: data/processed)")
    p.add_argument("--cache", default="data/cache", help="Enrichment cache folder (default: data/cache)")
    p.add_argument("--tz", default=None, help="Timezone for hour/day columns (default: LOCAL_TZ env or Asia/Kolkata)")
    p.add_argument("--enrich", action="store_true", help="Call Spotify/ReccoBeats/Last.fm (needs .env for Spotify)")
    p.add_argument("--enrich-limit", type=int, default=None, help="Only process N items per source (testing)")
    p.add_argument("--no-csv", action="store_true", help="Skip writing master_tracks.csv")
    args = p.parse_args()

    export = Path(args.export) if args.export else find_export()
    if not export or not export.exists():
        print("Could not find a Spotify export. Put the .zip in data/raw/ or pass --export PATH.")
        return 1
    run(export, out_dir=args.out, cache_dir=args.cache, tz=args.tz, enrich_data=args.enrich,
        enrich_limit=args.enrich_limit, write_csv=not args.no_csv)
    return 0


if __name__ == "__main__":
    sys.exit(main())

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from dataset.populate import DEFAULT_DATABASE, catalog_state, initialize_database


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify or snapshot the Songuess catalog.")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--target-total", type=int)
    parser.add_argument("--snapshot", type=Path, help="Write the current enabled identity set")
    parser.add_argument(
        "--preserve-snapshot", type=Path, help="Verify every identity in this snapshot remains"
    )
    return parser.parse_args(arguments)


def main() -> None:
    args = parse_args()
    initialize_database(args.database)
    if args.snapshot:
        snapshot_catalog(args.database, args.snapshot)
        print(f"Catalog identity snapshot written to {args.snapshot}")
        if args.target_total is None:
            return
    if args.target_total is None:
        raise SystemExit("--target-total is required unless only writing --snapshot")
    verify_catalog(
        args.database,
        target_total=args.target_total,
        preserve_snapshot=args.preserve_snapshot,
    )


def snapshot_catalog(database_path: Path, output_path: Path) -> None:
    state = catalog_state(database_path)
    payload = {
        "enabled_count": state.enabled_count,
        "musicbrainz_ids": sorted(state.musicbrainz_ids),
        "apple_track_ids": sorted(state.apple_track_ids),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output_path)


def verify_catalog(
    database_path: Path,
    *,
    target_total: int,
    preserve_snapshot: Path | None = None,
) -> dict[str, Any]:
    state = catalog_state(database_path)
    with sqlite3.connect(database_path) as connection:
        missing_previews = int(
            connection.execute(
                "SELECT COUNT(*) FROM songs WHERE enabled = 1 "
                "AND (preview_url IS NULL OR trim(preview_url) = '')"
            ).fetchone()[0]
        )
        years = [
            (int(year), int(count))
            for year, count in connection.execute(
                "SELECT release_year, COUNT(*) FROM songs WHERE enabled = 1 "
                "GROUP BY release_year ORDER BY release_year"
            )
        ]

    errors: list[str] = []
    if state.enabled_count != target_total:
        errors.append(f"enabled songs {state.enabled_count:,} != target {target_total:,}")
    if missing_previews:
        errors.append(f"{missing_previews:,} enabled songs have no preview URL")
    preserved_count = 0
    if preserve_snapshot:
        snapshot = json.loads(preserve_snapshot.read_text(encoding="utf-8"))
        expected_mbids = set(snapshot["musicbrainz_ids"])
        expected_apple_ids = {str(value) for value in snapshot["apple_track_ids"]}
        missing_mbids = expected_mbids - state.musicbrainz_ids
        missing_apple_ids = expected_apple_ids - state.apple_track_ids
        if missing_mbids or missing_apple_ids:
            errors.append(
                f"preservation failure: {len(missing_mbids):,} MusicBrainz and "
                f"{len(missing_apple_ids):,} Apple identities missing"
            )
        preserved_count = int(snapshot["enabled_count"])

    print("Catalog verification")
    print(f"  enabled unique songs: {state.enabled_count:,}/{target_total:,}")
    print(f"  distinct MusicBrainz IDs: {len(state.musicbrainz_ids):,}")
    print(f"  distinct Apple track IDs: {len(state.apple_track_ids):,}")
    print(f"  missing preview URLs: {missing_previews:,}")
    if preserve_snapshot:
        print(f"  preserved baseline songs: {preserved_count:,}")
    print("Year distribution (natural, unmodified)")
    for year, count in years:
        print(f"  {year}: {count:,}")
    if errors:
        raise SystemExit("Verification failed: " + "; ".join(errors))
    print("Verification passed.")
    return {
        "enabled_count": state.enabled_count,
        "musicbrainz_count": len(state.musicbrainz_ids),
        "apple_count": len(state.apple_track_ids),
        "missing_previews": missing_previews,
        "preserved_count": preserved_count,
        "years": dict(years),
    }


if __name__ == "__main__":
    main()

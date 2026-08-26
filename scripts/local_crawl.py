"""Run the full crawl against a tenant, bypassing Temporal + Dapr + the UI.

Use for local validation of transformer + client behaviour against a real
tenant. Prints elapsed time, entity counts and swallowed-error tallies, then
writes the NDJSON that scripts/inspect_dryrun.py can validate.

Credentials come from environment variables so nothing lands in shell history
or git-visible files:

    OMNI_BASE_URL=https://<your-org>.omniapp.co/api \\
    OMNI_API_TOKEN=<your token> \\
    uv run scripts/local_crawl.py

Optional:
    OMNI_CONNECTION_EPOCH_MS   13-digit ms epoch for the QN prefix
                               (default: current time truncated to ms)
    OMNI_MAX_PAGES             cap pages per listing (default: no cap)
    OMNI_AGGRESSIVE_FILTER     "0"/"false" to disable the content-backed-
                               workbook filter (default: on)
    OMNI_OUTPUT                path for the NDJSON dump
                               (default: omni_entities.ndjson)
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Ensure the app/ package resolves when running the script from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.client import ClientClass  # noqa: E402
from app.transformer import OmniMetadataTransformer  # noqa: E402


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _env_int_or_none(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw in (None, "", "null"):
        return None
    return int(raw)


def main() -> int:
    base_url = os.environ.get("OMNI_BASE_URL")
    token = os.environ.get("OMNI_API_TOKEN")
    if not base_url or not token:
        print(
            "error: OMNI_BASE_URL and OMNI_API_TOKEN must both be set.\n"
            "example:\n"
            "  OMNI_BASE_URL=https://acme.omniapp.co/api "
            "OMNI_API_TOKEN=omni_osk_... uv run scripts/local_crawl.py",
            file=sys.stderr,
        )
        return 2

    connection_epoch_ms = os.environ.get(
        "OMNI_CONNECTION_EPOCH_MS", str(int(time.time()) * 1000)
    )
    max_pages = _env_int_or_none("OMNI_MAX_PAGES")
    aggressive = _env_bool("OMNI_AGGRESSIVE_FILTER", True)
    output_path = Path(os.environ.get("OMNI_OUTPUT", "omni_entities.ndjson"))

    print(f"base_url = {base_url}")
    print(f"connection_epoch_ms = {connection_epoch_ms}")
    print(f"max_pages = {max_pages if max_pages else 'no cap'}")
    print(f"crawl_only_content_backed_workbooks = {aggressive}")
    print(f"output = {output_path}")
    print()

    client = ClientClass(
        credentials={"omni_base_url": base_url, "omni_api_token": token}
    )
    t0 = time.monotonic()
    snapshot = client.fetch_snapshot(
        page_size=100,
        max_pages=max_pages,
        max_concurrency=10,
        crawl_only_content_backed_workbooks=aggressive,
    )
    fetch_secs = time.monotonic() - t0

    transformer = OmniMetadataTransformer(connection_epoch_ms=connection_epoch_ms)
    t1 = time.monotonic()
    entities = transformer.transform(snapshot=snapshot)
    transform_secs = time.monotonic() - t1

    with output_path.open("w", encoding="utf-8") as handle:
        for entity in entities:
            handle.write(json.dumps(entity))
            handle.write("\n")

    by_type: dict[str, int] = {}
    for e in entities:
        by_type[e["typeName"]] = by_type.get(e["typeName"], 0) + 1

    print(f"fetch:     {fetch_secs:6.1f}s")
    print(f"transform: {transform_secs:6.1f}s")
    print()
    print("snapshot counts:")
    print(f"  connections:      {len(snapshot.get('connections', []))}")
    print(f"  models (raw):     {len(snapshot.get('models', []))}")
    print(f"  topics:           {len(snapshot.get('topics', []))}")
    print(f"  folders:          {len(snapshot.get('folders', []))}")
    print(f"  documents:        {len(snapshot.get('documents', []))}")
    print()
    print("emitted entities:")
    for type_name in sorted(by_type):
        print(f"  {type_name:20s} {by_type[type_name]}")
    print(f"  {'TOTAL':20s} {len(entities)}")
    print()
    print("fetch failures (silently caught, per blast radius):")
    for cat, n in client._failures.items():
        print(f"  {cat:20s} {n}")
    print()
    print(f"wrote {output_path} ({output_path.stat().st_size / (1024 * 1024):.2f} MB)")
    print()
    print("next: python3 scripts/inspect_dryrun.py " + str(output_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Import the curated Ashby index and exact public job records into a frozen dataset.

This is an offline import of the source-system evidence collected during research.
It deliberately does not re-fetch postings that may have changed or disappeared.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from urllib.parse import quote, unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmark.io import atomic_json as write_json, atomic_jsonl, digest, identifier


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", required=True, type=Path)
    parser.add_argument("--feeds", required=True, type=Path)
    parser.add_argument("--pagechecks", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Refusing to overwrite a frozen dataset; choose a new output directory.")
    index = json.loads(args.index.read_text())
    pages = json.loads(args.pagechecks.read_text())
    imported_at = datetime.now(timezone.utc).isoformat()
    roles, snapshots, ids = [], [], set()
    for category in index["categories"]:
        for entry in category["entries"]:
            parsed = urlsplit(entry["job_url"])
            if parsed.scheme != "https" or parsed.hostname != "jobs.ashbyhq.com":
                raise ValueError("Non-Ashby source URL")
            board, posting_id = unquote(parsed.path).strip("/").split("/")
            posting_id = identifier(posting_id.lower())
            slug = unquote(urlsplit(entry["board_url"]).path.strip("/").split("/")[0])
            feed_path = args.feeds / (quote(slug, safe="") + ".json")
            feed = json.loads(feed_path.read_text())
            matches = [job for job in feed["jobs"] if job["id"].lower() == posting_id.lower()]
            if len(matches) != 1 or not matches[0].get("isListed"):
                raise ValueError(f"Missing or unlisted source record: {posting_id}")
            job = matches[0]
            if job["title"].strip() != entry["job_title"].strip():
                raise ValueError(f"Title changed: {posting_id}")
            page = pages[entry["job_url"]]
            if page["http_status"] != "200" or page["expired_deadlines"] or not page["deadline_field_found"]:
                raise ValueError(f"Unverified posting page: {posting_id}")
            if posting_id in ids:
                raise ValueError("Duplicate posting")
            ids.add(posting_id)
            snapshot = {
                "schema_version": "1.0", "role_id": posting_id,
                "source": {"provider": "ashby", "job_url": entry["job_url"], "board_url": entry["board_url"],
                           "feed_url": entry["source_url"], "verified_at": index["checked_utc"],
                           "imported_at": imported_at, "page_check": page},
                "job": job,
            }
            snapshot_hash = digest(snapshot)
            snapshots.append((posting_id, snapshot))
            roles.append({
                "schema_version": "1.0", "role_id": posting_id, "category": category["category"],
                "company": entry["company"], "title": job["title"].strip(), "job_url": entry["job_url"],
                "location": job.get("location"), "secondary_locations": job.get("secondaryLocations", []),
                "workplace_type": job.get("workplaceType"), "employment_type": job.get("employmentType"),
                "department": job.get("department"), "team": job.get("team"),
                "published_at": job.get("publishedAt"), "fit_note": entry.get("fit_note", ""),
                "category_note": category.get("notes", ""), "snapshot_path": f"snapshots/{posting_id}.json",
                "snapshot_sha256": snapshot_hash, "rubric_path": f"rubrics/{posting_id}.json",
            })
    if len(roles) != 290 or len(index["categories"]) != 58:
        raise ValueError("Expected the complete 58-category, 290-role selection")
    for posting_id, snapshot in snapshots:
        write_json(args.output / "snapshots" / f"{posting_id}.json", snapshot)
    atomic_jsonl(args.output / "roles.jsonl", roles)
    manifest = {
        "schema_version": "1.0", "dataset_id": args.output.name, "as_of": index["checked_utc"][:10],
        "role_count": len(roles), "category_count": len(index["categories"]),
        "categories": [{"name": category["category"], "count": len(category["entries"])} for category in index["categories"]],
        "selection_sha256": digest(roles), "source_index_sha256": digest(index),
        "reference": {"repository": "https://github.com/LessieAI/people-search-bench",
                      "commit": "f1afbb928e7eb6104e8255d5a882fc1820682cb7", "relationship": "Directory structure and methodology reference; no upstream source code copied."},
        "source_notice": "Job descriptions are attributed employer material. This repository does not relicense them.",
    }
    write_json(args.output / "manifest.json", manifest)
    print(json.dumps({"dataset": str(args.output), "roles": len(roles), "categories": len(index["categories"])}))


if __name__ == "__main__":
    main()

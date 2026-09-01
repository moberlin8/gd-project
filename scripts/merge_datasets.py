#!/usr/bin/env python3
"""
Merge multiple GD comment JSON files into one combined dataset.
Also deduplicates comments by (show_identifier, comment_text) pairs.
"""

import json
import sys
import os
from collections import OrderedDict

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
OUTPUT_DIR = DATA_DIR


def merge_files(paths: list[str]) -> dict:
    """Merge multiple comment JSON files into one."""
    merged = {
        "metadata": [],
        "comments": OrderedDict(),  # dedup by (show_id, comment_text)
        "setlists": {},
    }

    total_comments = 0
    total_shows = set()

    for path in paths:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        print(f"\nLoading: {path}")
        print(f"  Shows: {data.get('shows_with_comments', '?')}")
        print(f"  Comments: {data.get('comments_total', '?')}")

        # Merge comments (dedup by show_id + comment_text)
        for c in data.get("comments", []):
            key = (c.get("show_identifier", ""), c.get("comment_text", ""))
            if key not in merged["comments"]:
                merged["comments"][key] = c
                total_comments += 1

        # Merge setlists
        for show_id, songs in data.get("setlists", {}).items():
            if show_id not in merged["setlists"]:
                setlist = os.path.basename(path)
                merged["setlists"][show_id] = songs

        # Collect all show IDs
        for sid in data.get("shows_processed", []):
            total_shows.add(sid)

    # Convert comments OrderedDict to list
    comments_list = list(merged["comments"].values())

    # Build comprehensive metadata
    metadata = {
        "experiment": "GD Comment RAT — Complete Dataset",
        "collection": "GratefulDead",
        "total_files_merged": len(paths),
        "shows_total": len(total_shows),
        "shows_with_setlists": len(merged["setlists"]),
        "comments_total": len(comments_list),
        "comments_deduplicated": total_comments - len(comments_list),
        "merged_timestamp": __import__("datetime").datetime.now().isoformat(),
        "source_files": [os.path.basename(p) for p in paths],
    }

    return {
        "metadata": metadata,
        "shows_processed": sorted(list(total_shows)),
        "setlists": merged["setlists"],
        "comments": comments_list,
    }


def main():
    data_dir = os.path.join(os.path.dirname(__file__), "..", "data")
    files = sorted([
        os.path.join(data_dir, f)
        for f in os.listdir(data_dir)
        if f.startswith("gd_comments_") and f.endswith(".json")
    ])

    # Only use final output files (not progress files or intermediate runs)
    final_files = [
        os.path.join(data_dir, f)
        for f in os.listdir(data_dir)
        if f.startswith("gd_comments_") and f.endswith(".json") and "progress" not in f
    ]
    # Filter to the two main runs only
    final_files = [
        os.path.join(data_dir, "gd_comments_20260830-233931.json"),
        os.path.join(data_dir, "gd_comments_20260831-005050.json"),
    ]
    # Filter out any that don't exist
    final_files = [f for f in final_files if os.path.exists(f)]
    print(f"Found {len(final_files)} final data files:")
    for f in final_files:
        print(f"  {os.path.basename(f)}")

    merged = merge_files(final_files)

    output_path = os.path.join(data_dir, "gd_comments_combined.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)

    size_kb = os.path.getsize(output_path) / 1024
    print(f"\n=== Merge Complete ===")
    print(f"Output: {output_path} ({size_kb:.1f} KB)")
    print(f"Shows: {merged['metadata']['shows_total']}")
    print(f"Comments: {merged['metadata']['comments_total']}")
    print(f"Duplicates removed: {merged['metadata']['comments_deduplicated']}")
    print(f"Setlists: {merged['metadata']['shows_with_setlists']}")


if __name__ == "__main__":
    main()

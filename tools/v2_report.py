#!/usr/bin/env python3
"""Print a compact shadow-mode report from the V2 SQLite ledger."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/verification.sqlite3")
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args()
    path = Path(args.db)
    if not path.exists():
        print(f"No V2 database found at {path}")
        return 1
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT decision_json FROM decisions ORDER BY decision_id DESC LIMIT ?",
        (args.limit,),
    ).fetchall()
    decisions = [json.loads(row["decision_json"]) for row in rows]
    print(f"V2 shadow decisions: {len(decisions)}")
    print("Decisions:", dict(Counter(d["decision"] for d in decisions)))
    print("Events:", dict(Counter(d["event_type"] for d in decisions)))
    failed = Counter()
    for decision in decisions:
        for gate in decision.get("gates", []):
            if gate.get("state") != "PASS":
                failed[f"{gate['name']}:{gate['state']}"] += 1
    print("Most common non-pass gates:")
    for name, count in failed.most_common(15):
        print(f"  {count:4d}  {name}")
    publications = connection.execute(
        "SELECT COUNT(*) FROM publications"
    ).fetchone()[0]
    print(f"Recorded V2 publications: {publications}")
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

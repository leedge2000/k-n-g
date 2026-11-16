"""V-04 外部 URI 可解析性验收：按固定种子抽样，逐条请求并留证。

用法：
    python scripts/verify_v04_uris.py --vocab AGROVOC
输出：
    ontology_ly/tools/v04_report.json
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "grain_oil.db"
OUT = ROOT / "ontology_ly" / "tools" / "v04_report.json"

HEADERS = {
    "User-Agent": "GrainOilKG-Validator/1.0 (read-only acceptance; contact: local-project)",
    "Accept": "text/html,application/xhtml+xml,application/rdf+xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "identity",
}


def load_snapshot(vocab: str) -> tuple[str, list[dict]]:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    snap = con.execute(
        """
        SELECT sn.snapshotId, sn.conceptCount
        FROM OntologySnapshot sn
        JOIN OntologyScheme s ON s.schemeId = sn.schemeId
        WHERE s.vocab = ? AND sn.status = '已校验'
        ORDER BY sn.conceptCount DESC
        LIMIT 1
        """,
        (vocab,),
    ).fetchone()
    if snap is None:
        raise SystemExit(f"未找到 {vocab} 的已校验快照")
    rows = con.execute(
        "SELECT externalId, externalUri FROM OntologyConcept WHERE snapshotId = ? ORDER BY externalId",
        (snap["snapshotId"],),
    ).fetchall()
    con.close()
    return snap["snapshotId"], [dict(r) for r in rows]


def fetch(uri: str, retries: int = 4) -> dict:
    ctx = ssl.create_default_context()
    ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
    last = {"httpStatus": None, "error": None, "finalUrl": None, "body": ""}
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(uri, headers=HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
                body = resp.read(200_000).decode("utf-8", "ignore")
                return {
                    "httpStatus": resp.status,
                    "finalUrl": resp.geturl(),
                    "body": body,
                    "error": None,
                    "attempts": attempt,
                }
        except urllib.error.HTTPError as exc:
            last = {
                "httpStatus": exc.code,
                "finalUrl": None,
                "body": "",
                "error": f"HTTP {exc.code}",
                "attempts": attempt,
            }
            if exc.code == 429:
                wait = int(exc.headers.get("Retry-After") or 0) or 10 * attempt
                time.sleep(wait)
                continue
            if 500 <= exc.code < 600:
                time.sleep(3 * attempt)
                continue
            return last
        except Exception as exc:  # noqa: BLE001
            last = {
                "httpStatus": None,
                "finalUrl": None,
                "body": "",
                "error": f"{type(exc).__name__}: {str(exc)[:100]}",
                "attempts": attempt,
            }
            time.sleep(2 * attempt)
    return last


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--vocab", default="AGROVOC")
    parser.add_argument("--seed", type=int, default=20261116)
    parser.add_argument("--pause", type=float, default=0.2)
    args = parser.parse_args()

    snapshot_id, concepts = load_snapshot(args.vocab)
    total = len(concepts)
    sample_size = min(total, max(20, math.ceil(total * 0.05)))
    rnd = random.Random(args.seed)
    picks = rnd.sample(concepts, sample_size)

    started = datetime.now(timezone.utc)
    results = []
    ok = 0
    for idx, item in enumerate(picks, 1):
        ext_id = item["externalId"]
        uri = item["externalUri"]
        got = fetch(uri)
        token = ext_id.split("/")[-1]
        haystack = f"{got.get('finalUrl') or ''}\n{got.get('body') or ''}"
        matched = got.get("httpStatus") == 200 and token in haystack
        if matched:
            ok += 1
        results.append(
            {
                "externalId": ext_id,
                "uri": uri,
                "httpStatus": got.get("httpStatus"),
                "finalUrl": got.get("finalUrl"),
                "matched": matched,
                "attempts": got.get("attempts"),
                "error": got.get("error"),
            }
        )
        if idx % 100 == 0:
            print(f"progress {idx}/{sample_size} ok={ok}", flush=True)
        time.sleep(args.pause)

    finished = datetime.now(timezone.utc)
    reasons: dict[str, int] = {}
    for row in results:
        if not row["matched"]:
            key = row["error"] or f"HTTP {row['httpStatus']} 但未确认同一概念"
            reasons[key] = reasons.get(key, 0) + 1

    report = {
        "checkId": "V-04",
        "vocab": args.vocab,
        "snapshotId": snapshot_id,
        "seed": args.seed,
        "sampleRule": "max(20, ceil(conceptCount * 5%))",
        "conceptCount": total,
        "sampleSize": sample_size,
        "passed": ok == sample_size,
        "matchedCount": ok,
        "failedCount": sample_size - ok,
        "failureReasons": reasons,
        "startedAt": started.isoformat(timespec="seconds"),
        "finishedAt": finished.isoformat(timespec="seconds"),
        "durationSeconds": round((finished - started).total_seconds(), 1),
        "samples": results,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(
        json.dumps(
            {
                "vocab": args.vocab,
                "sampleSize": sample_size,
                "matched": ok,
                "failed": sample_size - ok,
                "failureReasons": reasons,
                "passed": report["passed"],
                "report": str(OUT),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

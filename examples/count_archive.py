#!/usr/bin/env python3
"""Count a scanned archive end to end: nas_scan.py output -> the three bi-public/v1 documents.

    python3 conta/nas_scan.py --root /path/to/archive --out /tmp/scan
    python3 examples/count_archive.py /tmp/scan > totals.json

Every top-level folder of the archive is treated as one client, and every project the register keeps as the client's
own work is counted (received material is left out). CJ's own export adds what this example leaves out on purpose:
which folders are clients, partners or the agency itself, and who may be named. Those are CJ's rulings, not method.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "conta"))
import count  # noqa: E402


def main(out):
    tally, as_of = count.Tally(), ""
    for f in sorted(Path(out, "clients").glob("*/*.nas-scan.json")):
        scan = json.loads(f.read_text())
        as_of = max(as_of, (scan.get("scanned") or "")[:10])
        for p in scan.get("projects") or []:
            if not p.get("received"):
                tally.add(count.named_year_floor(p))
    meta = {"as_of": as_of, "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
            "provisional": False, "note": "example run"}
    summary, now_30d, starts = tally.documents(as_of, meta, active_clients=0, counts_include={"visible"})
    print(json.dumps({"summary": summary, "now_30d": now_30d, "starts_by_month": starts}, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])

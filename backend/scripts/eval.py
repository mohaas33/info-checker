"""Measure agreement with human fact-checkers.

Reads a CSV with columns `text,expected_verdict` (optional extra columns such as `source`
are shown in the table), sends each text to a running backend's /check, and prints
expected vs actual verdicts plus agreement scores.

Usage (from backend/):
    python scripts/eval.py eval/claims.csv [--url http://localhost:8000] [--out results.csv]

Each row should be a single claim. If the backend splits a row into several claims, the
first checkable (factual/quote) claim is scored, and the "n" column shows the count.

Scores:
- exact: actual verdict == expected verdict.
- direction: same broad class. true = supported; false = contradicted, misleading,
  outdated; unknown = unverifiable, not_checkable. Useful because fact-checkers' ratings
  ("Half True", "Mostly False") don't map one-to-one onto our verdicts.

Uncached rows cost LLM tokens and Tavily searches. Re-runs within 24h hit the cache.
"""

import argparse
import csv
import sys
import time
from collections import Counter

import httpx

VERDICTS = {"supported", "contradicted", "misleading", "unverifiable", "outdated", "not_checkable"}
DIRECTION = {
    "supported": "true",
    "contradicted": "false", "misleading": "false", "outdated": "false",
    "unverifiable": "unknown", "not_checkable": "unknown",
}


def pick_claim(claims: list[dict]) -> dict | None:
    checkable = [c for c in claims if c["type"] in ("factual", "quote")]
    return (checkable or claims or [None])[0]


def read_rows(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if rows and not {"text", "expected_verdict"} <= rows[0].keys():
        sys.exit(f"{path}: needs columns text,expected_verdict (found: {', '.join(rows[0])})")
    bad = [(i, r["expected_verdict"]) for i, r in enumerate(rows, 2)
           if r["expected_verdict"].strip().lower() not in VERDICTS]
    if bad:
        sys.exit("unknown expected_verdict on line(s): "
                 + ", ".join(f"{i} ({v!r})" for i, v in bad)
                 + f"\nallowed: {', '.join(sorted(VERDICTS))}")
    return [r for r in rows if r["text"].strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("csv_path")
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--out", help="also write per-row results to this CSV")
    args = parser.parse_args()

    rows = read_rows(args.csv_path)
    results = []
    with httpx.Client(base_url=args.url, timeout=95) as client:
        try:
            client.get("/health").raise_for_status()
        except httpx.HTTPError as e:
            print(f"Backend not reachable at {args.url}: {e}")
            return 1

        for i, row in enumerate(rows, 1):
            expected = row["expected_verdict"].strip().lower()
            t0 = time.monotonic()
            actual, confidence, n, note = "error", "", 0, ""
            try:
                r = client.post("/check", json={"text": row["text"], "url": None})
                if r.status_code == 200:
                    claims = r.json()["claims"]
                    n = len(claims)
                    c = pick_claim(claims)
                    if c:
                        actual, confidence = c["verdict"], c["confidence"]
                    else:
                        actual, note = "no_claims", "no claims extracted"
                else:
                    note = f"HTTP {r.status_code}"
            except httpx.HTTPError as e:
                note = type(e).__name__
            elapsed = time.monotonic() - t0
            results.append({**row, "expected": expected, "actual": actual,
                            "confidence": confidence, "n": n, "seconds": f"{elapsed:.1f}",
                            "note": note})
            mark = "✓" if actual == expected else ("~" if DIRECTION.get(actual) == DIRECTION[expected] else "✗")
            print(f"[{i}/{len(rows)}] {mark} {expected} -> {actual} ({elapsed:.1f}s)", file=sys.stderr)

    print_table(results)
    print_scores(results)
    if args.out:
        with open(args.out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(results[0]))
            w.writeheader()
            w.writerows(results)
        print(f"\nwrote {args.out}")
    return 0


def print_table(results: list[dict]) -> None:
    extra = [k for k in results[0] if k not in
             {"text", "expected_verdict", "expected", "actual", "confidence", "n", "seconds", "note"}]
    headers = ["#", "", "expected", "actual", "conf", "n", *extra, "text"]
    lines = []
    for i, r in enumerate(results, 1):
        match = "✓" if r["actual"] == r["expected"] else (
            "~" if DIRECTION.get(r["actual"]) == DIRECTION[r["expected"]] else "✗")
        text = " ".join(r["text"].split())
        text = text[:70] + "…" if len(text) > 70 else text
        if r["note"]:
            text = f"[{r['note']}] {text}"
        lines.append([str(i), match, r["expected"], r["actual"], r["confidence"], str(r["n"]),
                      *(str(r[k])[:20] for k in extra), text])
    widths = [max(len(h), *(len(line[j]) for line in lines)) for j, h in enumerate(headers)]
    print()
    print("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("  ".join("-" * w for w in widths))
    for line in lines:
        print("  ".join(c.ljust(w) for c, w in zip(line, widths)))


def print_scores(results: list[dict]) -> None:
    total = len(results)
    scored = [r for r in results if r["actual"] in VERDICTS]
    exact = sum(r["actual"] == r["expected"] for r in scored)
    direction = sum(DIRECTION[r["actual"]] == DIRECTION[r["expected"]] for r in scored)
    errors = total - len(scored)
    print()
    print(f"exact agreement:     {exact}/{total} = {exact / total:.0%}")
    print(f"direction agreement: {direction}/{total} = {direction / total:.0%}"
          "   (true / false-ish / unknown)")
    if errors:
        print(f"errors / no claims:  {errors} (counted as disagreement)")

    print("\nconfusion (rows = expected, columns = actual):")
    pairs = Counter((r["expected"], r["actual"]) for r in results)
    exp_labels = sorted({r["expected"] for r in results})
    act_labels = sorted({r["actual"] for r in results})
    w = max(map(len, exp_labels + act_labels)) + 1
    print(" " * w + "".join(a[:12].rjust(13) for a in act_labels))
    for e in exp_labels:
        print(e.ljust(w) + "".join(str(pairs[(e, a)] or ".").rjust(13) for a in act_labels))


if __name__ == "__main__":
    sys.exit(main())

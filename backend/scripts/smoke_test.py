"""Send 4 sample texts to a running backend and print the full JSON results.

Usage (from backend/):  python scripts/smoke_test.py [--url http://localhost:8000]

These are real calls: each uncached sample costs LLM tokens and a few Tavily searches.
Repeat runs within 24h are served from the cache.
"""

import argparse
import json
import sys
import time

import httpx

SAMPLES = [
    ("clearly false", "The Great Wall of China is the only man-made structure visible from "
                      "the Moon with the naked eye."),
    ("true", "In May 2023, the World Health Organization declared that COVID-19 no longer "
             "represents a global health emergency."),
    ("opinion", "Pineapple is the best pizza topping, and anyone who disagrees has terrible taste."),
    # Time-sensitive: the answer depends on recent, dated news, not on the model's memory.
    ("time-sensitive", "Keir Starmer is currently the Prime Minister of the United Kingdom."),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000")
    args = parser.parse_args()

    with httpx.Client(base_url=args.url, timeout=95) as client:
        try:
            print("health:", client.get("/health").json())
        except httpx.HTTPError as e:
            print(f"Backend not reachable at {args.url}: {e}")
            return 1

        for label, text in SAMPLES:
            print(f"\n=== {label} ===\n{text}\n")
            t0 = time.monotonic()
            r = client.post("/check", json={"text": text, "url": None})
            print(f"HTTP {r.status_code} in {time.monotonic() - t0:.1f}s")
            try:
                print(json.dumps(r.json(), indent=2, ensure_ascii=False))
            except ValueError:
                print(r.text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

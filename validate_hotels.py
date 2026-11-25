#!/usr/bin/env python3
"""Validate TravelPets hotel listings from an Excel extract.

Runs free checks first (the hotel's website, then Overture Maps open data) and only sends hotels
those can't confirm to the paid Google Places API, after showing how many that is.

    python validate_hotels.py hotels.xlsx --free-only        # free passes only, see what's left
    python validate_hotels.py hotels.xlsx                    # free passes, then Google (Pro tier) for the rest
    python validate_hotels.py hotels.xlsx --max-google 200   # Google for at most 200 hotels
    python validate_hotels.py hotels.xlsx --limit 20         # trial run on the first 20 rows

Writes <input>_validated.xlsx next to the input unless -o is given.
"""

import argparse
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests

from hotel_validator.columns import clean, clean_zip, map_columns, merge_duplicate_ids
from hotel_validator.overture import OvertureIndex
from hotel_validator.places import PlacesAuthError, PlacesClient
from hotel_validator.report import write_report
from hotel_validator.url_check import UrlResult
from hotel_validator.validate import (HotelRow, Result, _query, finish, free_passes, google_pass,
                                      unresolved)

# Google list prices for Text Search (New), checked Nov 2026:
# https://developers.google.com/maps/billing-and-pricing/pricing
FREE_PER_MONTH = {"pro": 5000, "enterprise": 1000}
PRICE_PER_1000 = {"pro": 32, "enterprise": 35}
FIELDS = ("id", "name", "address", "city", "state", "zip", "phone", "url")


def load_env(path: Path) -> None:
    """Minimal .env loader so the API key can live in a file instead of the shell."""
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def run_parallel(fn, items, workers, label, on_error):
    """Run fn(item) over items in a thread pool, printing progress. Returns {item: result}.

    An unexpected error on one row becomes on_error(exc) for that row instead of ending the run;
    only a rejected Google key stops everything.
    """
    out, start = {}, time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fn, it): it for it in items}
        for n, fut in enumerate(as_completed(futures), 1):
            try:
                out[futures[fut]] = fut.result()
            except PlacesAuthError:
                pool.shutdown(wait=False, cancel_futures=True)
                raise
            except Exception as e:
                out[futures[fut]] = on_error(e)
            if n % 25 == 0 or n == len(items):
                print(f"  {label}: {n}/{len(items)} rows ({time.time() - start:.0f}s)", flush=True)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="Excel extract (.xlsx/.xls) or .csv")
    ap.add_argument("-o", "--output", help="output .xlsx (default: <input>_validated.xlsx)")
    ap.add_argument("--sheet", default=0, help="sheet name or index (default: first sheet)")
    ap.add_argument("--limit", type=int, help="only process the first N rows (for a trial run)")
    ap.add_argument("--free-only", action="store_true", help="skip the paid Google pass")
    ap.add_argument("--max-google", type=int, help="send at most N hotels to Google this run")
    ap.add_argument("--google-tier", choices=["pro", "enterprise"], default="pro",
                    help="pro (default): 5,000 free requests/month, no phone/website from Google. "
                         "enterprise: adds Google's phone + website, only 1,000 free/month")
    ap.add_argument("--yes", "-y", action="store_true", help="don't ask before using Google")
    ap.add_argument("--no-overture", action="store_true", help="skip the Overture open-data pass")
    ap.add_argument("--no-merge", action="store_true", help="don't combine rows that share a hotel ID")
    ap.add_argument("--refresh-overture", action="store_true", help="download the latest Overture release")
    ap.add_argument("--workers", type=int, default=8, help="parallel rows (default 8)")
    for f in FIELDS:
        ap.add_argument(f"--col-{f}", help=f"column holding the hotel {f} if not auto-detected")
    args = ap.parse_args(argv)

    here = Path(__file__).resolve().parent
    cache = here / ".cache"
    cache.mkdir(exist_ok=True)
    load_env(here / ".env")

    src = Path(args.input)
    sheet = int(args.sheet) if str(args.sheet).isdigit() else args.sheet
    df = pd.read_csv(src, dtype=str) if src.suffix.lower() == ".csv" else pd.read_excel(src, sheet_name=sheet, dtype=str)
    df = df.dropna(how="all").reset_index(drop=True)
    cols = map_columns(df, {f: getattr(args, f"col_{f}") for f in FIELDS})
    if "id" in cols and not args.no_merge:
        df, merged = merge_duplicate_ids(df, cols["id"])
        if merged:
            print(f"Merged {merged} duplicate rows that share a {cols['id']} (one row per hotel; --no-merge to keep them)")
    if args.limit:
        df = df.head(args.limit)
    print(f"Read {len(df)} rows from {src.name}. Columns: " + ", ".join(f"{k}='{v}'" for k, v in cols.items()))

    client = None
    if not args.free_only:
        key = os.environ.get("GOOGLE_MAPS_API_KEY")
        if key:
            client = PlacesClient(key, cache_path=str(cache / "places.sqlite"), tier=args.google_tier)
        else:
            print("No GOOGLE_MAPS_API_KEY set - running free passes only.")

    overture = None
    if not args.no_overture:
        overture = OvertureIndex.load(cache, refresh=args.refresh_overture)
        print(f"Overture release {overture.release}: {overture.count:,} US lodging places loaded")

    def row_of(rec) -> HotelRow:
        g = lambda f: clean(rec.get(cols[f])) if f in cols else ""
        return HotelRow(name=g("name"), address=g("address"), city=g("city"), state=g("state"),
                        zip=clean_zip(rec.get(cols["zip"])) if "zip" in cols else "", phone=g("phone"), url=g("url"))

    rows = [row_of(r) for r in df.to_dict("records")]
    results: list[Result | None] = [None] * len(rows)
    url_checks = [None] * len(rows)
    hints = [None] * len(rows)     # what the free passes learned about hotels they couldn't confirm
    local = threading.local()

    def session():
        if not hasattr(local, "session"):
            local.session = requests.Session()
        return local.session

    for i, row in enumerate(rows):
        if not row.name or not row.address:
            results[i] = Result(hotel_status="ERROR", match_notes="Missing hotel name or address", action="Fix source data")
    todo = [i for i in range(len(rows)) if results[i] is None]

    # Passes 1 + 2: free.
    print("\nFree passes (hotel website, Overture open data, Overture's website for the hotel)...")
    free_error = lambda e: (None, UrlResult("ERROR", notes=f"Check failed: {e.__class__.__name__}: {e}"[:300]), None)
    for i, (res, u, h) in run_parallel(lambda i: free_passes(rows[i], session(), overture), todo, args.workers,
                                    "free", free_error).items():
        results[i], url_checks[i], hints[i] = res, u, h
    remaining = [i for i in todo if results[i] is None]
    by = pd.Series([results[i].verified_by for i in todo if results[i]]).value_counts()
    print(f"Confirmed for free: website {by.get('website', 0)}, overture {by.get('overture', 0)}, "
          f"Overture's website {by.get('overture_website', 0)}. "
          f"Still unconfirmed: {len(remaining)}")
    flagged = sum(bool(hints[i] and hints[i].overture_hint) for i in remaining)
    if flagged:
        print(f"  {flagged} of those have an overture_hint (possible rebrand or closure) in the output.")

    # Pass 3: Google, for what's left.
    if remaining and client:
        send = remaining[:args.max_google] if args.max_google is not None else remaining
        uncached = sum(not client.is_cached(_query(rows[i])) for i in send)
        free, rate = FREE_PER_MONTH[args.google_tier], PRICE_PER_1000[args.google_tier]
        lo, hi = (max(0, n - free) * rate / 1000 for n in (uncached, 2 * uncached))
        print(f"\nGoogle pass ({args.google_tier} tier): {len(send)} hotels ({uncached} not cached), "
              f"about {uncached}-{2 * uncached} requests.\n"
              f"  {free:,} requests/month are free, then ${rate}/1,000 -> estimated ${lo:.2f}-${hi:.2f} "
              f"(more if other Maps usage on this billing account this month).")
        go = args.yes or uncached == 0
        if not go:
            if sys.stdin.isatty():
                go = input("Proceed with Google lookups? [y/N] ").strip().lower() in ("y", "yes")
            else:
                print("Not interactive - pass --yes to use Google.")
        if go:
            try:
                google_error = lambda e: Result(hotel_status="ERROR", match_notes=f"{e.__class__.__name__}: {e}"[:300])
                for i, res in run_parallel(lambda i: google_pass(rows[i], client), send, args.workers,
                                           "google", google_error).items():
                    results[i] = res
            except PlacesAuthError as e:
                print(f"\nERROR: {e}\nSee README 'Google API key setup'.", file=sys.stderr)
                return 2

    for i in remaining:
        if results[i] is None:
            reason = ("Google lookup not enabled" if not client else
                      "Google not run" if args.max_google is None else "over --max-google limit for this run")
            results[i] = unresolved(reason)
    for i in todo:
        results[i] = finish(rows[i], results[i], url_checks[i], session(), hints[i])

    out = pd.concat([df, pd.DataFrame([r.as_dict() for r in results])], axis=1)
    out_path = Path(args.output) if args.output else src.with_name(f"{src.stem}_validated.xlsx")
    write_report(out, str(out_path), client.calls if client else 0)

    print(f"\nWrote {out_path}")
    print(out.groupby(["hotel_status", "verified_by"], dropna=False).size().to_string())
    print(out["url_status"].value_counts().to_string())
    if client:
        print(f"Google Places API calls this run: {client.calls} (cached rows are free on re-run)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

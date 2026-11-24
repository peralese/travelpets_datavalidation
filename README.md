# TravelPets hotel validator

Checks an Excel extract of TravelPets hotel listings and reports, for each hotel, whether it is
**still at the listed address**, and whether its **website still works and belongs to that hotel**.

To keep paid API use low, every hotel goes through up to three passes and stops at the first one that
confirms it:

| Pass | Cost | Confirms the hotel when... |
|---|---|---|
| 1. Hotel website | free | the page loads, names the hotel, and shows its street address or phone number |
| 2. [Overture Maps](https://overturemaps.org) open data | free | an open lodging record has the same name and address, and no *other* hotel is listed there |
| 3. Google Places API | paid | everything else. Also the only pass that can report closed, moved or rebranded |

**The free passes only confirm hotels.** They never mark a hotel as closed or changed, because stale
free data shouldn't be the reason a listing is removed. Anything doubtful goes on to Google, or is
left `UNRESOLVED` for manual review if you skip Google. Before the Google pass, the tool shows how many
hotels are left and asks before spending anything.

The output is a copy of your spreadsheet with result columns added, colour-coded, plus a **Summary** sheet.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # then paste your Google API key into .env
```

### Google API key setup
1. Go to https://console.cloud.google.com/, create a project and **enable billing** on it.
2. Under *APIs & Services → Library*, enable **Places API (New)**.
3. Under *APIs & Services → Credentials*, create an API key. Restrict it to *Places API (New)*.
4. Recommended: under *Quotas*, set a daily cap so a mistake can't run up a bill.

**Cost:** each hotel that reaches the Google pass uses 1 Text Search request, or 2 when it isn't
found by name (the second request checks what is at that address now). Because the tool asks for
website and phone, Google bills these at its Text Search **Enterprise** rate, roughly $35 per 1,000
requests, with a monthly free allowance. Check
https://developers.google.com/maps/billing-and-pricing/pricing for current numbers. Google responses
are cached in `.cache/places.sqlite`, so re-running the same rows costs nothing.

**Overture data:** the first run downloads every US lodging place from the latest Overture release
(about 300k places, 30 MB, 1–2 minutes) into `.cache/`. Later runs reuse it. Overture publishes
monthly; use `--refresh-overture` to get the newest release.

## Usage

```bash
# 1. free passes only: see how much is confirmed and how much would go to Google
.venv/bin/python validate_hotels.py data/hotels.xlsx --free-only

# 2. full run: free passes, then Google for the rest (asks before spending)
.venv/bin/python validate_hotels.py data/hotels.xlsx

# cap the paid pass, e.g. 200 hotels per run (re-run later to do the next batch)
.venv/bin/python validate_hotels.py data/hotels.xlsx --max-google 200

# trial on the first 20 rows; --yes skips the prompt (for scheduled runs)
.venv/bin/python validate_hotels.py data/hotels.xlsx --limit 20 --yes
```

Without a `GOOGLE_MAPS_API_KEY` the tool runs the free passes only. `--no-overture` skips pass 2.

Columns are found automatically by header name (for example *Hotel Name / Name*, *Address / Street*,
*City*, *State*, *Zip / Zip Code / Postal Code*, *Phone*, *URL / Website*). If a header isn't
recognised, name it explicitly, for example `--col-name "Property"`. City is optional. Leading zeros
that Excel drops from zip codes (`2116` → `02116`) are restored. All original columns, such as a
database ID, are kept in the output, so results can be matched back to the database.

## Reading the results

| `hotel_status` | Meaning |
|---|---|
| `VERIFIED` | Google lists this hotel at this address and it's operating. `confidence` = HIGH/MEDIUM |
| `NAME_CHANGED` | A *different* lodging is now at this address (rebrand or sale). Pet policy may have changed |
| `ADDRESS_MISMATCH` | The hotel exists, but Google has a different address (moved, or the data is wrong) |
| `CLOSED_TEMPORARILY` / `CLOSED_PERMANENTLY` | As marked by Google |
| `NOT_FOUND` | Nothing matching was found. Usually closed; review before removing |
| `UNRESOLVED` | Free passes couldn't confirm it and Google wasn't used (or `--max-google` was reached) |
| `ERROR` | Lookup failed or the row is missing its name/address. Re-run |

| `url_status` | Meaning |
|---|---|
| `OK` | Page loads and mentions the hotel |
| `MISSING` | No URL on file. `suggested_url` holds Google's website if it has one |
| `REDIRECTED` | Works, but ends up on a different domain. Consider updating to `url_final` |
| `CONTENT_MISMATCH` | Page loads but doesn't mention the hotel |
| `PARKED` | Domain expired or for sale |
| `BROKEN` / `UNREACHABLE` / `INVALID` | 404 or 5xx error / DNS failure or timeout / malformed URL |
| `BLOCKED` | The site refused automated access (common on big brand sites). Check by hand |

Other useful columns:
- `verified_by`: which pass confirmed the hotel (`website`, `overture` or `google`).
- `match_name`, `match_address`, `match_phone`, `match_website`: what Overture or Google has on file.
- `url_shows_address`: whether the hotel's page shows its address.
- `url_matches_reference`: whether the listed site matches the one Overture or Google has.
- `suggested_url`: a replacement website when the listed one is missing, broken or different.
- `action`: a plain-English next step.
- `google_maps_url`: links to the place for quick manual checks.

## Tests

```bash
.venv/bin/python -m pytest -q
```

The tests use fake Google and web responses, so no key or network access is needed.

## Ideas for later iterations
- Read from and write to the SQL database directly instead of using Excel exports.
- Check the pet policy text on the hotel's site.
- Schedule periodic re-validation and flag only what changed.

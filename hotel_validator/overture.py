"""Free second pass: match hotels against Overture Maps' open places data.

The first run downloads every US lodging place from the latest Overture release (~300k rows,
~30 MB, about 1-2 minutes) into .cache/. Later runs reuse that file until --refresh-overture.
"""

import re
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

import requests

from .matching import zip5
from .places import Place

BUCKET = "https://overturemaps-us-west-2.s3.amazonaws.com"
S3_ROOT = "s3://overturemaps-us-west-2/release"
STATUS_MAP = {"open": "OPERATIONAL", "permanently_closed": "CLOSED_PERMANENTLY",
              "temporarily_closed": "CLOSED_TEMPORARILY"}
# Websites in Overture that aren't the hotel's own site - never suggest these.
NOT_A_HOTEL_SITE = {"facebook.com", "instagram.com", "linktr.ee", "yelp.com", "tripadvisor.com", "booking.com",
                    "expedia.com", "hotels.com", "agoda.com", "kayak.com", "trivago.com", "twitter.com", "x.com",
                    "google.com", "goo.gl", "airbnb.com", "vrbo.com", "bit.ly"}


def _own_site(url: str) -> bool:
    host = (urlparse(url if "//" in url else "//" + url).hostname or "").lower()
    return bool(host) and not any(host == d or host.endswith("." + d) for d in NOT_A_HOTEL_SITE)


def latest_release() -> str:
    resp = requests.get(f"{BUCKET}/?list-type=2&prefix=release/&delimiter=/", timeout=30)
    resp.raise_for_status()
    releases = re.findall(r"<Prefix>release/([^<]+)/</Prefix>", resp.text)
    if not releases:
        raise RuntimeError("Could not list Overture releases")
    return sorted(releases)[-1]


def download(cache_dir: Path, release: str | None = None) -> Path:
    import duckdb  # only needed for the download

    release = release or latest_release()
    out = cache_dir / f"overture_us_lodging_{release}.parquet"
    tmp = out.with_suffix(".tmp")
    print(f"Downloading US lodging places from Overture release {release} (one-time, ~1-2 min)...", flush=True)
    con = duckdb.connect()
    con.sql("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
    con.sql(f"""COPY (
        SELECT id, names.primary AS name, a.freeform AS street, a.locality AS city, a.postcode AS zip,
               replace(a.region, 'US-', '') AS state, phones, websites, operating_status, confidence,
               taxonomy.primary AS category
        FROM (SELECT *, addresses[1] AS a FROM read_parquet('{S3_ROOT}/{release}/theme=places/type=place/*')
              WHERE bbox.xmin BETWEEN -180 AND -60 AND bbox.ymin BETWEEN 15 AND 72
                AND taxonomy.hierarchy[1] = 'lodging')
        WHERE a.country = 'US'
    ) TO '{tmp}' (FORMAT parquet)""")
    tmp.rename(out)
    return out


def _place(r: dict) -> Place:
    street, city, state, z = r["street"] or "", r["city"] or "", r["state"] or "", zip5(r["zip"] or "")
    sites = [w for w in (r["websites"] or []) if w and _own_site(w)]
    return Place(
        place_id=r["id"], name=r["name"] or "", address=f"{street}, {city}, {state} {z}".strip(", "),
        street=street, zip=z, state=state, city=city,
        business_status=STATUS_MAP.get(r["operating_status"] or "", ""),
        website=sites[0] if sites else "", phone=(r["phones"] or [""])[0] or "", maps_url="",
        types=["lodging", r["category"] or ""],
    )


class OvertureIndex:
    def __init__(self, path: Path):
        import duckdb

        self.path = path
        self.release = path.stem.removeprefix("overture_us_lodging_")
        cur = duckdb.connect().execute("SELECT * FROM read_parquet(?)", [str(path)])
        names = [d[0] for d in cur.description]
        records = [dict(zip(names, r)) for r in cur.fetchall()]
        self.confidence = {}
        self.by_zip, self.by_city = defaultdict(list), defaultdict(list)
        for r in records:
            p = _place(r)
            self.confidence[p.place_id] = r["confidence"] or 0.0
            if p.zip:
                self.by_zip[p.zip].append(p)
            if p.city and p.state:
                self.by_city[(p.state.upper(), p.city.lower())].append(p)
        self.count = len(records)

    @classmethod
    def load(cls, cache_dir: Path, refresh: bool = False, max_age_days: int = 45) -> "OvertureIndex":
        """Use the local copy unless it's missing, older than max_age_days, or refresh is asked for."""
        existing = sorted(cache_dir.glob("overture_us_lodging_*.parquet"))
        stale = existing and (time.time() - existing[-1].stat().st_mtime) > max_age_days * 86400
        if refresh or not existing or stale:
            if stale:
                print(f"Local Overture data is over {max_age_days} days old - fetching the latest release.")
            try:
                path = download(cache_dir)
            except Exception as e:  # offline / S3 hiccup: an older copy beats no free pass at all
                if not existing:
                    raise
                print(f"WARNING: Overture download failed ({e.__class__.__name__}); using {existing[-1].name}")
                return cls(existing[-1])
            for old in existing:  # keep only the newest release on disk
                if old != path:
                    old.unlink()
        else:
            path = existing[-1]
        return cls(path)

    def candidates(self, zip_code: str, state: str, city: str) -> list[Place]:
        found = {p.place_id: p for p in self.by_zip.get(zip_code[:5], [])}
        if city and state:  # catches rows whose zip is wrong in the source data
            found.update({p.place_id: p for p in self.by_city.get((state.upper(), city.lower()), [])})
        return list(found.values())

"""Google Places API (New) Text Search client with an on-disk response cache.

Responses are cached in a small SQLite file so a re-run (e.g. after a crash or a fix to the
spreadsheet) costs nothing. Cached answers expire after CACHE_MAX_AGE_DAYS, so next year's run
always asks Google again instead of reusing stale results.

Billing tier is set by the fields requested:
  pro        - name, address, business status (closed?), types. 5,000 free requests/month.
  enterprise - adds website and phone. Only 1,000 free requests/month.
"""

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field

import requests

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
PRO_FIELDS = ["places.id", "places.displayName", "places.formattedAddress", "places.addressComponents",
              "places.businessStatus", "places.types", "places.googleMapsUri"]
ENTERPRISE_FIELDS = PRO_FIELDS + ["places.websiteUri", "places.nationalPhoneNumber", "places.internationalPhoneNumber"]
FIELD_MASKS = {"pro": ",".join(PRO_FIELDS), "enterprise": ",".join(ENTERPRISE_FIELDS)}
CACHE_MAX_AGE_DAYS = 30
LODGING_TYPES = {"lodging", "hotel", "motel", "resort_hotel", "extended_stay_hotel", "inn", "bed_and_breakfast",
                 "guest_house", "hostel", "cottage", "campground", "rv_park", "private_guest_room",
                 "budget_japanese_inn", "farmstay", "japanese_inn"}


@dataclass
class Place:
    place_id: str
    name: str
    address: str
    street: str
    zip: str
    state: str
    city: str
    business_status: str
    website: str
    phone: str
    maps_url: str
    types: list = field(default_factory=list)

    @property
    def is_lodging(self) -> bool:
        return bool(LODGING_TYPES & set(self.types))

    @classmethod
    def from_api(cls, p: dict) -> "Place":
        comps = {t: c for c in p.get("addressComponents", []) for t in c.get("types", [])}
        get = lambda t, k="shortText": comps.get(t, {}).get(k, "")
        street = " ".join(x for x in (get("street_number"), get("route")) if x)
        if not street:
            street = p.get("formattedAddress", "").split(",")[0]
        return cls(
            place_id=p.get("id", ""),
            name=p.get("displayName", {}).get("text", ""),
            address=p.get("formattedAddress", ""),
            street=street,
            zip=get("postal_code"),
            state=get("administrative_area_level_1"),
            city=get("locality", "longText"),
            business_status=p.get("businessStatus", ""),
            website=p.get("websiteUri", ""),
            phone=p.get("nationalPhoneNumber", "") or p.get("internationalPhoneNumber", ""),
            maps_url=p.get("googleMapsUri", ""),
            types=p.get("types", []),
        )


class PlacesClient:
    def __init__(self, api_key: str, cache_path: str = ".cache/places.sqlite", timeout: int = 20,
                 tier: str = "pro", max_age_days: float = CACHE_MAX_AGE_DAYS):
        if tier not in FIELD_MASKS:
            raise ValueError(f"tier must be one of {list(FIELD_MASKS)}")
        self.api_key, self.tier = api_key, tier
        self.min_ts = time.time() - max_age_days * 86400
        self.timeout = timeout
        self.session = requests.Session()
        self.calls = 0  # billable calls actually made this run (cache misses)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(cache_path, check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, response TEXT, ts REAL)")
        self._db.commit()

    def _key(self, query: str, lodging_only: bool, max_results: int) -> tuple[dict, str]:
        body = {"textQuery": query, "pageSize": max_results, "regionCode": "us", "languageCode": "en"}
        if lodging_only:
            body["includedType"] = "lodging"
        # tier is part of the key: a cached Pro answer has no website/phone to give an Enterprise run
        return body, json.dumps({**body, "_tier": self.tier}, sort_keys=True)

    def _cached(self, key: str):
        with self._lock:
            return self._db.execute("SELECT response FROM cache WHERE key = ? AND ts >= ?", (key, self.min_ts)).fetchone()

    def is_cached(self, query: str, lodging_only: bool = False, max_results: int = 5) -> bool:
        return self._cached(self._key(query, lodging_only, max_results)[1]) is not None

    def search(self, query: str, lodging_only: bool = False, max_results: int = 5) -> list[Place]:
        body, key = self._key(query, lodging_only, max_results)
        row = self._cached(key)
        if row:
            data = json.loads(row[0])
        else:
            data = self._post(body)
            with self._lock:
                self._db.execute("INSERT OR REPLACE INTO cache VALUES (?, ?, ?)", (key, json.dumps(data), time.time()))
                self._db.commit()
        return [Place.from_api(p) for p in data.get("places", [])]

    def _post(self, body: dict) -> dict:
        headers = {"X-Goog-Api-Key": self.api_key, "X-Goog-FieldMask": FIELD_MASKS[self.tier],
                   "Content-Type": "application/json"}
        for attempt in range(4):
            resp = self.session.post(SEARCH_URL, json=body, headers=headers, timeout=self.timeout)
            with self._lock:
                self.calls += 1
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            if resp.status_code in (400, 401, 403) and ("API key" in resp.text or "PERMISSION_DENIED" in resp.text
                                                           or "BILLING" in resp.text.upper()):
                try:
                    msg = resp.json()["error"]["message"]
                except (ValueError, KeyError, TypeError):
                    msg = resp.text[:300]
                raise PlacesAuthError(f"Google rejected the API key / project setup: {msg}")
            if resp.status_code != 200:
                raise PlacesError(f"Places API HTTP {resp.status_code}: {resp.text[:300]}")
            return resp.json()
        raise PlacesError("Places API retries exhausted")


class PlacesError(Exception):
    pass


class PlacesAuthError(Exception):
    """Bad key, API not enabled, or billing off - fatal for the whole run (not a PlacesError)."""

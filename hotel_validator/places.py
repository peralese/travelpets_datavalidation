"""Google Places API (New) Text Search client with an on-disk response cache.

Re-running the same extract costs nothing for rows already looked up, because
responses are cached by query text in a small SQLite file.
"""

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field

import requests

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = ",".join([
    "places.id", "places.displayName", "places.formattedAddress", "places.addressComponents",
    "places.businessStatus", "places.websiteUri", "places.nationalPhoneNumber",
    "places.internationalPhoneNumber", "places.types", "places.googleMapsUri",
])
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
    def __init__(self, api_key: str, cache_path: str = ".cache/places.sqlite", timeout: int = 20):
        self.api_key = api_key
        self.timeout = timeout
        self.session = requests.Session()
        self.calls = 0  # billable calls actually made this run (cache misses)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(cache_path, check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, response TEXT, ts REAL)")
        self._db.commit()

    @staticmethod
    def _key(query: str, lodging_only: bool, max_results: int) -> tuple[dict, str]:
        body = {"textQuery": query, "pageSize": max_results, "regionCode": "us", "languageCode": "en"}
        if lodging_only:
            body["includedType"] = "lodging"
        return body, json.dumps(body, sort_keys=True)

    def is_cached(self, query: str, lodging_only: bool = False, max_results: int = 5) -> bool:
        _, key = self._key(query, lodging_only, max_results)
        with self._lock:
            return self._db.execute("SELECT 1 FROM cache WHERE key = ?", (key,)).fetchone() is not None

    def search(self, query: str, lodging_only: bool = False, max_results: int = 5) -> list[Place]:
        body, key = self._key(query, lodging_only, max_results)
        with self._lock:
            row = self._db.execute("SELECT response FROM cache WHERE key = ?", (key,)).fetchone()
        if row:
            data = json.loads(row[0])
        else:
            data = self._post(body)
            with self._lock:
                self._db.execute("INSERT OR REPLACE INTO cache VALUES (?, ?, ?)", (key, json.dumps(data), time.time()))
                self._db.commit()
        return [Place.from_api(p) for p in data.get("places", [])]

    def _post(self, body: dict) -> dict:
        headers = {"X-Goog-Api-Key": self.api_key, "X-Goog-FieldMask": FIELD_MASK,
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

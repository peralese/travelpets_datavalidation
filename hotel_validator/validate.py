"""Decide, for one hotel row, whether it still exists at the listed address and its URL works.

Rows go through up to three passes, stopping as soon as one confirms the hotel:

  1. website  (free)  - the hotel's own page loads, names the hotel and shows its address or phone
  2. overture (free)  - Overture Maps open data has an open lodging with the same name and address
  3. google   (paid)  - Google Places lookup, which also detects closures, moves and rebrands

The free passes can only *confirm* a hotel. Anything that looks closed, moved or renamed is left
for Google (or manual review), because stale free data shouldn't be the reason a listing is removed.
"""

import re
from dataclasses import asdict, dataclass

import requests

from .matching import address_match, name_score, phone_match
from .places import Place, PlacesClient, PlacesError
from .url_check import UrlResult, check_url, same_site

NAME_OK = 80        # name similarity that counts as "same hotel"
NAME_WEAK = 60      # acceptable only when address or phone also agree
OVERTURE_MIN_CONFIDENCE = 0.6

# Hotel status values, most to least healthy.
VERIFIED = "VERIFIED"
NAME_CHANGED = "NAME_CHANGED"            # a different lodging now at this address (rebrand / sold)
ADDRESS_MISMATCH = "ADDRESS_MISMATCH"    # hotel found, but Google has a different address
CLOSED_TEMPORARILY = "CLOSED_TEMPORARILY"
CLOSED_PERMANENTLY = "CLOSED_PERMANENTLY"
NOT_FOUND = "NOT_FOUND"
UNRESOLVED = "UNRESOLVED"                # free passes couldn't confirm it and Google wasn't used
ERROR = "ERROR"


@dataclass
class HotelRow:
    name: str
    address: str
    city: str
    state: str
    zip: str
    phone: str
    url: str


@dataclass
class Result:
    hotel_status: str = ""
    verified_by: str = ""         # website / overture / google
    confidence: str = ""          # HIGH / MEDIUM / LOW
    match_notes: str = ""
    match_name: str = ""          # what the reference source (Overture or Google) calls the place
    match_address: str = ""
    match_phone: str = ""
    match_website: str = ""
    match_business_status: str = ""
    google_maps_url: str = ""
    name_score: int | str = ""
    address_match: str = ""
    phone_match: str = ""
    url_status: str = ""
    url_http_code: str = ""
    url_final: str = ""
    url_shows_address: str = ""
    url_matches_reference: str = ""
    url_notes: str = ""
    suggested_url: str = ""
    action: str = ""              # plain-English next step for whoever updates the database

    def as_dict(self) -> dict:
        return asdict(self)


def _query(row: HotelRow, with_name: bool = True) -> str:
    parts = [row.name] if with_name else []
    parts += [row.address]
    if row.city and row.city.lower() not in row.address.lower():
        parts.append(row.city)
    parts.append(f"{row.state} {row.zip}".strip())
    return ", ".join(p for p in parts if p)


def _score(row: HotelRow, p: Place) -> dict:
    addr, _ = address_match(row.address, row.zip, p.street, p.zip)
    ignore = f"{row.city} {p.city} {row.state} {p.state}"
    return {"place": p, "name": name_score(row.name, p.name, ignore), "addr": addr, "phone": phone_match(row.phone, p.phone)}


def _rank(s: dict) -> tuple:
    return (s["name"] >= NAME_OK, s["addr"] == "MATCH", s["phone"] == "MATCH", s["addr"] == "PARTIAL", s["name"])


# --- pass 2: Overture ---------------------------------------------------------
def overture_confirm(row: HotelRow, index) -> Result | None:
    """Return a VERIFIED result if Overture clearly has this hotel open at this address, else None."""
    scored = [_score(row, p) for p in index.candidates(row.zip, row.state, row.city)]
    good = [s for s in scored
            if s["name"] >= NAME_OK and s["addr"] == "MATCH" and s["phone"] != "MISMATCH"
            and s["place"].business_status != "CLOSED_PERMANENTLY"
            and index.confidence.get(s["place"].place_id, 0) >= OVERTURE_MIN_CONFIDENCE]
    if not good:
        return None
    # Overture often keeps a rebranded hotel's old record next to the new one. If another lodging
    # with a different name shares this address, it's ambiguous - let Google decide.
    rivals = [s for s in scored if s["addr"] == "MATCH" and s["name"] < NAME_WEAK and _is_hotel_record(s["place"], index)]
    if rivals:
        return None
    best = max(good, key=_rank)
    res = Result(hotel_status=VERIFIED, verified_by="overture",
                 confidence="HIGH" if best["phone"] == "MATCH" else "MEDIUM")
    _fill(res, best)
    return res


HOTEL_CATEGORIES = {"hotel", "motel", "inn", "resort"}
COMPANY_NAME = re.compile(r"\b(llc|inc|corp|corporation|associates|partners|hospitality|owner|development|"
                          r"management|holdings|properties|investments?|group|ltd|lp)\b", re.I)


def _is_hotel_record(p: Place, index) -> bool:
    """An Overture record that really is a hotel (not the owning company, a cabin rental or a bar)."""
    return (p.types[-1] in HOTEL_CATEGORIES and p.business_status != "CLOSED_PERMANENTLY"
            and not COMPANY_NAME.search(p.name) and index.confidence.get(p.place_id, 0) >= OVERTURE_MIN_CONFIDENCE)


# --- pass 3: Google -----------------------------------------------------------
def find_hotel(row: HotelRow, client: PlacesClient) -> Result:
    candidates = [_score(row, p) for p in client.search(_query(row))]
    best = max(candidates, key=_rank, default=None)
    res = Result(verified_by="google")

    same_hotel = best and (
        best["name"] >= NAME_OK
        or (best["name"] >= NAME_WEAK and (best["addr"] == "MATCH" or best["phone"] == "MATCH"))
    )  # a phone match alone isn't enough: rebranded hotels usually keep their phone line

    if not same_hotel:
        # Not found by name - see what lodging (if any) Google lists at the address now.
        at_address = [_score(row, p) for p in client.search(_query(row, with_name=False), lodging_only=True)]
        at_address = [s for s in at_address if s["addr"] == "MATCH" and s["place"].is_lodging]
        if at_address:
            other = max(at_address, key=lambda s: (s["phone"] == "MATCH", s["name"]))
            _fill(res, other)
            res.hotel_status = NAME_CHANGED
            res.confidence = "MEDIUM" if other["phone"] == "MATCH" else "LOW"
            res.match_notes = f"Address now listed as '{other['place'].name}' - possible rebrand or new owner"
            return _closed_override(res, other["place"])
        if best:
            _fill(res, best)  # show nearest candidate to help manual review
        res.hotel_status = NOT_FOUND
        res.confidence = "MEDIUM" if not candidates else "LOW"
        res.match_notes = ("Google returned no results" if not candidates
                           else f"Closest Google result '{best['place'].name}' does not match")
        return res

    _fill(res, best)
    p = best["place"]
    if best["addr"] == "MISMATCH":
        res.hotel_status = ADDRESS_MISMATCH
        res.confidence = "MEDIUM" if best["phone"] == "MATCH" else "LOW"
        res.match_notes = f"Hotel found at a different address: {p.address}"
    else:
        res.hotel_status = VERIFIED
        strong = best["name"] >= NAME_OK and best["addr"] == "MATCH"
        res.confidence = "HIGH" if strong and best["phone"] != "MISMATCH" else "MEDIUM"
        notes = []
        if best["addr"] == "PARTIAL":
            notes.append(f"Address differs slightly - Google: {p.address}")
        if best["phone"] == "MISMATCH":
            notes.append(f"Phone differs - Google: {p.phone}")
        if best["name"] < NAME_OK:
            notes.append(f"Name differs - Google: '{p.name}'")
        res.match_notes = "; ".join(notes)
    return _closed_override(res, p)


def _closed_override(res: Result, p: Place) -> Result:
    if p.business_status == "CLOSED_PERMANENTLY":
        res.hotel_status = CLOSED_PERMANENTLY
        res.match_notes = "; ".join(x for x in ["Google marks this place permanently closed", res.match_notes] if x)
    elif p.business_status == "CLOSED_TEMPORARILY":
        res.hotel_status = CLOSED_TEMPORARILY
        res.match_notes = "; ".join(x for x in ["Google marks this place temporarily closed", res.match_notes] if x)
    return res


def _fill(res: Result, s: dict) -> None:
    p = s["place"]
    res.match_name, res.match_address, res.match_phone = p.name, p.address, p.phone
    res.match_website, res.match_business_status, res.google_maps_url = p.website, p.business_status, p.maps_url
    res.name_score, res.address_match, res.phone_match = s["name"], s["addr"], s["phone"]


# --- the pipeline ---------------------------------------------------------------
def free_passes(row: HotelRow, session: requests.Session, overture=None) -> tuple[Result | None, UrlResult]:
    """Passes 1 and 2. Returns (result or None if still unconfirmed, website check)."""
    u = check_url(row.url, row.name, session, street=row.address, phone=row.phone)
    if u.confirms_hotel:
        shows = [x for x, ok in (("address", u.address_on_page), ("phone", u.phone_on_page)) if ok]
        return Result(hotel_status=VERIFIED, verified_by="website",
                      confidence="HIGH" if u.address_on_page else "MEDIUM",
                      match_notes=f"Hotel website shows the listed {' and '.join(shows)}"), u
    if overture is not None:
        return overture_confirm(row, overture), u
    return None, u


def google_pass(row: HotelRow, client: PlacesClient) -> Result:
    try:
        return find_hotel(row, client)
    except (PlacesError, requests.RequestException) as e:
        return Result(hotel_status=ERROR, match_notes=str(e)[:300])


def unresolved(reason: str) -> Result:
    return Result(hotel_status=UNRESOLVED, match_notes=f"Not confirmed by free checks; {reason}")


def finish(row: HotelRow, res: Result, u: UrlResult, session: requests.Session) -> Result:
    """Attach the website check and the recommended action."""
    if res.hotel_status == NAME_CHANGED and res.match_name and row.url:
        # a rebranded property's page should be judged against its new name
        u = check_url(row.url, res.match_name, session, street=row.address, phone=row.phone)
    _apply_url(res, row, u)
    res.action = _action(res)
    return res


def validate_row(row: HotelRow, session: requests.Session, overture=None, client: PlacesClient | None = None) -> Result:
    """All passes for a single row (the CLI runs the passes in bulk instead, to report Google usage first)."""
    res, u = free_passes(row, session, overture)
    if res is None:
        res = google_pass(row, client) if client else unresolved("Google lookup not enabled")
    return finish(row, res, u, session)


def _apply_url(res: Result, row: HotelRow, u: UrlResult) -> None:
    res.url_status, res.url_http_code, res.url_final, res.url_notes = u.status, u.http_code, u.final_url, u.notes
    if u.status in ("OK", "REDIRECTED"):
        res.url_shows_address = "YES" if u.address_on_page else ("PHONE_ONLY" if u.phone_on_page else "NO")
    if res.match_website and row.url:
        res.url_matches_reference = "YES" if (same_site(u.final_url or row.url, res.match_website)
                                              or same_site(row.url, res.match_website)) else "NO"
    if res.match_website and (u.status not in ("OK", "BLOCKED") or res.url_matches_reference == "NO"
                              or res.hotel_status == NAME_CHANGED):
        res.suggested_url = res.match_website


def _action(r: Result) -> str:
    s = r.hotel_status
    if s == CLOSED_PERMANENTLY:
        return "Remove listing (permanently closed)"
    if s == NOT_FOUND:
        return "Review manually - hotel not found at address; likely remove"
    if s == NAME_CHANGED:
        return "Review - property appears to have a new name; confirm pet policy before updating"
    if s == ADDRESS_MISMATCH:
        return "Review - update address if Google's is correct"
    if s == CLOSED_TEMPORARILY:
        return "Review - temporarily closed; consider hiding listing"
    if s == UNRESOLVED:
        return "Run Google pass or check manually"
    if s == ERROR:
        return "Re-run - lookup failed"
    url_action = {
        "OK": "", "MISSING": "Add URL" + (" (see suggested_url)" if r.suggested_url else ""),
        "BROKEN": "Fix URL", "UNREACHABLE": "Fix URL", "PARKED": "Replace URL (domain parked)",
        "INVALID": "Fix URL", "REDIRECTED": "Update URL to final destination",
        "CONTENT_MISMATCH": "Check URL - page doesn't mention hotel", "BLOCKED": "Check URL manually",
    }.get(r.url_status, "")
    if url_action and r.suggested_url and r.url_status != "MISSING":
        url_action += " (see suggested_url)"
    if not url_action and r.url_matches_reference == "NO":
        url_action = "URL works but differs from the hotel's listed site - check suggested_url"
    if s == VERIFIED and r.confidence != "HIGH" and r.match_notes and r.verified_by == "google":
        url_action = "; ".join(x for x in ["Spot-check details", url_action] if x)
    return url_action or "No action needed"

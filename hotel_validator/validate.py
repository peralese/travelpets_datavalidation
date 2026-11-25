"""Decide, for one hotel row, whether it still exists at the listed address and its URL works.

Rows go through up to three passes, stopping as soon as one confirms the hotel:

  1. website  (free)  - the hotel's own page loads, names the hotel and shows its address or phone
  2. overture (free)  - Overture Maps open data has an open lodging with the same name and address
  2b. overture_website (free) - the hotel's website as listed in Overture (when it differs from the
                       one on file) names the hotel and shows its address or phone
  3. google   (paid)  - Google Places lookup, which also detects closures, moves and rebrands

The free passes can only *confirm* a hotel. Anything that looks closed, moved or renamed is left
for Google (or manual review), because stale free data shouldn't be the reason a listing is removed.
"""

import re
from dataclasses import asdict, dataclass

import requests

from .matching import (address_match, name_score, normalize_street, phone_match, same_brand, street_line,
                       street_number)
from .places import Place, PlacesClient, PlacesError
from .url_check import UrlResult, check_url, normalize_url, same_site

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
    verified_by: str = ""         # website / overture / overture_website / google
    confidence: str = ""          # HIGH / MEDIUM / LOW
    match_notes: str = ""
    overture_hint: str = ""       # free warning for unconfirmed hotels (possible rebrand / closure); never a verdict
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
    if _has_rival(scored, index):
        return None
    best = max(good, key=_rank)
    res = Result(hotel_status=VERIFIED, verified_by="overture",
                 confidence="HIGH" if best["phone"] == "MATCH" else "MEDIUM")
    _fill(res, best)
    return res


HOTEL_CATEGORIES = {"hotel", "motel", "inn", "resort"}
COMPANY_NAME = re.compile(r"\b(llc|inc|corp|corporation|associates|partners|hospitality|owner|development|"
                          r"management|holdings|properties|investments?|group|ltd|lp)\b", re.I)


def _has_rival(scored: list[dict], index) -> bool:
    """Overture often keeps a rebranded hotel's old record next to the new one. If another lodging
    with a different name shares this address, it's ambiguous - let Google decide."""
    return any(s["addr"] == "MATCH" and s["name"] < NAME_WEAK and _is_hotel_record(s["place"], index) for s in scored)


# --- pass 2b: the website Overture has for this hotel ---------------------------
MAX_OVERTURE_SITES = 2


def overture_website_confirm(row: HotelRow, index, session: requests.Session, checked: UrlResult) -> tuple[Result | None, str]:
    """Check the website(s) Overture lists for this hotel, when they differ from the one on file.

    Returns (VERIFIED result or None, a working site that names the hotel, for suggested_url).
    Same bar as pass 1: the page must name the hotel and show its listed address or phone.
    """
    scored = [_score(row, p) for p in index.candidates(row.zip, row.state, row.city)]
    if _has_rival(scored, index):
        return None, ""
    named = sorted((s for s in scored if s["name"] >= NAME_OK and s["place"].website
                    and s["place"].business_status != "CLOSED_PERMANENTLY"), key=_rank, reverse=True)
    on_file = [x for x in (normalize_url(row.url), checked.final_url) if x]
    sites, working = [], ""
    for s in named:
        w = s["place"].website
        if not any(same_site(w, x) for x in on_file + sites):
            sites.append(w)
    for w in sites[:MAX_OVERTURE_SITES]:
        u = check_url(w, row.name, session, street=row.address, phone=row.phone)
        if u.confirms_hotel:
            shows = " and ".join(x for x, ok in (("address", u.address_on_page), ("phone", u.phone_on_page)) if ok)
            res = Result(hotel_status=VERIFIED, verified_by="overture_website",
                         confidence="HIGH" if u.address_on_page else "MEDIUM",
                         match_notes=f"The hotel's website in Overture ({u.final_url or w}) shows the listed {shows}")
            res.match_website = u.final_url or w
            return res, res.match_website
        if u.status in ("OK", "REDIRECTED") and not working:
            working = u.final_url or w
    return None, working


def _is_hotel_record(p: Place, index) -> bool:
    """An Overture record that really is a hotel (not the owning company, a cabin rental or a bar)."""
    return (p.types[-1] in HOTEL_CATEGORIES and p.business_status != "CLOSED_PERMANENTLY"
            and not COMPANY_NAME.search(p.name) and index.confidence.get(p.place_id, 0) >= OVERTURE_MIN_CONFIDENCE)


# --- pass 3: Google -----------------------------------------------------------
def _same_state(row: HotelRow, p: Place) -> bool:
    return not (row.state and p.state) or row.state.strip().upper() == p.state.strip().upper()


def _same_hotel(row: HotelRow, s: dict) -> bool:
    """Google's place is this hotel: at (or right by) the listed address, under a similar name.

    A matching name somewhere else isn't enough - chains have several branches per city, and
    hotels almost never move. Those cases become NOT_FOUND with the other branch in the notes.
    """
    if s["addr"] == "MATCH":
        return s["name"] >= NAME_WEAK or same_brand(row.name, s["place"].name, f"{row.city} {row.state}")
    if s["addr"] != "PARTIAL":
        return False
    # Same zip, close address. If both have street numbers and they differ, it may be another branch
    # in the same zip, so the name must be a strong match; if ours has no number ("I-95 Exit 77 / ..."),
    # a similar name is enough.
    ours, theirs = street_number(normalize_street(street_line(row.address))), street_number(normalize_street(s["place"].street))
    return s["name"] >= (NAME_OK if ours and theirs and ours != theirs else NAME_WEAK)


def find_hotel(row: HotelRow, client: PlacesClient) -> Result:
    candidates = [s for s in (_score(row, p) for p in client.search(_query(row))) if _same_state(row, s["place"])]
    res = Result(verified_by="google")

    same = [s for s in candidates if _same_hotel(row, s)]
    if same:
        best = max(same, key=_rank)
        _fill(res, best)
        p = best["place"]
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

    # Same name and same phone at another address: the hotel really moved (or our address is wrong).
    # Only possible on the enterprise tier, which returns phone numbers.
    moved = [s for s in candidates if s["name"] >= NAME_OK and s["addr"] == "MISMATCH" and s["phone"] == "MATCH"]
    if moved:
        best = max(moved, key=_rank)
        _fill(res, best)
        res.hotel_status, res.confidence = ADDRESS_MISMATCH, "MEDIUM"
        res.match_notes = f"Hotel found at a different address: {best['place'].address}"
        return _closed_override(res, best["place"])

    # Not found by name: see what lodging (if any) Google lists at the address now. The first
    # search often already returned it, which saves a second request.
    at_address = [s for s in candidates if s["addr"] == "MATCH" and s["place"].is_lodging]
    if not at_address:
        at_address = [s for s in (_score(row, p) for p in client.search(_query(row, with_name=False), lodging_only=True))
                      if s["addr"] == "MATCH" and s["place"].is_lodging and _same_state(row, s["place"])]
    if at_address:
        other = max(at_address, key=lambda s: (s["phone"] == "MATCH", s["name"]))
        _fill(res, other)
        res.hotel_status = NAME_CHANGED
        res.confidence = "MEDIUM" if other["phone"] == "MATCH" else "LOW"
        res.match_notes = f"Address now listed as '{other['place'].name}' - possible rebrand or new owner"
        return _closed_override(res, other["place"])

    best = max(candidates, key=_rank, default=None)
    if best:
        _fill(res, best)  # show nearest candidate to help manual review
    res.hotel_status = NOT_FOUND
    res.confidence = "MEDIUM" if not candidates else "LOW"
    if not candidates:
        res.match_notes = "Google returned no results"
    elif best["name"] >= NAME_OK:
        res.match_notes = (f"Nothing at the listed address. Google has '{best['place'].name}' at "
                           f"{best['place'].address} - a different branch, or the listed address is wrong")
    else:
        res.match_notes = f"Closest Google result '{best['place'].name}' does not match"
    return res


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


@dataclass
class FreeHints:
    """What the free passes learned about a hotel they couldn't confirm."""
    alt_site: str = ""        # working site from Overture that names the hotel (for suggested_url)
    overture_hint: str = ""   # possible rebrand / closure, for review


def overture_hint(row: HotelRow, index) -> str:
    """Describe what Overture lists at this address, when that suggests a rebrand or closure."""
    scored = [_score(row, p) for p in index.candidates(row.zip, row.state, row.city)]
    ours = [s for s in scored if s["name"] >= NAME_OK and s["addr"] == "MATCH"]
    others = sorted((s for s in scored if s["addr"] == "MATCH" and s["name"] < NAME_WEAK
                     and _is_hotel_record(s["place"], index)),
                    key=lambda s: -index.confidence.get(s["place"].place_id, 0))
    names = " and ".join(f"'{n}'" for n in dict.fromkeys(s["place"].name for s in others[:2]))
    if others and not ours:
        return f"Possible rebrand: Overture lists {names} at this address, not this hotel"
    if others:
        return f"Overture lists this hotel and also {names} at this address"
    if ours and all(s["place"].business_status == "CLOSED_PERMANENTLY" for s in ours):
        return "Overture marks this hotel permanently closed"
    return ""


# --- the pipeline ---------------------------------------------------------------
def free_passes(row: HotelRow, session: requests.Session, overture=None) -> tuple[Result | None, UrlResult, FreeHints]:
    """Passes 1, 2 and 2b.

    Returns (result or None if still unconfirmed, check of the URL on file, hints for unconfirmed hotels).
    """
    u = check_url(row.url, row.name, session, street=row.address, phone=row.phone)
    if u.confirms_hotel:
        shows = [x for x, ok in (("address", u.address_on_page), ("phone", u.phone_on_page)) if ok]
        return Result(hotel_status=VERIFIED, verified_by="website",
                      confidence="HIGH" if u.address_on_page else "MEDIUM",
                      match_notes=f"Hotel website shows the listed {' and '.join(shows)}"), u, FreeHints()
    if overture is None:
        return None, u, FreeHints()
    res = overture_confirm(row, overture)
    if res:
        return res, u, FreeHints()
    res, alt = overture_website_confirm(row, overture, session, u)
    return res, u, FreeHints(alt_site=alt, overture_hint="" if res else overture_hint(row, overture))


def google_pass(row: HotelRow, client: PlacesClient) -> Result:
    try:
        return find_hotel(row, client)
    except (PlacesError, requests.RequestException) as e:
        return Result(hotel_status=ERROR, match_notes=str(e)[:300])


def unresolved(reason: str) -> Result:
    return Result(hotel_status=UNRESOLVED, match_notes=f"Not confirmed by free checks; {reason}")


def finish(row: HotelRow, res: Result, u: UrlResult, session: requests.Session,
           hints: FreeHints | None = None) -> Result:
    """Attach the website check, the free-pass hints and the recommended action.

    hints.alt_site is a working site from Overture that names the hotel (pass 2b). Google's Pro tier
    returns no websites, so this is often the only replacement for a missing or dead URL.
    """
    hints = hints or FreeHints()
    alt_site, res.overture_hint = hints.alt_site, hints.overture_hint
    if res.hotel_status == NAME_CHANGED and res.match_name and row.url:
        # a rebranded property's page should be judged against its new name
        u = check_url(row.url, res.match_name, session, street=row.address, phone=row.phone)
    _apply_url(res, row, u)
    if (alt_site and not res.suggested_url and u.status != "OK"
            and res.hotel_status in (VERIFIED, ADDRESS_MISMATCH, CLOSED_TEMPORARILY, UNRESOLVED)):
        res.suggested_url = alt_site
        res.url_notes = "; ".join(x for x in [res.url_notes, "suggested_url is the hotel's website in Overture "
                                              "(names the hotel; address not shown)"] if x)
    res.action = _action(res)
    return res


def validate_row(row: HotelRow, session: requests.Session, overture=None, client: PlacesClient | None = None) -> Result:
    """All passes for a single row (the CLI runs the passes in bulk instead, to report Google usage first)."""
    res, u, hints = free_passes(row, session, overture)
    if res is None:
        res = google_pass(row, client) if client else unresolved("Google lookup not enabled")
    return finish(row, res, u, session, hints)


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
        return "Run Google pass or check manually" + (" - see overture_hint" if r.overture_hint else "")
    if s == ERROR:
        return "Re-run - lookup failed"
    url_action = {
        "OK": "", "MISSING": "Add URL" + (" (see suggested_url)" if r.suggested_url else ""),
        "BROKEN": "Fix URL", "UNREACHABLE": "Fix URL", "PARKED": "Replace URL (domain parked)",
        "INVALID": "Fix URL", "REDIRECTED": "Update URL to final destination",
        "CONTENT_MISMATCH": "Check URL - page doesn't mention hotel", "BLOCKED": "Check URL manually",
        "ERROR": "Re-run - URL check failed",
    }.get(r.url_status, "")
    if url_action and r.suggested_url and r.url_status != "MISSING":
        url_action += " (see suggested_url)"
    if not url_action and r.url_matches_reference == "NO":
        url_action = "URL works but differs from the hotel's listed site - check suggested_url"
    if s == VERIFIED and r.confidence != "HIGH" and r.match_notes and r.verified_by == "google":
        url_action = "; ".join(x for x in ["Spot-check details", url_action] if x)
    return url_action or "No action needed"

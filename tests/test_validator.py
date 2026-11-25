import pandas as pd
import pytest

from hotel_validator.columns import clean_zip, map_columns
from hotel_validator.matching import address_match, name_score, phone_match
from hotel_validator.places import Place
from hotel_validator.url_check import check_url, same_site
from hotel_validator import validate as v


def place(name, street, zip_, status="OPERATIONAL", phone="(512) 555-0100", website="", types=("lodging", "hotel")):
    return Place("pid", name, f"{street}, Austin, TX {zip_}, USA", street, zip_, "TX", "Austin", status, website, phone,
                 "https://maps.google.com/?cid=1", list(types))


class FakeClient:
    def __init__(self, by_name=(), at_address=()):
        self.by_name, self.at_address, self.calls = list(by_name), list(at_address), 0

    def search(self, query, lodging_only=False, max_results=5):
        self.calls += 1
        return self.at_address if lodging_only else self.by_name


class FakeOverture:
    def __init__(self, places, confidence=0.9):
        self.places, self.confidence = places, {p.place_id: confidence for p in places}

    def candidates(self, zip_code, state, city):
        return self.places


class FakeResp:
    def __init__(self, code=200, text="", url="https://example.com/"):
        self.status_code, self.text, self.url = code, text, url


class FakeSession:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc = resp, exc

    def get(self, url, **kw):
        if self.exc:
            raise self.exc
        return self.resp


ROW = v.HotelRow("Hampton Inn & Suites Austin Downtown", "200 San Jacinto Blvd", "Austin", "TX", "78701",
                 "512-555-0100", "")


# --- matching -------------------------------------------------------------
def test_name_score_tolerates_suffixes():
    assert name_score("Hampton Inn & Suites Austin Downtown",
                      "Hampton Inn & Suites Austin-Downtown/Convention Center") >= 80
    assert name_score("Hampton Inn Austin", "Motel 6 Austin", ignore="Austin TX") < 60
    # rebrands sharing location words must not look like the same hotel
    assert name_score("Hampton Inn & Suites Austin Downtown", "Holiday Inn Express Austin Downtown", "Austin") < 60
    assert name_score("La Quinta Inn Austin Airport", "Wyndham Garden Austin Airport", "Austin") < 60
    assert name_score("Example Lodge", "Example Domain") < 60
    assert name_score("Kimpton Hotel Van Zandt", "Hotel Van Zandt") >= 80
    assert name_score("The Driskil", "The Driskill Hotel") >= 80  # typo tolerance
    assert name_score("Navasota Inn", "Days Inn by Wyndham Navasota", "Navasota TX") < 80
    assert name_score("Navasota Inn", "Navasota Inn", "Navasota TX") == 100
    assert name_score("Courtyard by Marriott Irvine Spectrum", "Marriott", "Irvine CA") < 80
    assert name_score("Big Iron Motel", "Riggins Motel", "Riggins ID") < 60
    assert name_score("Red Roof Inn Austin North", "Red Roof PLUS+ Austin North", "Austin") >= 80
    assert name_score("Austin Marriott South", "Marriott Austin South", "Austin") == 100
    assert name_score("The Westin Austin Downtown", "Westin Austin Downtown Hotel", ignore="Austin") >= 90


def test_address_match_abbreviations():
    assert address_match("200 San Jacinto Boulevard, Austin", "78701", "200 San Jacinto Blvd", "78701")[0] == "MATCH"
    assert address_match("200 San Jacinto Blvd", "78701", "1900 Elm St", "75201")[0] == "MISMATCH"


def test_phone_and_zip():
    assert phone_match("1 (512) 555-0100", "512.555.0100") == "MATCH"
    assert phone_match("", "512") == "NO_PHONE"
    assert clean_zip("2134") == "02134" and clean_zip(2134.0) == "02134"


def test_column_detection():
    df = pd.DataFrame(columns=["Hotel Name", "Street Address", "State", "Zip Code", "Phone Number", "Website"])
    m = map_columns(df)
    assert m == {"name": "Hotel Name", "address": "Street Address", "state": "State", "zip": "Zip Code",
                 "phone": "Phone Number", "url": "Website"}
    with pytest.raises(ValueError):
        map_columns(pd.DataFrame(columns=["foo"]))


# --- hotel decisions ------------------------------------------------------
def test_verified():
    c = FakeClient([place("Hampton Inn & Suites Austin-Downtown/Convention Center", "200 San Jacinto Blvd", "78701")])
    r = v.find_hotel(ROW, c)
    assert (r.hotel_status, r.confidence) == ("VERIFIED", "HIGH")


def test_permanently_closed():
    c = FakeClient([place("Hampton Inn & Suites Austin Downtown", "200 San Jacinto Blvd", "78701",
                          status="CLOSED_PERMANENTLY")])
    assert v.find_hotel(ROW, c).hotel_status == "CLOSED_PERMANENTLY"


def test_rebrand_detected():
    c = FakeClient(by_name=[place("Starbucks", "500 Congress Ave", "78701", types=("cafe",))],
                   at_address=[place("Holiday Inn Express Austin Downtown", "200 San Jacinto Blvd", "78701")])
    r = v.find_hotel(ROW, c)
    assert r.hotel_status == "NAME_CHANGED" and "Holiday Inn" in r.match_name


def test_rebrand_with_shared_location_words():
    c = FakeClient(by_name=[place("Holiday Inn Express Austin Downtown", "200 San Jacinto Blvd", "78701")],
                   at_address=[place("Holiday Inn Express Austin Downtown", "200 San Jacinto Blvd", "78701")])
    assert v.find_hotel(ROW, c).hotel_status == "NAME_CHANGED"


def test_moved():
    c = FakeClient([place("Hampton Inn & Suites Austin Downtown", "1700 Elm Rd", "78745", phone="512-555-0100")])
    assert v.find_hotel(ROW, c).hotel_status == "ADDRESS_MISMATCH"


def test_not_found():
    assert v.find_hotel(ROW, FakeClient()).hotel_status == "NOT_FOUND"


def test_missing_url_gets_google_suggestion():
    c = FakeClient([place("Hampton Inn & Suites Austin Downtown", "200 San Jacinto Blvd", "78701",
                          website="https://www.hilton.com/en/hotels/ausdthx-hampton/")])
    r = v.validate_row(ROW, FakeSession(), client=c)
    assert r.url_status == "MISSING" and r.suggested_url.startswith("https://www.hilton.com")
    assert "Add URL" in r.action


# --- URL checks -----------------------------------------------------------
def test_url_ok():
    s = FakeSession(FakeResp(200, "<title>Hampton Inn &amp; Suites Austin Downtown | Hilton</title>"))
    assert check_url("example.com", ROW.name, s).status == "OK"


def test_url_broken_parked_blocked_unreachable():
    import requests
    assert check_url("example.com", ROW.name, FakeSession(FakeResp(404))).status == "BROKEN"
    assert check_url("example.com", ROW.name, FakeSession(FakeResp(403))).status == "BLOCKED"
    parked = FakeResp(200, "<title>hamptonaustin.com</title> This domain is for sale!")
    assert check_url("hamptonaustin.com", ROW.name, FakeSession(parked)).status == "PARKED"
    dns = requests.exceptions.ConnectionError("NameResolutionError")
    r = check_url("nope.example", ROW.name, FakeSession(exc=dns))
    assert r.status == "UNREACHABLE" and "DNS" in r.notes


def test_url_content_mismatch_and_redirect():
    other = FakeResp(200, "<title>Joe's Pizza</title>", "https://example.com/")
    assert check_url("example.com", ROW.name, FakeSession(other)).status == "CONTENT_MISMATCH"
    generic = FakeResp(200, "<title>Example Domain</title><p>This domain is for use in examples.</p>")
    assert check_url("example.com", "Example Lodge", FakeSession(generic)).status == "CONTENT_MISMATCH"
    footer = FakeResp(200, "<title>Hotel Van Zandt | Austin</title><footer>A Kimpton hotel</footer>",
                    "https://www.hotelvanzandt.com/")
    assert check_url("hotelvanzandt.com", "Kimpton Hotel Van Zandt", FakeSession(footer)).status == "OK"
    moved = FakeResp(200, f"<title>{ROW.name}</title>", "https://newsite.com/")
    assert check_url("oldsite.com", ROW.name, FakeSession(moved)).status == "REDIRECTED"


def test_same_site_brand_paths():
    assert same_site("https://hilton.com/en/hotels/ausdthx-hampton/", "https://www.hilton.com/en/hotels/ausdthx-hampton/x")
    assert not same_site("https://hilton.com/en/hotels/ausdthx-hampton/", "https://www.hilton.com/en/hotels/dalxyz/")
    assert same_site("http://www.myinn.com/rooms", "https://myinn.com/")


# --- multi-pass pipeline -------------------------------------------------
HOTEL_PAGE = f"<title>{ROW.name}</title><p>200 San Jacinto Boulevard, Austin TX</p><a href='tel:5125550100'>call</a>"


def test_website_pass_confirms_without_google():
    row = v.HotelRow(**{**ROW.__dict__, "url": "hamptonaustin.com"})
    c = FakeClient([place(ROW.name, "200 San Jacinto Blvd", "78701")])
    r = v.validate_row(row, FakeSession(FakeResp(200, HOTEL_PAGE, "https://hamptonaustin.com/")), client=c)
    assert (r.hotel_status, r.verified_by, r.confidence) == ("VERIFIED", "website", "HIGH")
    assert c.calls == 0 and r.url_shows_address == "YES"


def test_website_without_address_or_phone_is_not_enough():
    row = v.HotelRow(**{**ROW.__dict__, "url": "hamptonaustin.com"})
    page = FakeResp(200, f"<title>{ROW.name}</title><p>Book now!</p>", "https://hamptonaustin.com/")
    r = v.validate_row(row, FakeSession(page))
    assert r.hotel_status == "UNRESOLVED" and r.url_status == "OK" and r.url_shows_address == "NO"


def test_overture_pass_confirms_without_google():
    c = FakeClient()
    o = FakeOverture([place("Hampton Inn & Suites Austin-Downtown", "200 San Jacinto Blvd", "78701",
                            website="https://www.hilton.com/en/hotels/ausdthx/")])
    r = v.validate_row(ROW, FakeSession(), overture=o, client=c)
    assert (r.hotel_status, r.verified_by, r.confidence) == ("VERIFIED", "overture", "HIGH")
    assert c.calls == 0 and r.suggested_url.startswith("https://www.hilton.com")


def test_overture_never_condemns_goes_to_google():
    """Closed, low-confidence or different-name Overture records must fall through to Google."""
    for o in (FakeOverture([place(ROW.name, "200 San Jacinto Blvd", "78701", status="CLOSED_PERMANENTLY")]),
              FakeOverture([place(ROW.name, "200 San Jacinto Blvd", "78701")], confidence=0.3),
              FakeOverture([place("Holiday Inn Express Austin Downtown", "200 San Jacinto Blvd", "78701")])):
        c = FakeClient([place(ROW.name, "200 San Jacinto Blvd", "78701")])
        r = v.validate_row(ROW, FakeSession(), overture=o, client=c)
        assert r.verified_by == "google" and c.calls >= 1


def test_overture_ambiguous_address_goes_to_google():
    o = FakeOverture([place(ROW.name, "200 San Jacinto Blvd", "78701"),
                      place("Holiday Inn Express Austin Downtown", "200 San Jacinto Blvd", "78701")])
    assert v.overture_confirm(ROW, o) is None


def test_no_google_leaves_unresolved():
    r = v.validate_row(ROW, FakeSession(), overture=FakeOverture([]))
    assert r.hotel_status == "UNRESOLVED" and "Google" in r.action


def test_overture_website_filter():
    from hotel_validator.overture import _own_site
    assert _own_site("https://www.loewshotels.com/boston-hotel")
    assert _own_site("https://www.hotelx.com/")
    assert not _own_site("https://www.hotels.com/ho123") and not _own_site("https://linktr.ee/thedriskill")
    assert not _own_site("https://m.facebook.com/somehotel")


# --- Google cache (yearly runs must not reuse last year's answers) -------
def _client(tmp_path, **kw):
    from hotel_validator.places import PlacesClient
    c = PlacesClient("k", cache_path=str(tmp_path / "c.sqlite"), **kw)
    c._post = lambda body: (setattr(c, "calls", c.calls + 1), {"places": []})[1]
    return c


def test_cache_reused_within_a_run_but_expires(tmp_path):
    import time
    c = _client(tmp_path)
    c.search("Hotel A"); c.search("Hotel A")
    assert c.calls == 1 and c.is_cached("Hotel A")
    c._db.execute("UPDATE cache SET ts = ?", (time.time() - 40 * 86400,)); c._db.commit()
    c2 = _client(tmp_path)
    assert not c2.is_cached("Hotel A")
    c2.search("Hotel A")
    assert c2.calls == 1


def test_cache_separate_per_tier(tmp_path):
    _client(tmp_path, tier="pro").search("Hotel A")
    assert not _client(tmp_path, tier="enterprise").is_cached("Hotel A")
    assert _client(tmp_path, tier="pro").is_cached("Hotel A")


def test_travelpets_export_headers():
    df = pd.DataFrame(columns=["id_hotel", "hotelName", "address", "postal", "phone", "city", "id_country",
                               "id_region", "webAddress"])
    assert map_columns(df) == {"name": "hotelName", "address": "address", "state": "id_region", "zip": "postal",
                               "phone": "phone", "city": "city", "url": "webAddress"}


def test_report_strips_characters_excel_rejects(tmp_path):
    from hotel_validator.report import write_report
    row = {**v.Result(hotel_status="VERIFIED", url_status="OK", url_notes="title \x1b\x00 here").as_dict()}
    path = tmp_path / "out.xlsx"
    write_report(pd.DataFrame([row]), str(path), 0)
    assert pd.read_excel(path, sheet_name="Results")["url_notes"][0] == "title  here"


def test_one_bad_row_does_not_stop_the_run():
    from validate_hotels import run_parallel

    def fn(i):
        if i == 2:
            raise UnicodeDecodeError("utf-8", b"", 0, 1, "bad")
        return i * 10
    assert run_parallel(fn, [1, 2, 3], 2, "t", on_error=lambda e: "failed") == {1: 10, 2: "failed", 3: 30}


def test_redirect_to_domain_marketplace_is_parked_not_blocked():
    sold = FakeResp(403, "", url="https://www.hugedomains.com/domain_profile.cfm?d=oldhotel.com")
    assert check_url("oldhotel.com", ROW.name, FakeSession(sold)).status == "PARKED"


# --- pass 2b: the hotel's website as listed in Overture ----------------------
class ExplodingSession:
    def get(self, url, **kw):
        raise AssertionError(f"should not fetch {url}")


def _overture_with_site(name=ROW.name, street="200 San Jacinto Blvd", phone="(512) 555-9999", **kw):
    # different phone, so pass 2 (Overture record alone) rejects it and pass 2b decides
    return FakeOverture([place(name, street, "78701", phone=phone, website="https://hamptonaustin.com/", **kw)])


def test_overture_website_confirms_hotel_with_no_url_on_file():
    c = FakeClient()
    r = v.validate_row(ROW, FakeSession(FakeResp(200, HOTEL_PAGE, "https://hamptonaustin.com/")),
                       overture=_overture_with_site(), client=c)
    assert (r.hotel_status, r.verified_by, r.confidence) == ("VERIFIED", "overture_website", "HIGH")
    assert c.calls == 0 and r.suggested_url == "https://hamptonaustin.com/" and r.url_status == "MISSING"
    assert "see suggested_url" in r.action


def test_overture_website_that_names_hotel_without_address_is_only_suggested():
    page = FakeResp(200, f"<title>{ROW.name}</title><p>Book now!</p>", "https://hamptonaustin.com/")
    r = v.validate_row(ROW, FakeSession(page), overture=_overture_with_site())
    assert r.hotel_status == "UNRESOLVED" and r.suggested_url == "https://hamptonaustin.com/"
    assert "address not shown" in r.url_notes


def test_overture_website_skipped_for_rebrands_and_closures():
    rival = _overture_with_site()
    rival.places.append(place("Holiday Inn Express Austin Downtown", "200 San Jacinto Blvd", "78701"))
    closed = _overture_with_site(status="CLOSED_PERMANENTLY")
    for o in (rival, closed):
        r = v.validate_row(ROW, ExplodingSession(), overture=o)
        assert r.hotel_status == "UNRESOLVED" and not r.suggested_url


def test_overture_website_not_rechecked_when_same_as_url_on_file():
    row = v.HotelRow(**{**ROW.__dict__, "url": "hamptonaustin.com"})
    page = FakeResp(200, f"<title>{ROW.name}</title><p>Book now!</p>", "https://hamptonaustin.com/")

    class CountingSession(FakeSession):
        n = 0

        def get(self, url, **kw):
            CountingSession.n += 1
            return super().get(url, **kw)
    r = v.validate_row(row, CountingSession(page), overture=_overture_with_site())
    assert CountingSession.n == 1 and r.hotel_status == "UNRESOLVED"


# --- overture_hint: free leads on rebrands / closures --------------------
def test_hint_possible_rebrand_when_only_another_hotel_at_address():
    o = FakeOverture([place("Ramada by Wyndham Austin", "200 San Jacinto Blvd", "78701")])
    r = v.validate_row(ROW, FakeSession(), overture=o)
    assert r.hotel_status == "UNRESOLVED"  # a hint is never a verdict
    assert r.overture_hint.startswith("Possible rebrand") and "Ramada by Wyndham Austin" in r.overture_hint
    assert "overture_hint" in r.action


def test_hint_both_hotels_and_closed():
    both = FakeOverture([place(ROW.name, "200 San Jacinto Blvd", "78701", phone="(512) 555-9999"),
                         place("Ramada by Wyndham Austin", "200 San Jacinto Blvd", "78701")])
    assert v.validate_row(ROW, FakeSession(), overture=both).overture_hint.startswith("Overture lists this hotel and also")
    closed = FakeOverture([place(ROW.name, "200 San Jacinto Blvd", "78701", status="CLOSED_PERMANENTLY")])
    assert v.validate_row(ROW, FakeSession(), overture=closed).overture_hint == "Overture marks this hotel permanently closed"
    assert v.validate_row(ROW, FakeSession(), overture=FakeOverture([])).overture_hint == ""


def test_plural_names_match():
    assert name_score("Adirondack Efficiency Hotel", "Adirondack Efficiencies") >= 80
    assert name_score("Lakeside Cottages", "Lakeside Cottage") >= 80
    assert name_score("Hampton Inn Austin", "Motel 6 Austin") < 60

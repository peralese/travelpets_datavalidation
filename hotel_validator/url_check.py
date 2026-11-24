"""Check that a hotel's website still loads and still belongs to that hotel."""

import re
import warnings
from dataclasses import dataclass
from html import unescape
from urllib.parse import urlparse

import requests
from .matching import (core_words, name_score, normalize_street, phone_digits, street_line,
                       street_number, token_coverage)

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
PARKED_MARKERS = [
    "this domain is for sale", "buy this domain", "domain may be for sale", "is for sale!",
    "parked free", "domain parking", "this domain has expired", "sedoparking", "hugedomains",
    "dan.com", "afternic", "godaddy.com/domains", "the domain name", "parkingcrew",
]
# Hotel brand sites serve one hotel per sub-path; domain matching on these is meaningless alone.
SHARED_HOSTS = {"hilton.com", "marriott.com", "ihg.com", "hyatt.com", "wyndhamhotels.com", "choicehotels.com",
                "bestwestern.com", "radissonhotels.com", "redlion.com", "laquinta.com", "motel6.com",
                "extendedstayamerica.com", "sonesta.com", "accor.com", "all.accor.com", "omnihotels.com",
                "loewshotels.com", "kimptonhotels.com", "redroof.com", "druryhotels.com"}


@dataclass
class UrlResult:
    status: str          # OK, REDIRECTED, CONTENT_MISMATCH, PARKED, BROKEN, UNREACHABLE, BLOCKED, MISSING, INVALID
    http_code: str = ""
    final_url: str = ""
    notes: str = ""
    address_on_page: bool = False   # page shows the listed street address
    phone_on_page: bool = False     # page shows the listed phone number

    @property
    def confirms_hotel(self) -> bool:
        """Page loads, is about this hotel, and shows its address or phone: good enough to skip paid lookups."""
        return self.status in ("OK", "REDIRECTED") and (self.address_on_page or self.phone_on_page)


def normalize_url(url: str) -> str:
    url = url.strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "http://" + url.lstrip("/")
    return url


def registrable_domain(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    parts = host.split(".")
    # good enough for .com/.net/.us etc.; keeps e.g. "hotel.co.uk" intact
    if len(parts) > 2 and len(parts[-2]) <= 3 and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def same_site(a: str, b: str) -> bool:
    """True when two URLs point at the same hotel site (path-aware for brand sites)."""
    if not a or not b:
        return False
    da, db = registrable_domain(a), registrable_domain(b)
    if da != db:
        return False
    if da in SHARED_HOSTS:
        # e.g. hilton.com/en/hotels/austxhx-hampton.../ - compare the hotel code segment
        pa = [s for s in urlparse(a).path.lower().split("/") if s]
        pb = [s for s in urlparse(b).path.lower().split("/") if s]
        return bool(set(pa) & set(pb) - {"en", "en-us", "hotels", "hotel", "us", "travel"})
    return True


def _page_text(html: str) -> tuple[str, str]:
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = unescape(re.sub(r"\s+", " ", title.group(1))).strip() if title else ""
    meta = " ".join(re.findall(r'<meta[^>]+(?:og:title|og:site_name|description)[^>]+content="([^"]*)"', html, re.I))
    body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html[:400_000], flags=re.I | re.S)
    body = unescape(re.sub(r"<[^>]+>", " ", body))
    body = re.sub(r"\s+", " ", body).lower()
    return title, f"{title} {unescape(meta)} {body}".lower()


def check_url(url: str, hotel_name: str, session: requests.Session | None = None, timeout: int = 20,
              street: str = "", phone: str = "") -> UrlResult:
    url = normalize_url(url)
    if not url:
        return UrlResult("MISSING", notes="No URL on file")
    if not urlparse(url).hostname or "." not in urlparse(url).hostname:
        return UrlResult("INVALID", notes=f"Not a valid URL: {url}")

    session = session or requests.Session()
    resp, err = None, ""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for verify in (True, False):  # retry without TLS verification so an expired cert isn't "down"
            try:
                resp = session.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True, verify=verify)
                if not verify:
                    err = "SSL certificate invalid"
                break
            except requests.exceptions.SSLError as e:
                err = f"SSL error: {e.__class__.__name__}"
            except requests.exceptions.RequestException as e:
                err = _short_error(e)
                break

    if resp is None:
        return UrlResult("UNREACHABLE", notes=err)

    code, final = resp.status_code, resp.url
    notes = [err] if err else []
    if code in (401, 403, 429, 503) or "cf-chl" in resp.text[:5000]:
        # Many hotel / brand sites block scripts (Cloudflare, Akamai) - not proof the site is down.
        return UrlResult("BLOCKED", str(code), final, "; ".join(notes + ["Site blocked automated check - verify manually"]))
    if code >= 400:
        return UrlResult("BROKEN", str(code), final, "; ".join(notes + [f"HTTP {code}"]))

    title, text = _page_text(resp.text)
    if any(m in text[:20000] for m in PARKED_MARKERS) and name_score(hotel_name, title) < 80:
        return UrlResult("PARKED", str(code), final, "; ".join(notes + ["Domain appears parked / for sale"]))

    redirected = registrable_domain(url) != registrable_domain(final)
    if redirected:
        notes.append(f"Redirects to different site: {registrable_domain(final)}")

    if _mentions(hotel_name, title, text):
        return UrlResult("REDIRECTED" if redirected else "OK", str(code), final, "; ".join(notes),
                         address_on_page=address_on_page(street, text), phone_on_page=phone_on_page(phone, resp.text))
    notes.append(f"Page does not mention hotel name (title: '{title[:80]}')")
    return UrlResult("CONTENT_MISMATCH", str(code), final, "; ".join(notes))


def _mentions(hotel_name: str, title: str, text: str) -> bool:
    """Does the page look like it belongs to this hotel?"""
    if not hotel_name:
        return True
    if name_score(hotel_name, title) >= 80:
        return True
    # Otherwise most of the distinctive words of the name must appear somewhere on the page
    # (e.g. "Kimpton" in the footer, "Van Zandt" in the title).
    words = core_words(hotel_name)
    return bool(words) and token_coverage(words, " ".join(re.findall(r"[a-z0-9']+", text[:200_000]))) >= 0.66


DIRECTIONS = {"n", "s", "e", "w", "ne", "nw", "se", "sw"}


def address_on_page(street: str, page_text: str) -> bool:
    """True if the page shows the street number followed by the street name, e.g. '604 Brazos'."""
    line = normalize_street(street_line(street))
    num = street_number(line)
    words = [w for w in line.split()[1:] if w not in DIRECTIONS]
    if not num or not words:
        return False
    page = normalize_street(page_text)
    if line in page:
        return True
    # tolerate "604 Brazos Street" vs "604 Brazos St." and "200 North Lamar" vs "200 N Lamar"
    return re.search(rf"\b{re.escape(num)} (?:(?:{'|'.join(DIRECTIONS)}) )?{re.escape(words[0])}\b", page) is not None


PHONE_RE = re.compile(r"(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}")


def phone_on_page(phone: str, html: str) -> bool:
    """True if the listed phone appears anywhere in the page source (including tel: links)."""
    want = phone_digits(phone)
    if len(want) < 10:
        return False
    return any(phone_digits(m)[-10:] == want[-10:] for m in PHONE_RE.findall(html[:400_000]))


def _short_error(e: Exception) -> str:
    msg = str(e)
    if "Name or service not known" in msg or "NameResolutionError" in msg or "getaddrinfo" in msg:
        return "Domain does not resolve (DNS)"
    if isinstance(e, requests.exceptions.Timeout):
        return "Timed out"
    if isinstance(e, requests.exceptions.ConnectionError):
        return "Connection refused/failed"
    if isinstance(e, requests.exceptions.TooManyRedirects):
        return "Redirect loop"
    return e.__class__.__name__

"""Fuzzy comparison of hotel names, street addresses and phone numbers."""

import re

from rapidfuzz import fuzz

# Words that carry no identifying signal in hotel names.
NAME_NOISE = {"the", "hotel", "hotels", "inn", "inns", "and", "by", "at", "a", "an", "of", "resort", "suites", "&",
              "motel", "motor"}
# Location descriptors shared by unrelated hotels near each other ("... Downtown", "... Airport").
# Ignoring them means a rebrand at the same address can't pass on location words alone.
LOCATION_NOISE = {"downtown", "airport", "north", "south", "east", "west", "northeast", "northwest",
                  "southeast", "southwest", "n", "s", "e", "w", "central", "center", "centre", "city",
                  "convention", "midtown", "uptown", "near", "mall", "area", "i", "interstate", "hwy",
                  "highway", "beach", "university", "medical", "district", "historic", "tx", "usa"}

STREET_ABBREV = {
    "street": "st", "avenue": "ave", "av": "ave", "boulevard": "blvd", "road": "rd", "drive": "dr",
    "lane": "ln", "court": "ct", "place": "pl", "parkway": "pkwy", "highway": "hwy", "hiway": "hwy",
    "freeway": "fwy", "expressway": "expy", "circle": "cir", "terrace": "ter", "trail": "trl",
    "square": "sq", "plaza": "plz", "suite": "ste", "north": "n", "south": "s", "east": "e",
    "west": "w", "northeast": "ne", "northwest": "nw", "southeast": "se", "southwest": "sw",
    "route": "rte", "mount": "mt", "fort": "ft", "saint": "st", "center": "ctr", "centre": "ctr",
    "first": "1st", "second": "2nd", "third": "3rd", "fourth": "4th", "fifth": "5th",
}


def name_score(ours: str, theirs: str, ignore: str = "") -> int:
    """0-100 similarity, tolerant of word order and extra words ("Downtown/Convention Center").

    Filler words, location descriptors and the city/state in `ignore` are removed first, so
    "Hampton Inn Austin" vs "Motel 6 Austin" isn't scored as similar just because both mention Austin.
    """
    if not ours or not theirs:
        return 0
    a, b = core_words(ours, ignore), core_words(theirs, ignore)
    if bool(a) != bool(b):
        # One name is just "<city> Inn" and the other has real words ("Florence Villager Lodge" vs
        # "Florence Inn and Suites"): the city is all they share, which isn't evidence of the same hotel.
        return min(50, int(round(fuzz.token_set_ratio(core_words(ours), core_words(theirs)))))
    if not a:
        # Both names are just "<city> Inn" etc. Keep the city words, since they're all there is to compare.
        a, b = core_words(ours), core_words(theirs)
        if not a or not b:
            return int(round(fuzz.ratio(ours.lower(), theirs.lower())))
    a, b = _join_split_words(a, b), _join_split_words(b, a)
    short, long_ = sorted((a, b), key=lambda x: len(x.split()))
    # token_set_ratio alone scores >=70 whenever two names share one word ("Example Lodge" vs
    # "Example Domain"), so also require that the shorter name's words are found in the longer
    # one, and that they make up a fair share of it ("Marriott" vs "Courtyard by Marriott" = 67).
    return int(round(min(fuzz.token_set_ratio(a, b),
                         100 * token_coverage(short, long_),
                         50 + 50 * token_coverage(long_, short))))


def same_brand(ours: str, theirs: str, ignore: str = "") -> bool:
    """Both names start with the same brand words, e.g. "La Quinta Inn Orange County" and
    "La Quinta Inn & Suites by Wyndham Irvine Spectrum". Only meaningful at the same address."""
    a, b = core_words(ours, ignore).split(), core_words(theirs, ignore).split()
    k = min(2, len(a), len(b))
    return k >= 1 and a[:k] == b[:k] and len("".join(a[:k])) >= 4


def _join_split_words(text: str, other: str) -> str:
    """Join adjacent words that the other name writes as one ("old town" vs "Oldtown")."""
    words, joined = text.split(), set(other.split())
    out, i = [], 0
    while i < len(words):
        if i + 1 < len(words) and words[i] + words[i + 1] in joined:
            out.append(words[i] + words[i + 1])
            i += 2
        else:
            out.append(words[i])
            i += 1
    return " ".join(out)


def token_coverage(needle: str, haystack: str) -> float:
    """Fraction of words in `needle` that appear (allowing small typos) among words of `haystack`."""
    words, pool = needle.split(), set(haystack.split())
    if not words:
        return 0.0
    hit = sum(1 for w in words if w in pool or (len(w) > 4 and any(fuzz.ratio(w, x) >= 85 for x in pool)))
    return hit / len(words)


def core_words(name: str, ignore: str = "") -> str:
    skip = NAME_NOISE | LOCATION_NOISE | set(re.findall(r"[a-z0-9']+", ignore.lower()))
    return " ".join(_singular(w) for w in re.findall(r"[a-z0-9']+", name.lower()) if w not in skip)


def _singular(word: str) -> str:
    """Crude plural folding so "Efficiencies" matches "Efficiency" and "Cottages" matches "Cottage"."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def normalize_street(street: str) -> str:
    words = re.findall(r"[a-z0-9]+", street.lower().replace("#", " ste "))
    return " ".join(STREET_ABBREV.get(w, w) for w in words)


def street_number(street: str) -> str:
    m = re.match(r"\s*(\d+[a-z]?)(?:-\d+)?\b", street.lower())
    return m.group(1) if m else ""


def street_line(address: str) -> str:
    """First line of an address: '123 Main St, Austin, TX' -> '123 Main St'."""
    return address.split(",")[0].strip()


def zip5(text: str) -> str:
    m = re.search(r"\b(\d{5})(?:-\d{4})?\b", text or "")
    return m.group(1) if m else ""


def address_match(our_street: str, our_zip: str, g_street: str, g_zip: str) -> tuple[str, int]:
    """Compare two US addresses.

    Returns (verdict, street_similarity) where verdict is MATCH, PARTIAL or MISMATCH.
    Street number must agree for a MATCH; the zip and street name break ties.
    """
    ours, theirs = normalize_street(street_line(our_street)), normalize_street(g_street)
    if not ours or not theirs:
        return "MISMATCH", 0
    sim = int(round(fuzz.token_set_ratio(ours, theirs)))
    num_a, num_b = street_number(ours), street_number(theirs)
    zip_ok = bool(our_zip) and our_zip[:5] == (g_zip or "")[:5]
    if num_a and num_b and num_a == num_b and (sim >= 75 or (zip_ok and sim >= 60)):
        return "MATCH", sim
    if num_a and num_b and num_a != num_b:
        # Same street, different number: hotels on a corner often have two valid addresses.
        return ("PARTIAL" if zip_ok and sim >= 60 else "MISMATCH"), sim
    if sim >= 85 and zip_ok:
        return "MATCH", sim
    if zip_ok:
        return "PARTIAL", sim
    return "MISMATCH", sim


def phone_digits(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return digits[1:] if len(digits) == 11 and digits.startswith("1") else digits


def phone_match(ours: str, theirs: str) -> str:
    a, b = phone_digits(ours), phone_digits(theirs)
    if not a:
        return "NO_PHONE"
    if not b:
        return "UNKNOWN"
    return "MATCH" if a[-10:] == b[-10:] else "MISMATCH"

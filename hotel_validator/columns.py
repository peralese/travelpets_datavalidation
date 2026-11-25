"""Locate the hotel fields in an extract whose headers may vary slightly."""

import re

import pandas as pd

# Canonical field -> accepted header spellings (compared after normalising).
ALIASES = {
    "id": ["id_hotel", "hotel id", "hotel_id", "hotelid", "id"],
    "name": ["hotel name", "hotel", "name", "property", "property name", "hotelname"],
    "address": ["address", "street", "street address", "address1", "address 1", "addr"],
    "city": ["city", "town"],
    "state": ["state", "st", "province", "state code", "id_region", "region"],
    "zip": ["zip", "zip code", "zipcode", "postal code", "postal", "postcode", "zip_code"],
    "phone": ["phone", "phone number", "telephone", "tel", "phone_number"],
    "url": ["url", "website", "web site", "web", "site", "website url", "web address", "webaddress"],
}
REQUIRED = ["name", "address", "state"]


def _norm(header) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(header).lower()).strip()


def map_columns(df: pd.DataFrame, overrides: dict | None = None) -> dict:
    """Return {canonical field: actual column name} for the columns found."""
    overrides = {k: v for k, v in (overrides or {}).items() if v}
    by_norm = {_norm(c): c for c in df.columns}
    mapping = {}
    for field, aliases in ALIASES.items():
        if field in overrides:
            if overrides[field] not in df.columns:
                raise ValueError(f"Column '{overrides[field]}' (for {field}) not found in sheet. "
                                 f"Available: {list(df.columns)}")
            mapping[field] = overrides[field]
            continue
        for alias in aliases:
            if _norm(alias) in by_norm:
                mapping[field] = by_norm[_norm(alias)]
                break
    missing = [f for f in REQUIRED if f not in mapping]
    if missing:
        raise ValueError(f"Could not find column(s) for {missing}. Available: {list(df.columns)}. "
                         f"Use --col-{missing[0]} to name the column explicitly.")
    return mapping


def clean(value) -> str:
    """Excel cells come back as NaN/float/int; turn everything into a trimmed string."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "null", "n/a", "na", "-"} else text


def clean_zip(value) -> str:
    """Restore leading zeros Excel strips from New England zips (e.g. 2134 -> 02134)."""
    text = clean(value)
    if text.isdigit() and len(text) < 5:
        text = text.zfill(5)
    return text


def merge_duplicate_ids(df: pd.DataFrame, id_col: str) -> tuple[pd.DataFrame, int]:
    """Collapse rows that share an ID into one, keeping every non-empty value.

    The TravelPets export lists each hotel that has a pet restriction twice (once with it, once
    without). Different non-empty values for the same ID are kept, joined with " | ".
    Rows without an ID are left alone. Returns (merged frame, number of rows removed).
    """
    ids = df[id_col].map(clean)
    if not ids.duplicated().any() or ids.eq("").all():
        return df, 0

    def combine(values):
        seen = list(dict.fromkeys(clean(v) for v in values if clean(v)))
        return " | ".join(seen) if seen else None

    has_id = df[ids != ""]
    merged = (has_id.groupby(ids[ids != ""], sort=False).agg(combine).reset_index(drop=True))
    merged[id_col] = list(dict.fromkeys(ids[ids != ""]))
    out = pd.concat([merged, df[ids == ""]], ignore_index=True)[list(df.columns)]
    return out, len(df) - len(out)

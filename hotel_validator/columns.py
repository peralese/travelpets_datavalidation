"""Locate the hotel fields in an extract whose headers may vary slightly."""

import re

import pandas as pd

# Canonical field -> accepted header spellings (compared after normalising).
ALIASES = {
    "name": ["hotel name", "hotel", "name", "property", "property name", "hotelname"],
    "address": ["address", "street", "street address", "address1", "address 1", "addr"],
    "city": ["city", "town"],
    "state": ["state", "st", "province", "state code"],
    "zip": ["zip", "zip code", "zipcode", "postal code", "postal", "postcode", "zip_code"],
    "phone": ["phone", "phone number", "telephone", "tel", "phone_number"],
    "url": ["url", "website", "web site", "web", "site", "website url", "web address"],
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

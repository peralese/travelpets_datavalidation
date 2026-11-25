"""Write the results workbook: original columns + validation columns, colour-coded, with a summary sheet."""

import pandas as pd
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

FILLS = {
    "VERIFIED": "C6EFCE", "OK": "C6EFCE",
    "NAME_CHANGED": "FFEB9C", "ADDRESS_MISMATCH": "FFEB9C", "CLOSED_TEMPORARILY": "FFEB9C",
    "REDIRECTED": "FFEB9C", "CONTENT_MISMATCH": "FFEB9C", "BLOCKED": "FFEB9C", "MISSING": "FFEB9C",
    "CLOSED_PERMANENTLY": "FFC7CE", "NOT_FOUND": "FFC7CE", "BROKEN": "FFC7CE", "UNREACHABLE": "FFC7CE",
    "PARKED": "FFC7CE", "INVALID": "FFC7CE", "ERROR": "D9D9D9", "UNRESOLVED": "D9D9D9",  # ERROR: hotel or URL
}


def _excel_safe(value):
    """Strip control characters (e.g. from scraped page titles) that openpyxl refuses to write."""
    return ILLEGAL_CHARACTERS_RE.sub("", value) if isinstance(value, str) else value


def write_report(df: pd.DataFrame, path: str, places_calls: int) -> None:
    df = df.map(_excel_safe)
    summary = pd.concat([
        df["hotel_status"].value_counts().rename_axis("value").reset_index(name="count").assign(check="Hotel"),
        df["verified_by"].replace("", "(none)").value_counts().rename_axis("value").reset_index(name="count")
          .assign(check="Confirmed by"),
        df["url_status"].value_counts().rename_axis("value").reset_index(name="count").assign(check="URL"),
    ])[["check", "value", "count"]]
    actions = df["action"].value_counts().rename_axis("action").reset_index(name="count")

    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name="Results", index=False)
        summary.to_excel(xw, sheet_name="Summary", index=False)
        actions.to_excel(xw, sheet_name="Summary", index=False, startcol=5)
        pd.DataFrame({"info": [f"Rows checked: {len(df)}", f"Google Places API calls (billable): {places_calls}"]}) \
            .to_excel(xw, sheet_name="Summary", index=False, startcol=9)

        ws = xw.sheets["Results"]
        ws.freeze_panes = "B2"
        ws.auto_filter.ref = ws.dimensions
        cols = list(df.columns)
        for i, col in enumerate(cols, start=1):
            ws.cell(row=1, column=i).font = Font(bold=True)
            width = min(max(len(str(col)), *(len(str(v)) for v in df[col].head(200))) + 2, 60)
            ws.column_dimensions[get_column_letter(i)].width = width
        for col in ("hotel_status", "url_status"):
            idx = cols.index(col) + 1
            for r in range(2, len(df) + 2):
                cell = ws.cell(row=r, column=idx)
                if cell.value in FILLS:
                    cell.fill = PatternFill("solid", fgColor=FILLS[cell.value])
        for col in ("google_maps_url", "match_website", "url_final", "suggested_url"):
            idx = cols.index(col) + 1
            for r in range(2, len(df) + 2):
                cell = ws.cell(row=r, column=idx)
                if cell.value and str(cell.value).startswith("http"):
                    cell.hyperlink, cell.style = str(cell.value), "Hyperlink"

        for name in ("Summary",):
            for col_cells in xw.sheets[name].columns:
                xw.sheets[name].column_dimensions[col_cells[0].column_letter].width = \
                    max(len(str(c.value or "")) for c in col_cells) + 2

"""Google Sheet mirror: one row per listing, kept up to date as statuses change."""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread

from .models import Listing, Status

log = logging.getLogger(__name__)
TZ = ZoneInfo("Europe/Amsterdam")

# Listings that never got an application go to a separate tab to keep the main one readable.
SKIPPED = {Status.FILTERED, Status.LIKELY_SCAM, Status.UNAVAILABLE}
HIDDEN = {Status.DUPLICATE}


def _ts(value: float | None) -> str:
    return datetime.fromtimestamp(value, TZ).strftime("%Y-%m-%d %H:%M") if value else ""


def _d(listing: Listing, attr: str):
    return getattr(listing.details, attr, None) if listing.details else None


COLUMNS: list[tuple[str, Callable[[Listing], object]]] = [
    ("ID", lambda l: l.id),
    ("Found", lambda l: _ts(l.first_seen)),
    ("Status", lambda l: l.status.value),
    ("🚩", lambda l: "🚩" if l.flagged else ""),
    ("📞", lambda l: "CALL" if l.call_needed else ""),
    ("Address", lambda l: l.address),
    ("Area", lambda l: ", ".join(x for x in [l.buurt, l.municipality] if x)),
    ("Rent €/mo", lambda l: l.total_rent),
    ("m²", lambda l: _d(l, "size_m2")),
    ("€/m²", lambda l: round(l.total_rent / _d(l, "size_m2"), 1) if l.total_rent and _d(l, "size_m2") else ""),
    ("Furnished", lambda l: _d(l, "furnished")),
    ("Registration", lambda l: _d(l, "registration_allowed")),
    ("Bike min", lambda l: l.bike_minutes),
    ("Walk min", lambda l: l.walk_minutes),
    ("km to work", lambda l: round(l.distance_km, 1) if l.distance_km is not None else ""),
    ("Area score /10", lambda l: l.neighbourhood_score),
    ("Scam risk /100", lambda l: l.scam_score),
    ("Scam notes", lambda l: "; ".join(l.scam_reasons)),
    ("Apply as", lambda l: l.apply_as),
    ("Why", lambda l: l.apply_reason),
    ("Permit", lambda l: l.permit_note),
    ("Rank", lambda l: l.rank),
    ("Agency", lambda l: l.agency),
    ("Agent", lambda l: l.agent),
    ("Phone", lambda l: l.phone),
    ("Email", lambda l: l.email),
    ("Call reason", lambda l: l.call_reason),
    ("Applied", lambda l: _ts(l.applied_at)),
    ("Method", lambda l: l.apply_method),
    ("Seconds to apply", lambda l: round(l.time_to_apply_s) if l.time_to_apply_s else ""),
    ("Style", lambda l: l.variant),
    ("Viewing", lambda l: l.viewing_at),
    ("Viewing link", lambda l: l.viewing_link),
    ("Last reply", lambda l: l.last_reply),
    ("Skip reasons", lambda l: "; ".join(l.filter_reasons)),
    ("Notes", lambda l: l.notes or l.apply_error),
    ("Message sent", lambda l: l.message),
    ("Source", lambda l: l.source),
    ("Link", lambda l: l.url),
]
VIEWING_COLUMNS = ["Listing ID", "When", "Status", "Address", "Agent", "Phone", "Booking link", "Listing link"]


def _col(n: int) -> str:
    name = ""
    while n:
        n, rem = divmod(n - 1, 26)
        name = chr(65 + rem) + name
    return name


def _clean(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return value
    return str(value)[:5000]


class Sheet:
    def __init__(self, service_account_file: str, sheet_id: str):
        client = gspread.service_account(filename=service_account_file, http_client=gspread.BackOffHTTPClient)
        self.book = client.open_by_key(sheet_id)
        self._lock = threading.Lock()
        self._rows: dict[tuple[str, str], int] = {}
        self.setup()

    def _tab(self, title: str, headers: list[str]) -> gspread.Worksheet:
        try:
            ws = self.book.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = self.book.add_worksheet(title, rows=1000, cols=len(headers))
        if ws.row_values(1) != headers:
            ws.update([headers], "A1")
            ws.freeze(rows=1)
            ws.format("1:1", {"textFormat": {"bold": True}})
        return ws

    def setup(self) -> None:
        headers = [name for name, _ in COLUMNS]
        self.listings = self._tab("Listings", headers)
        self.skipped = self._tab("Skipped", headers)
        self.viewings = self._tab("Viewings", VIEWING_COLUMNS)
        for ws in (self.listings, self.skipped, self.viewings):
            for row, value in enumerate(ws.col_values(1)[1:], start=2):
                if value:
                    self._rows[(ws.title, value)] = row
        if not self._has_rules():
            self._add_highlighting()

    def _has_rules(self) -> bool:
        meta = self.book.fetch_sheet_metadata({"fields": "sheets(properties.sheetId,conditionalFormats)"})
        return any(s.get("conditionalFormats") for s in meta.get("sheets", [])
                   if s["properties"]["sheetId"] == self.listings.id)

    def _add_highlighting(self) -> None:
        rules = [
            ('=$C2="VIEWING_BOOKED"', {"red": 0.96, "green": 0.6, "blue": 0.6}),
            ('=OR($C2="VIEWING_INVITED",$C2="DOCS_REQUESTED",$C2="NEEDS_REPLY")', {"red": 1, "green": 0.8, "blue": 0.5}),
            ('=$E2="CALL"', {"red": 1, "green": 0.95, "blue": 0.55}),
            ('=$C2="MANUAL_APPLY"', {"red": 0.8, "green": 0.88, "blue": 1}),
        ]
        requests = [{"addConditionalFormatRule": {"index": i, "rule": {
            "ranges": [{"sheetId": self.listings.id, "startRowIndex": 1, "startColumnIndex": 0,
                        "endColumnIndex": len(COLUMNS)}],
            "booleanRule": {"condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": formula}]},
                            "format": {"backgroundColor": color}}}}} for i, (formula, color) in enumerate(rules)]
        self.book.batch_update({"requests": requests})

    def _upsert(self, ws: gspread.Worksheet, key: str, values: list) -> None:
        row = self._rows.get((ws.title, key))
        if row:
            ws.update([values], f"A{row}:{_col(len(values))}{row}")
            return
        response = ws.append_row(values, value_input_option="RAW", insert_data_option="INSERT_ROWS")
        match = re.search(r"![A-Z]+(\d+)", response.get("updates", {}).get("updatedRange", ""))
        if match:
            self._rows[(ws.title, key)] = int(match.group(1))

    def upsert(self, listing: Listing) -> None:
        if listing.status in HIDDEN:
            return
        values = [_clean(get(listing)) for _, get in COLUMNS]
        with self._lock:
            if (self.listings.title, listing.id) in self._rows:
                ws = self.listings
            elif (self.skipped.title, listing.id) in self._rows:
                ws = self.skipped
            else:
                ws = self.skipped if listing.status in SKIPPED else self.listings
            self._upsert(ws, listing.id, values)
            if listing.viewing_at or listing.status in (Status.VIEWING_INVITED, Status.VIEWING_BOOKED):
                self._upsert(self.viewings, listing.id, [
                    listing.id, listing.viewing_at, listing.status.value, listing.address, listing.agent,
                    listing.phone, listing.viewing_link, listing.url])

    def write_stats(self, rows: list[list]) -> None:
        with self._lock:
            try:
                ws = self.book.worksheet("Stats")
                ws.clear()
            except gspread.WorksheetNotFound:
                ws = self.book.add_worksheet("Stats", rows=200, cols=8)
            ws.update([[_clean(v) for v in row] for row in rows], "A1")

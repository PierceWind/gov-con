"""Read the GovCon tracker sheet and match its notice IDs to opportunities.

The sheet is read through Google's CSV export endpoint so the dashboard can
refresh its local cache without storing a Google password or OAuth token.
Write-back remains a separate, authenticated operation.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import find_opportunities as finder

ROOT = finder.ROOT
CACHE = ROOT / "data" / "tracker_rows.json"
DEFAULT_SPREADSHEET_ID = "1YVfQpp5ZcD9WnHueAucCZyvPRlch6ortHCj-TQhONdM"
DEFAULT_GID = "513000975"
DEFAULT_TTL = 300


def normalize_id(value) -> str:
    """Normalize IDs so punctuation/case differences do not create duplicates."""
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _sheet_csv_url() -> str:
    explicit = finder.env_value("GOOGLE_SHEETS_TRACKER_CSV_URL")
    if explicit:
        return explicit
    sheet_id = finder.env_value("GOOGLE_SHEETS_TRACKER_SPREADSHEET_ID") or DEFAULT_SPREADSHEET_ID
    gid = finder.env_value("GOOGLE_SHEETS_TRACKER_GID") or DEFAULT_GID
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={urllib.parse.quote(gid)}"


def _read_remote_rows() -> list[dict]:
    request = urllib.request.Request(_sheet_csv_url(), headers={
        "Accept": "text/csv",
        "Connection": "close",
        "User-Agent": "gov-con tracker sync/1.0",
    })
    with urllib.request.urlopen(request, timeout=30) as response:
        text = response.read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    rows = [dict(row) for row in reader]
    headers = {str(x).strip().upper() for x in (reader.fieldnames or [])}
    if rows and not headers.intersection({"NOTICE ID", "SOLICITATION #", "SOLICITATION", "SOLICITATION NUMBER"}):
        raise ValueError("tracker export does not include a NOTICE ID or solicitation column")
    return rows


def _notice_id(row: dict) -> str:
    for key in ("NOTICE ID", "Notice ID", "Solicitation #", "Solicitation", "SOLICITATION NUMBER"):
        if row.get(key):
            return normalize_id(row[key])
    return ""


def _payload(rows: list[dict], error: str | None = None) -> dict:
    ids = {}
    for row in rows:
        notice_id = _notice_id(row)
        if notice_id:
            ids[notice_id] = row
    return {
        "synced_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "count": len(ids),
        "ids": ids,
        "error": error,
    }


def refresh() -> dict:
    """Fetch the tracker tab and save its ID index locally."""
    try:
        rows = _read_remote_rows()
        data = _payload(rows)
        CACHE.parent.mkdir(exist_ok=True)
        CACHE.write_text(json.dumps(data, indent=1), encoding="utf-8")
        return data
    except (OSError, urllib.error.URLError, UnicodeError, ValueError, csv.Error) as exc:
        current = load(refresh_if_stale=False)
        current["error"] = f"Tracker sync failed: {exc}"
        return current


def load(refresh_if_stale: bool = True) -> dict:
    """Load the cached tracker and refresh it when it is missing or stale."""
    data = {}
    if CACHE.exists():
        try:
            data = json.loads(CACHE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
    synced = data.get("synced_at")
    stale = True
    if synced:
        try:
            stale = time.time() - dt.datetime.fromisoformat(synced).timestamp() > DEFAULT_TTL
        except ValueError:
            stale = True
    if refresh_if_stale and (not data or stale):
        return refresh()
    return {"synced_at": synced, "count": len(data.get("ids") or {}),
            "ids": data.get("ids") or {}, "error": data.get("error")}


def opportunity_ids(row: dict) -> set[str]:
    """Return all identifiers that may match the tracker Notice ID column."""
    values = set()
    for key in ("Solicitation #", "Notice ID", "source_id", "opp_key"):
        value = normalize_id(row.get(key))
        if value:
            values.add(value)
    return values


def matched_tracker_row(row: dict, index: dict) -> dict | None:
    for notice_id in opportunity_ids(row):
        if notice_id in index:
            return index[notice_id]
    return None


def status_for(row: dict, index: dict) -> str:
    tracker_row = matched_tracker_row(row, index)
    if tracker_row is None:
        return "Not in tracker"
    for key in ("STATUS", "Status", "status"):
        if tracker_row.get(key):
            return str(tracker_row[key]).strip()
    return "In tracker"

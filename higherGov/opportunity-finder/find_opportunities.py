"""Multi-source opportunity finder for temporary-facility rental contracts.

Pulls opportunities captured by HigherGov and, when configured, open notices
from SAM.gov's public Opportunities API. It applies the keyword/NAICS search
locally, keeps only open opportunities (response deadline today or later),
scores each match, and writes a report of NEW opportunities only. Every
reported opportunity is recorded in data/seen_opportunities.json so later
runs never repeat it.

Usage (from the gov-con folder):
    python higherGov/opportunity-finder/find_opportunities.py            # since last run
    python higherGov/opportunity-finder/find_opportunities.py --days 14  # backfill window
    python higherGov/opportunity-finder/find_opportunities.py --min-match 50
    python higherGov/opportunity-finder/find_opportunities.py --dry-run  # don't mark seen
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # gov-con/
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
SEEN_FILE = DATA / "seen_opportunities.json"
STATE_FILE = DATA / "finder_state.json"
STORE_FILE = DATA / "opportunities.json"
API = "https://www.highergov.com/api-external/opportunity/"
# SAM's production examples use the /prod prefix. Keep this centralized because
# the public documentation has also shown the shorter path over time.
SAM_API = "https://api.sam.gov/opportunities/v2/search"
SAM_API_FALLBACK = "https://api.sam.gov/prod/opportunities/v2/search"
SAM_SOURCE = "SAM.gov"
SAM_API_STATS = {"ok": 0, "not_found": 0}

PHRASES = [
    "Temporary Kitchen", "Mobile Kitchen", "Temporary Kitchen Facilities", "Modular Kitchen",
    "Field Kitchen", "Disaster Relief Kitchen", "Temporary Dining Facility", "Basecamp Kitchen",
    "Emergency Kitchen Trailer", "Kitchen Container Unit", "Remote Catering Services",
    "Disaster Catering Support", "Turnkey Food Service Facility", "Laundry Trailer",
    "Mobile Laundry Unit", "Temporary Laundry Facility", "Modular Laundry Facilities",
    "Containerized Laundry Unit", "Portable Laundry Trailer", "Field Laundry System",
    "Disaster Laundry Services", "Emergency Laundry Trailer", "Shower Trailer",
    "Mobile Shower Unit", "Temporary Shower Facilities", "Portable Shower Trailer",
    "Containerized Shower Unit", "Restroom Trailer", "Mobile Restroom Facility",
    "Modular Restroom", "Portable Toilet Trailer", "Temporary Sanitation Facility",
    "Emergency Hygiene Facility", "Basecamp Shower Restroom", "Sleeper Trailer",
    "Mobile Sleeping Quarters", "Temporary Housing Trailer", "Temporary Sleeping Facilities",
    "Bunkhouse Trailer", "Crew Housing Unit", "Emergency Billeting Trailer",
    "Basecamp Sleeping Trailers", "Workforce Housing Camp", "Modular Bunk Units",
    "Bunk Bed Rental", "Temporary Dormitory Facility", "Dishwashing Trailer",
    "Mobile Dishwashing Unit", "Field Dishwashing System", "Portable Dishwashing Trailer",
    "Basecamp Facilities", "Temporary Basecamp Services", "Turnkey Basecamp Services",
    "Life Support Services", "Wastewater Trailer", "Potable Water Tank Rental",
    "Fuel Truck Service", "Generator Rental", "Light Tower Rental",
    "Temporary Power Distribution",
    "Portable Restroom", "Portable Toilet", "Mobile Restroom", "Handwashing Station", "Hand Washing Station", "Hand Wash Station", "Portable Sanitation", "Porta Potty", "Porta John", "Mobile Office Trailer", "Office Trailer",
]
RENTAL_TERMS = ["Rent", "Rental", "Rentals", "Lease", "Leasing", "Hire", "Service"]
EXCLUDE_TERMS = ["Purchase", "Buy", "Buying", "For Sale", "Equipment Sale", "Acquisition"]
# Words that disqualify a hit: "Solar Light Tower Rental" is not our light tower rental.
NEGATIVE_TERMS = ["Solar"]
NAICS = {"624230", "721310", "532490", "722310", "561210", "624221", "423740", "562991"}
TIERS = [100, 50, 25, 0]  # match filters: >=100%, >=50%, >=25%, >0%


CONFIG_FILE = DATA / "search_config.json"
DEFAULTS = {"phrases": PHRASES, "rental_terms": RENTAL_TERMS,
            "exclude_terms": EXCLUDE_TERMS, "negative_terms": NEGATIVE_TERMS, "naics": sorted(NAICS)}
if CONFIG_FILE.exists():  # edits saved from the dashboard override the defaults
    _c = {**DEFAULTS, **json.loads(CONFIG_FILE.read_text())}
    PHRASES, RENTAL_TERMS, EXCLUDE_TERMS = _c["phrases"], _c["rental_terms"], _c["exclude_terms"]
    NEGATIVE_TERMS = _c["negative_terms"]
    NAICS = set(_c["naics"])


def rx(terms):
    # Word-bounded, case-insensitive; "Trailer" also matches "Trailers".
    alts = "|".join(re.escape(t).replace(r"\ ", r"\s+") + "s?" for t in sorted(terms, key=len, reverse=True))
    return re.compile(rf"\b(?:{alts})\b", re.I)


PHRASE_RX, RENTAL_RX, EXCLUDE_RX = rx(PHRASES), rx(RENTAL_TERMS), rx(EXCLUDE_TERMS)
NEG_RX = rx(NEGATIVE_TERMS) if NEGATIVE_TERMS else None
NEG_WINDOW = 30  # chars before a phrase checked for a negative modifier ("solar light tower")


def phrase_hits(text):
    """Keyword phrases found in text, skipping any qualified by a negative term just before it."""
    hits = set()
    for m in PHRASE_RX.finditer(text):
        before = text[max(0, m.start() - NEG_WINDOW):m.start()]
        if NEG_RX and NEG_RX.search(before):
            return None  # e.g. "solar powered light tower": the contract is for the wrong product
        hits.add(re.sub(r"\s+", " ", m.group(0).lower()).rstrip("s"))
    return hits


def score(opp):
    """Return match % (0 = rejected).
    Required: one of our keyword phrases (not qualified by a negative term such as
    "Solar"), a rental term, and no excluded or negative term in the title.
    The NAICS code only raises the score; it never qualifies a contract by itself.
    Excluded terms are checked on the title only: SAM descriptions are full of
    boilerplate such as 'Federal Acquisition Regulation' and 'purchase order'."""
    title = opp.get("title") or ""
    desc = opp.get("description_text") or ""
    naics = ((opp.get("naics_code") or {}).get("naics_code") or "")
    if EXCLUDE_RX.search(title) or (NEG_RX and NEG_RX.search(title)):
        return 0
    if not RENTAL_RX.search(f"{title}\n{desc}"):
        return 0
    phrases_title, phrases_desc = phrase_hits(title), phrase_hits(desc)
    if phrases_title is None or phrases_desc is None:
        return 0
    if not (phrases_title or phrases_desc):
        return 0
    pct = 0
    pct += 50 if phrases_title else 0               # service named in the title
    pct += 25 if phrases_desc else 0                # service named in the description
    pct += 25 if RENTAL_RX.search(title) else 0     # rental/lease named in the title
    pct += 25 if naics in NAICS else 0              # one of our NAICS codes
    extra = len(phrases_title | phrases_desc) - 1   # several services named
    pct += min(max(extra, 0) * 10, 25)
    return pct


def env_value(name):
    """Read a secret from the process environment or the local env file."""
    if os.environ.get(name):
        return os.environ[name]
    env_file = ROOT / ".env.local"
    if not env_file.exists():
        return ""
    prefix = name + "="
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith(prefix):
            return line.split("=", 1)[1].strip().strip('"\'')
    return ""


def api_key():
    key = env_value("HIGHER_GOV_API_KEY")
    if key:
        return key
    sys.exit("HIGHER_GOV_API_KEY missing from gov-con/.env.local")


def sam_api_key():
    return env_value("SAM_GOV_API_KEY") or env_value("SAM_API_KEY")


def enabled_sources():
    return {"HigherGov": bool(env_value("HIGHER_GOV_API_KEY")), SAM_SOURCE: bool(sam_api_key())}


WORKERS = 8  # parallel page requests; one page of 100 takes ~5 s


class FetchError(RuntimeError):
    """Any failed API request. The run halts on it; progress is already saved."""
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


SKIPPED = []  # records HigherGov itself cannot serve (server error on that one record)


def fetch_page(key, params, page, size=100):
    q = urllib.parse.urlencode({"api_key": key, "page_size": size, "page_number": page, **params})
    try:
        with urllib.request.urlopen(f"{API}?{q}", timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:  # page past the end
            return {"results": [], "meta": {"pagination": {"pages": 0}}}
        raise FetchError(f"HigherGov returned HTTP {e.code} for {params} page {page}", e.code) from None
    except Exception as e:
        raise FetchError(f"Request failed for {params} page {page}: {e}") from None


def fetch_page_safe(key, params, page):
    """A 100-record page; if HigherGov's server errors on it (5xx), narrow down to the broken
    record(s), skip only those, and log them. Network and other errors still halt the run."""
    try:
        return fetch_page(key, params, page).get("results", [])
    except FetchError as e:
        if not (e.status and e.status >= 500):
            raise
    out = []
    for p10 in range((page - 1) * 10 + 1, page * 10 + 1):  # same 100 records, 10 at a time
        try:
            out += fetch_page(key, params, p10, 10).get("results", [])
            continue
        except FetchError as e:
            if not (e.status and e.status >= 500):
                raise
        for p1 in range((p10 - 1) * 10 + 1, p10 * 10 + 1):  # then 1 at a time
            try:
                out += fetch_page(key, params, p1, 1).get("results", [])
            except FetchError as e:
                if not (e.status and e.status >= 500):
                    raise
                SKIPPED.append(f"{params} record #{p1}")
                print(f"  WARNING: HigherGov server error on {params} record #{p1}; skipped that one record",
                      file=sys.stderr, flush=True)
    return out


def _pages(key, params, pages):
    """Fetch pages in parallel; on the first error cancel the rest and raise."""
    pool = ThreadPoolExecutor(WORKERS)
    try:
        return list(pool.map(lambda p: fetch_page_safe(key, params, p), pages))
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def fetch_all(key, params):
    """Every page for one filter (e.g. {'posted_date': '2026-11-10'})."""
    first = fetch_page(key, params, 1)
    pages = first["meta"]["pagination"]["pages"]
    out = first.get("results", [])
    for results in _pages(key, params, range(2, pages + 1)):
        out += results
    return out


def safe_fetch(label, fn, *a):
    """Run one API step; on any error log it, skip that step, and let the run continue."""
    try:
        return fn(*a)
    except FetchError as e:
        SKIPPED.append(f"{label} ({e})")
        print(f"  WARNING: {label} failed and was skipped: {e}", file=sys.stderr, flush=True)
        return []


def fetch_naics_recent(key, naics, cutoff):
    """One NAICS code, newest posted first, stopping once postings are older than cutoff."""
    params = {"naics_code": naics, "ordering": "-posted_date"}
    out, page = [], 1
    while True:
        batch = [o for res in _pages(key, params, range(page, page + WORKERS)) for o in res]
        out += batch
        if not batch or (batch[-1].get("posted_date") or "") < cutoff:
            return out
        page += WORKERS


def load(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def full_url(u):
    """HigherGov sometimes returns site-relative links (e.g. /sl/contract-forecast/...)."""
    u = (u or "").strip()
    if u.startswith("/"):
        return "https://www.highergov.com" + u
    if u and not u.startswith(("http://", "https://")):
        return ""  # e.g. a bare title instead of a link
    return u


def sam_date(value):
    """Normalize SAM timestamps and date-only values to YYYY-MM-DD."""
    return str(value or "")[:10]


def sam_link(notice):
    if notice.get("path"):
        return full_url(notice.get("path"))
    link = (notice.get("uiLink") or "").strip()
    if link and link.lower() != "null":
        return link
    notice_id = notice.get("noticeId") or ""
    return f"https://sam.gov/opp/{notice_id}/view" if notice_id else ""


def sam_contact(notice):
    contacts = notice.get("pointOfContact") or []
    if isinstance(contacts, dict):
        contacts = [contacts]
    if not isinstance(contacts, list):
        return ""
    for contact in contacts:
        if isinstance(contact, dict) and contact.get("email"):
            return contact["email"]
    return ""


def sam_place(notice):
    if notice.get("pop_city") or notice.get("pop_state"):
        return ", ".join(str(x) for x in (notice.get("pop_city"), notice.get("pop_state")) if x)
    place = notice.get("placeOfPerformance") or {}
    city = place.get("city") or {}
    state = place.get("state") or {}
    return ", ".join(str(x) for x in (city.get("name"), state.get("code")) if x)


def sam_notice(notice):
    """Map one SAM.gov notice into the finder’s source-neutral opportunity shape."""
    notice_id = str(notice.get("noticeId") or "").strip()
    if not notice_id:
        return None
    description = notice.get("description")
    description_url = description if isinstance(description, str) and description.startswith("http") else ""
    return {
        "_source": SAM_SOURCE,
        "_sam_description_url": description_url,
        "_sam_resource_links": notice.get("resourceLinks") or [],
        "opp_key": f"sam:{notice_id}",
        "title": (notice.get("title") or "").strip(),
        "description_text": (notice.get("descriptionText") or "").strip(),
        "source_id": (notice.get("solicitationNumber") or notice_id).strip(),
        "agency": {"agency_name": (notice.get("fullParentPathName") or notice.get("department") or "").strip()},
        "naics_code": {"naics_code": str(notice.get("naicsCode") or "")},
        "psc_code": {"psc_code": str(notice.get("classificationCode") or "")},
        "set_aside": notice.get("typeOfSetAsideDescription") or notice.get("typeOfSetAside") or "",
        "opp_type": {"description": notice.get("type") or notice.get("baseType") or ""},
        "posted_date": sam_date(notice.get("postedDate")),
        "due_date": sam_date(notice.get("responseDeadLine")),
        "pop_city": (notice.get("placeOfPerformance") or {}).get("city", {}).get("name", ""),
        "pop_state": (notice.get("placeOfPerformance") or {}).get("state", {}).get("code", ""),
        "path": sam_link(notice),
        "source_path": sam_link(notice),
        "primary_contact_email": {"contact_email": sam_contact(notice)},
        "_raw_title": (notice.get("title") or "").strip(),
    }


def fetch_sam_page(key, params, limit=1000, offset=0):
    query = {**params, "api_key": key, "limit": limit, "offset": offset}
    last_404 = None
    for endpoint in dict.fromkeys((SAM_API, SAM_API_FALLBACK)):
        try:
            request = urllib.request.Request(
                f"{endpoint}?{urllib.parse.urlencode(query, doseq=True)}",
                headers={"Accept": "application/json", "User-Agent": "gov-con opportunity finder/1.0"},
            )
            with urllib.request.urlopen(request, timeout=60) as response:
                SAM_API_STATS["ok"] += 1
                return json.load(response)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                SAM_API_STATS["not_found"] += 1
                last_404 = endpoint
                continue  # Some SAM deployments use 404 for no matching notices.
            raise FetchError(f"SAM.gov returned HTTP {e.code} for offset {offset}", e.code) from None
        except Exception as e:
            raise FetchError(f"SAM.gov request failed at offset {offset}: {e}") from None
    if last_404:
        return {"totalRecords": 0, "opportunitiesData": []}
    raise FetchError(f"SAM.gov returned no response for offset {offset}")


def fetch_sam_query(key, params, max_records=5000):
    """Fetch one SAM query, paging in 1,000-record batches."""
    limit, offset, out = 1000, 0, []
    while len(out) < max_records:
        page = fetch_sam_page(key, params, limit, offset)
        batch = page.get("opportunitiesData") or []
        out.extend(batch)
        total = int(page.get("totalRecords") or 0)
        if not batch or len(out) >= total or len(batch) < limit:
            break
        # SAM documents offset as a page index, not a record offset.
        offset += 1
    return out[:max_records]


def sam_opportunities(key, cutoff, today, naics):
    """Fetch open SAM notices by target NAICS and broad title terms, de-duped by notice ID."""
    SAM_API_STATS.update(ok=0, not_found=0)
    posted_from = dt.date.fromisoformat(cutoff).strftime("%m/%d/%Y")
    posted_to = today.strftime("%m/%d/%Y")
    base = {"postedFrom": posted_from, "postedTo": posted_to,
            "ptype": ["p", "o", "k", "r", "s"]}
    queries = [(f"NAICS {code}", {**base, "ncode": code}) for code in sorted(naics)]
    # NAICS is the main scan. These terms catch notices whose NAICS is missing or misclassified.
    title_terms = ["trailer", "kitchen", "laundry", "shower", "restroom", "toilet", "portable",
                   "sanitation", "comfort station", "generator", "basecamp", "wastewater"]
    queries += [(f"title {term}", {**base, "title": term}) for term in title_terms]
    queries.append(("PSC W085", {**base, "ccode": "W085"}))
    notices, seen = [], set()
    for label, params in queries:
        for raw in fetch_sam_query(key, params):
            notice = sam_notice(raw)
            if notice and notice["opp_key"] not in seen:
                seen.add(notice["opp_key"])
                notices.append(notice)
        print(f"SAM.gov {label}: scanned {len(notices)} unique notices", file=sys.stderr)
    if SAM_API_STATS["ok"] == 0 and SAM_API_STATS["not_found"] >= len(queries) * 2:
        raise FetchError(
            "SAM.gov's production API gateway returned empty HTTP 404s for every query (an outage "
            "on SAM's side, not a key problem). No SAM records were imported; they will be "
            "picked up on the next run once SAM is back."
        )
    return notices


def row(o, pct):
    if o.get("_source") == SAM_SOURCE:
        return {
            "Match %": pct,
            "Source": SAM_SOURCE,
            "Title": o.get("title") or "",
            "Solicitation #": o.get("source_id") or o["opp_key"],
            "Agency": (o.get("agency") or {}).get("agency_name", ""),
            "NAICS": (o.get("naics_code") or {}).get("naics_code", ""),
            "PSC": (o.get("psc_code") or {}).get("psc_code", ""),
            "Set-aside": o.get("set_aside") or "",
            "Type": (o.get("opp_type") or {}).get("description", ""),
            "Posted": o.get("posted_date") or "",
            "Response Deadline": (o.get("due_date") or "")[:10],
            "Place of Performance": sam_place(o),
            "Link": sam_link(o),
            "HigherGov": "",
            "Contact": (o.get("primary_contact_email") or {}).get("contact_email", ""),
            "opp_key": o["opp_key"],
            "_desc": (o.get("description_text") or "")[:3000],
            "_raw_title": o.get("title") or "",
            "_naics": (o.get("naics_code") or {}).get("naics_code", ""),
            "_source": SAM_SOURCE,
            "_sam_description_url": o.get("_sam_description_url") or "",
            "_sam_resource_links": o.get("_sam_resource_links") or [],
        }
    contact = o.get("primary_contact_email") or {}
    pop = ", ".join(p for p in (o.get("pop_city"), o.get("pop_state")) if p)
    return {
        "Match %": pct,
        "Source": "HigherGov",
        "Title": o.get("title") or "",
        "Solicitation #": o.get("source_id") or o.get("opp_key"),
        "Agency": (o.get("agency") or {}).get("agency_name", ""),
        "NAICS": (o.get("naics_code") or {}).get("naics_code", ""),
        "PSC": (o.get("psc_code") or {}).get("psc_code", ""),
        "Set-aside": o.get("set_aside") or "",
        "Type": (o.get("opp_type") or {}).get("description", ""),
        "Posted": o.get("posted_date") or "",
        "Response Deadline": (o.get("due_date") or "")[:10],
        "Place of Performance": pop,
        "Link": full_url(o.get("source_path") or o.get("path")),
        "HigherGov": full_url(o.get("path")),
        "Contact": contact.get("contact_email") or "",
        "opp_key": o["opp_key"],
        "_version_key": o.get("version_key") or "",
        "_desc": (o.get("description_text") or "")[:3000],
        "_raw_title": o.get("title") or "",
    }


def rescore_store(key):
    """Re-check every saved opportunity against the current rules; drop the ones that fail.
    Items saved before descriptions were stored are re-fetched by opp_key."""
    store = load(STORE_FILE, {})
    if not store:
        return 0, 0
    missing = [k for k, r in store.items() if "_desc" not in r and r.get("Source") != SAM_SOURCE]
    if missing:
        def get(k):
            res = fetch_page(key, {"opp_key": k}, 1).get("results") or []
            return k, (res[0] if res else None)
        pool = ThreadPoolExecutor(WORKERS)
        try:
            for k, o in pool.map(get, missing):
                if o:
                    store[k]["_desc"] = (o.get("description_text") or "")[:3000]
                    store[k]["_raw_title"] = o.get("title") or ""
                    store[k]["_naics"] = (o.get("naics_code") or {}).get("naics_code", "")
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
    removed = []
    for k, r in list(store.items()):
        opp = {"title": r.get("_raw_title") or r.get("Title"), "description_text": r.get("_desc", ""),
               "naics_code": {"naics_code": r.get("_naics") or r.get("NAICS")}}
        pct = score(opp)
        if pct == 0:
            removed.append(r.get("Title"))
            del store[k]
        else:
            r["Match %"] = pct
    STORE_FILE.write_text(json.dumps(store, indent=1), encoding="utf-8")
    for t in removed:
        print(f"  removed (no longer matches): {t}", file=sys.stderr)
    return len(removed), len(store)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, help="force a full open-opportunity scan of postings from the last N days")
    ap.add_argument("--lookback", type=int, default=120,
                    help="first/full run: how far back postings can be and still be open (default 120 days)")
    ap.add_argument("--min-match", type=int, default=0, choices=TIERS, help="lowest match tier to report")
    ap.add_argument("--dry-run", action="store_true", help="report without marking opportunities as seen")
    ap.add_argument("--rescore", action="store_true",
                    help="only re-check saved opportunities against the current rules, then exit")
    ap.add_argument("--include-seen", action="store_true", help="also list previously reported opportunities")
    args = ap.parse_args()

    DATA.mkdir(exist_ok=True)
    REPORTS.mkdir(exist_ok=True)
    today = dt.date.today()
    seen = load(SEEN_FILE, {})
    state = load(STATE_FILE, {})

    key = api_key()
    sam_key = sam_api_key()
    if args.rescore:
        try:
            gone, kept = rescore_store(key)
        except FetchError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(2)
        print(f"Re-checked saved opportunities: removed {gone}, kept {kept}.", file=sys.stderr)
        return
    found, scanned = {}, 0

    def consider(o):
        due = (o.get("due_date") or "")[:10]
        if not due or due < today.isoformat():
            return  # only open opportunities: deadline today or later
        pct = score(o)
        if pct > 0 and pct >= args.min_match:
            k = o["opp_key"]  # stable across amendments/versions
            if k in seen and not args.include_seen:
                return
            if k not in found or pct > found[k]["Match %"]:
                found[k] = row(o, pct)

    def checkpoint(progress):
        """Save matches found so far and where to resume, so a halt loses nothing."""
        if args.dry_run:
            return
        store = load(STORE_FILE, {})
        for k, r in found.items():
            seen.setdefault(k, {"first_seen": today.isoformat(), "title": r["Title"],
                                "deadline": r["Response Deadline"]})
            store.setdefault(k, {**r, "first_seen": today.isoformat()})
        SEEN_FILE.write_text(json.dumps(seen, indent=1), encoding="utf-8")
        STORE_FILE.write_text(json.dumps(store, indent=1), encoding="utf-8")
        state.update(progress)
        STATE_FILE.write_text(json.dumps(state, indent=1), encoding="utf-8")

    full = bool(args.days) or not state.get("backfilled")
    resume = state.get("resume") if full and not args.days else None
    if full and not resume:
        window = args.days or args.lookback
        resume = {"cutoff": (today - dt.timedelta(days=window)).isoformat(), "naics_done": [],
                  "next_posted": (today - dt.timedelta(days=window)).isoformat()}
    start = dt.date.fromisoformat(resume["cutoff"]) if full else dt.date.fromisoformat(state["last_captured_date"])
    error = None
    if full:
        days_left = (today - dt.date.fromisoformat(resume["next_posted"])).days + 1
        total = len([c for c in NAICS if c not in resume["naics_done"]]) + max(days_left, 0)
    else:
        total = (today - start).days + 1
    total += 1 if sam_key else 0
    done = 0

    def progress(label):
        nonlocal done
        done += 1
        print(f"PROGRESS {done} {total} {label}", file=sys.stderr, flush=True)

    print(f"PROGRESS 0 {total} starting", file=sys.stderr, flush=True)
    try:
        if full:
            # Everything still open, whenever it was posted.
            if state.get("resume"):
                print(f"Resuming: NAICS done {resume['naics_done']}, posted from {resume['next_posted']}", file=sys.stderr)
            for code in sorted(NAICS):                     # 1) our NAICS codes, any keyword
                if code in resume["naics_done"]:
                    continue
                batch = safe_fetch(f"NAICS {code}", fetch_naics_recent, key, code, resume["cutoff"])
                for o in batch:
                    consider(o)
                scanned += len(batch)
                resume["naics_done"].append(code)
                checkpoint({"resume": resume})
                print(f"NAICS {code}: scanned {len(batch)}", file=sys.stderr)
                progress(f"NAICS {code}")
            day = dt.date.fromisoformat(resume["next_posted"])  # 2) keyword hits under any NAICS
            while day <= today:
                batch = safe_fetch(f"posted {day}", fetch_all, key, {"posted_date": day.isoformat()})
                for o in batch:
                    consider(o)
                scanned += len(batch)
                day += dt.timedelta(days=1)
                resume["next_posted"] = day.isoformat()
                checkpoint({"resume": resume})
                print(f"posted {day - dt.timedelta(days=1)}: scanned {len(batch)}  ({len(found)} open matches so far)", file=sys.stderr)
                progress(f"posted {day - dt.timedelta(days=1)}")
            state.pop("resume", None)
            checkpoint({"backfilled": True, "last_captured_date": today.isoformat()})
        else:
            # Incremental: only what HigherGov captured since the last run (new + amended).
            day = start
            while day <= today:
                batch = safe_fetch(f"captured {day}", fetch_all, key, {"captured_date": day.isoformat()})
                for o in batch:
                    consider(o)
                scanned += len(batch)
                checkpoint({"last_captured_date": day.isoformat()})
                print(f"captured {day}: scanned {len(batch)}", file=sys.stderr)
                progress(f"captured {day}")
                day += dt.timedelta(days=1)
    except FetchError as e:
        error = str(e)

    # SAM.gov is an optional second source. It uses a short rolling window so amendments
    # and newly posted notices are picked up even though its public API is posted-date based.
    if sam_key and not args.rescore:
        try:
            configured_sam_days = int(env_value("SAM_GOV_LOOKBACK_DAYS") or 120)
        except ValueError:
            configured_sam_days = 120
        sam_days = max(1, min(365, args.days or configured_sam_days))
        sam_cutoff = (today - dt.timedelta(days=sam_days)).isoformat()
        try:
            sam_rows = sam_opportunities(sam_key, sam_cutoff, today, NAICS)
            for o in sam_rows:
                consider(o)
            state["last_sam_posted"] = today.isoformat()
            checkpoint({"last_sam_posted": today.isoformat()})
            progress(f"SAM.gov ({len(sam_rows)} notices)")
        except FetchError as e:
            # SAM is optional: warn, keep HigherGov results, and leave last_sam_posted
            # unchanged so the next run re-scans the missed window.
            print(f"WARNING: SAM.gov skipped this run: {e}", file=sys.stderr)
    elif not sam_key:
        print("SAM.gov skipped: set SAM_GOV_API_KEY in .env.local to enable it.", file=sys.stderr)

    rows = sorted(found.values(), key=lambda r: (-r["Match %"], r["Response Deadline"]))
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
    cols = [c for c in rows[0].keys() if c != "opp_key" and not c.startswith("_")] if rows else []
    tsv = REPORTS / f"opportunities_{stamp}.tsv"
    md = REPORTS / f"opportunities_{stamp}.md"
    if rows:
        with tsv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, delimiter="\t", extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        lines = [f"# New opportunities {today} (scanned {scanned} captured {start}..{today})", ""]
        for t in TIERS:
            tier = [r for r in rows if (r["Match %"] >= t if t else r["Match %"] > 0)
                    and not any(r["Match %"] >= u for u in TIERS if u > t)]
            label = f">={t}%" if t else ">0%"
            lines += [f"## Match {label} ({len(tier)})", ""]
            if tier:
                lines.append("| " + " | ".join(cols) + " |")
                lines.append("|" + "---|" * len(cols))
                for r in tier:
                    lines.append("| " + " | ".join(str(r[c]).replace("|", "/") for c in cols) + " |")
            lines.append("")
        md.write_text("\n".join(lines), encoding="utf-8")

    print(f"Scanned {scanned}; {len(rows)} new open matches.", file=sys.stderr)
    if SKIPPED:
        print(f"Note: skipped {len(SKIPPED)} item(s) after API errors: "
              + "; ".join(SKIPPED[:10]), file=sys.stderr)
    if rows:
        print(f"Report: {md}\nTSV:    {tsv}", file=sys.stderr)
        print("Found these contracts.\n")
        print("\t".join(cols))
        for r in rows:
            print("\t".join(str(r[c]) for c in cols))
    if error:
        print(f"ERROR: {error}\nSearch halted. {len(rows)} matches found before the error are saved; "
              "run the search again to resume where it stopped.", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()

"""Local dashboard for the HigherGov opportunity finder.

    python higherGov/opportunity-finder/dashboard.py     # then open http://localhost:8765

Serves only on 127.0.0.1; the API key never leaves this machine.
"""
import json
import re
import subprocess
import urllib.parse
import urllib.request
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
STORE = ROOT / "data" / "opportunities.json"
STATE = ROOT / "data" / "finder_state.json"
LEAD_STATUS = ROOT / "data" / "lead_status.json"
PORT = 8765
# Lead workflow. Auto updates (copy, Teams send) only move a lead forward through
# AUTO_ORDER, so they never overwrite a manual status like "Bid submitted".
STATUSES = ["New", "In review", "Copied", "Sent", "Bid submitted", "Won", "Lost", "No bid"]
AUTO_ORDER = ["New", "In review", "Copied", "Sent"]
status_lock = threading.Lock()

sys.path.insert(0, str(HERE))
import find_opportunities as finder  # noqa: E402  (api key + fetch helpers)
import proposal  # noqa: E402
import teams  # noqa: E402
import tracker  # noqa: E402

teams_log = {"last": None, "error": None}


def lead_statuses():
    return json.loads(LEAD_STATUS.read_text()) if LEAD_STATUS.exists() else {}


def set_status(keys, status, auto=False):
    """Set a lead status (with history). auto=True only advances New -> In review -> Copied -> Sent."""
    import datetime as dt
    now = dt.datetime.now().isoformat(timespec="minutes")
    with status_lock:
        data, changed = lead_statuses(), 0
        for k in keys:
            cur = (data.get(k) or {}).get("status", "New")
            if auto and (cur not in AUTO_ORDER or AUTO_ORDER.index(cur) >= AUTO_ORDER.index(status)):
                continue
            rec = data.setdefault(k, {"history": []})
            rec.update(status=status, updated=now)
            rec["history"].append({"status": status, "at": now, "auto": auto})
            changed += 1
        LEAD_STATUS.parent.mkdir(exist_ok=True)
        LEAD_STATUS.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return changed


def api_rows():
    st = lead_statuses()
    # Keep page loads local and predictable; the dashboard's Refresh tracker
    # button performs the network sync explicitly.
    tracker_data = tracker.load(refresh_if_stale=False)
    tracker_index = tracker_data.get("ids") or {}
    tracker_available = bool(tracker_data.get("synced_at"))
    return [{"Status": (st.get(r.get("opp_key")) or {}).get("status", "New"),
             "Status updated": (st.get(r.get("opp_key")) or {}).get("updated", ""), **{k: v for k, v in r.items() if not k.startswith("_")}, "bid": bid_summary(r),
             "Summary": r.get("Summary") or r.get("_desc") or "",
             "Link": finder.full_url(r.get("Link")) or finder.full_url(r.get("HigherGov")),
             "HigherGov": finder.full_url(r.get("HigherGov")),
             "Tracker": tracker.status_for(r, tracker_index, tracker_available)}
            for r in (json.loads(STORE.read_text()).values() if STORE.exists() else [])]


def teams_auto():
    return teams.webhook() and (proposal.env_value("TEAMS_AUTO") or "on").lower() != "off"


def post_teams(rows, title, subtitle="", format="table"):
    """Send to Teams; record the outcome for the dashboard instead of raising."""
    try:
        n = teams.send_rows(rows, title, subtitle, format=format)
        info = teams.last_send_info()
        label = "detailed briefs" if format == "brief" else "table"
        parts = []
        if info["sent_keys"]:
            parts.append(f"sent {len(info['sent_keys'])}")
        if info["duplicates"]:
            parts.append(f"skipped {len(info['duplicates'])} already in Gov Contracting")
        if info["unverified"]:
            parts.append(f"skipped {len(info['unverified'])} without a solicitation number")
        summary = ", ".join(parts) or "nothing eligible to send"
        teams_log.update(last=f"Teams {label}: {summary} ({n} card{'s' if n != 1 else ''}; destination {info['destination']}).",
                         duplicates=info["duplicate_solicitations"], unverified=info["unverified"], error=None)
        return True
    except Exception as e:
        teams_log.update(error=f"Teams: {e}")
        return False

ai_jobs = {}  # opp_key -> {"kind", "running", "error"}


def run_ai(k, kind):
    ai_jobs[k] = {"kind": kind, "running": True, "error": None}
    try:
        if opp_record(k).get("Source") != "HigherGov":
            raise RuntimeError("AI bid packs are currently available for HigherGov records; open the SAM.gov notice for its attachments.")
        (proposal.bid_check if kind == "bidcheck" else proposal.draft_proposal)(k)
    except proposal.NoAIKey as e:
        ai_jobs[k]["error"] = str(e)
        ai_jobs[k]["nokey"] = True
    except Exception as e:
        ai_jobs[k]["error"] = f"{type(e).__name__}: {e}"
    ai_jobs[k]["running"] = False


batch = {"running": False, "done": 0, "total": 0, "current": [], "error": None, "stop": False, "results": {}}
BATCH_WORKERS = 3


def run_batch(keys):
    """Bid-check many opportunities, 3 at a time. Halts on the first error (nothing done is lost)."""
    from concurrent.futures import ThreadPoolExecutor
    batch.update(running=True, done=0, total=len(keys), current=[], error=None, stop=False, results={})

    def one(k):
        if batch["stop"] or batch["error"]:
            return
        title = (opp_record(k).get("Title") or k)[:60]
        batch["current"].append(title)
        run_ai(k, "bidcheck")
        batch["current"].remove(title)
        job = ai_jobs.get(k) or {}
        if job.get("error"):
            batch["error"] = f"{title}: {job['error']}"
            return
        st = ai_state(k).get("bidcheck") or {}
        batch["results"][k] = {"verdict": st.get("verdict"), "score": st.get("score")}
        batch["done"] += 1

    # paid API: 3 at a time; subscription: 2 (shares your usage limits); free Gemini: 1
    workers = {"claude": BATCH_WORKERS, "claude-code": 2, "codex": 2}.get(proposal.provider(), 1)
    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(one, keys))
    batch["running"] = False
    if teams_auto() and batch["results"]:
        rows = [r for r in api_rows() if r["opp_key"] in batch["results"]]
        order = {"Bid": 0, "Maybe": 1, "No bid": 2}
        rows.sort(key=lambda r: (order.get((r["bid"] or {}).get("verdict"), 3), -((r["bid"] or {}).get("score") or 0)))
        tally = ", ".join(f"{sum(1 for x in batch['results'].values() if x['verdict'] == v)} {v}" for v in order)
        post_teams(rows, "Bid check results", tally + (" (halted early)" if batch["error"] else ""))


def bid_summary(r):
    f = proposal.DRAFTS / proposal.safe_name(r.get("Solicitation #") or r["opp_key"]) / "bid-check.json"
    if not f.exists():
        return None
    try:
        b = json.loads(f.read_text(encoding="utf-8"))
        return {"verdict": b.get("verdict"), "score": b.get("score"), "one_line": b.get("one_line"),
                "scope": b.get("scope"), "must_have": b.get("must_have") or [],
                "gaps": b.get("gaps") or [], "deadline": b.get("deadline")}
    except Exception:
        return None


def ai_state(k):
    r = opp_record(k)
    folder = proposal.DRAFTS / proposal.safe_name(r.get("Solicitation #") or k)
    out = {"job": ai_jobs.get(k), "folder": str(folder), "has_pack": (folder / "bid-pack.md").exists()}
    if (folder / "bid-check.json").exists():
        out["bidcheck"] = json.loads((folder / "bid-check.json").read_text(encoding="utf-8"))
    for name in ("proposal-draft.md", "sources-sought-response.md"):
        if (folder / name).exists():
            out["draft"] = (folder / name).read_text(encoding="utf-8")
            out["draft_name"] = name
            meta = (folder / name).with_suffix(".meta.json")
            if meta.exists():
                out["draft_meta"] = json.loads(meta.read_text(encoding="utf-8"))
    out["market_research"] = any(m in (r.get("Type") or "").lower() for m in proposal.MARKET_RESEARCH)
    out["capability_pdf"] = proposal.CAPABILITY_PDF.exists()
    out["ai"] = proposal.provider()
    return out

DOC_API = "https://www.highergov.com/api-external/document/"
BASIS = ROOT / "basis"


def opp_record(k):
    store = json.loads(STORE.read_text()) if STORE.exists() else {}
    r = store.get(k)
    if r is None:
        raise KeyError("unknown opportunity")
    if r.get("Source") == finder.SAM_SOURCE:
        return r
    if not r.get("_version_key"):  # saved before version keys were stored
        res = finder.fetch_page(finder.api_key(), {"opp_key": k}, 1).get("results") or []
        if not res:
            raise KeyError("opportunity no longer on HigherGov")
        r["_version_key"] = res[0]["version_key"]
        store[k] = r
        STORE.write_text(json.dumps(store, indent=1), encoding="utf-8")
    return r


def list_docs(k):
    r = opp_record(k)
    if r.get("Source") == finder.SAM_SOURCE:
        return r, []
    q = urllib.parse.urlencode({"api_key": finder.api_key(), "page_size": 100, "related_key": r["_version_key"]})
    with urllib.request.urlopen(f"{DOC_API}?{q}", timeout=60) as resp:
        docs = json.load(resp).get("results", [])
    return r, docs


def folder_for(r):
    name = re.sub(r"[^\w.\- ]+", "_", str(r.get("Solicitation #") or r["opp_key"])).strip()[:80]
    return BASIS / name


def save_docs(k):
    r, docs = list_docs(k)
    if r.get("Source") == finder.SAM_SOURCE:
        return {"folder": "", "saved": 0, "skipped": 0, "total": 0}
    folder = folder_for(r)
    folder.mkdir(parents=True, exist_ok=True)
    saved = skipped = 0
    lines = [f"# {r['Title']}", "", f"- Solicitation #: {r.get('Solicitation #')}", f"- Agency: {r.get('Agency')}",
             f"- Deadline: {r.get('Response Deadline')}", f"- Link: {r.get('Link')}", ""]
    for doc in docs:
        fname = re.sub(r'[<>:"/\\|?*]+', "_", doc.get("file_name") or "document")
        dest = folder / fname
        size = int(doc.get("file_size") or 0)
        if dest.exists() and (not size or dest.stat().st_size == size):
            skipped += 1  # already downloaded: never duplicate
        else:
            with urllib.request.urlopen(doc["download_url"], timeout=120) as resp:
                dest.write_bytes(resp.read())
            saved += 1
        lines += [f"## {fname}", "", f"Posted {(doc.get('posted_date') or '')[:10]}", "",
                  doc.get("summary") or "_No summary._", ""]
        if doc.get("text_extract"):
            (folder / (dest.stem + ".txt")).write_text(doc["text_extract"], encoding="utf-8")
    (folder / "_summaries.md").write_text("\n".join(lines), encoding="utf-8")
    return {"folder": str(folder), "saved": saved, "skipped": skipped, "total": len(docs)}


job = {"running": False, "log": "", "code": None, "error": None, "proc": None, "progress": None}


def run_finder(days, rescore=False):
    before = set(json.loads(STORE.read_text())) if STORE.exists() else set()
    _run_finder(days, rescore)
    if rescore or not teams_auto():
        return
    new = [r for r in api_rows() if r["opp_key"] not in before]
    if new:
        new.sort(key=lambda r: (-r["Match %"], r.get("Response Deadline") or ""))
        post_teams(new, "New contract opportunities found", f"{len(new)} new open contracts, best match first")


def _run_finder(days, rescore=False):
    cmd = [sys.executable, str(HERE / "find_opportunities.py")]
    if rescore:
        cmd.append("--rescore")
    elif days:
        cmd += ["--days", str(days)]
    job.update(running=True, log="", code=None, error=None, progress=None)
    p = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    job["proc"] = p
    for line in p.stderr:
        if line.startswith("PROGRESS "):
            _, d, t, label = (line.strip().split(" ", 3) + [""])[:4]
            job["progress"] = {"done": int(d), "total": int(t), "label": label}
            continue
        job["log"] = (job["log"] + line)[-4000:]
        if line.startswith("ERROR:") or line.startswith("Traceback"):
            job["error"] = line.strip()
    code = p.wait()
    if code and not job["error"]:
        job["error"] = f"Search failed (exit {code})."
    job.update(running=False, code=code, proc=None)


class H(BaseHTTPRequestHandler):
    def send(self, body, ctype="application/json", code=200):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            return self.send((HERE / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/opportunities":
            rows = api_rows()
            return self.send(json.dumps(rows))
        if self.path == "/api/config":
            sys.path.insert(0, str(HERE))
            import importlib, find_opportunities as f
            importlib.reload(f)
            cur = {"phrases": f.PHRASES, "rental_terms": f.RENTAL_TERMS, "exclude_terms": f.EXCLUDE_TERMS,
                   "negative_terms": f.NEGATIVE_TERMS, "naics": sorted(f.NAICS)}
            return self.send(json.dumps({"current": cur, "defaults": f.DEFAULTS, "sources": f.enabled_sources()}))
        if self.path == "/api/teams":
            return self.send(json.dumps({**teams_log, "configured": bool(teams.webhook()),
                                         "auto": bool(teams_auto()), "destination": teams.send_target(),
                                         "duplicate_check": teams.duplicate_check_configured()}))
        if self.path == "/api/tracker":
            data = tracker.load(refresh_if_stale=False)
            return self.send(json.dumps({"configured": bool(tracker._sheet_csv_url()),
                                         "count": data.get("count", 0), "synced_at": data.get("synced_at"),
                                         "error": data.get("error")}))
        if self.path == "/api/ai/batch":
            return self.send(json.dumps({k: v for k, v in batch.items() if k != "stop"}))
        if self.path.startswith("/api/ai?"):
            k = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("k", [""])[0]
            try:
                if opp_record(k).get("Source") != "HigherGov":
                    return self.send(json.dumps({"error": "AI bid packs are currently available for HigherGov records; open the SAM.gov notice for its attachments."}), code=409)
                return self.send(json.dumps(ai_state(k)))
            except Exception as e:
                return self.send(json.dumps({"error": str(e)}), code=502)
        if self.path.startswith("/api/docs?"):
            k = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("k", [""])[0]
            try:
                r, docs = list_docs(k)
            except Exception as e:
                return self.send(json.dumps({"error": f"Could not load documents: {e}"}), code=502)
            out = [{"file_name": x.get("file_name"), "file_type": x.get("file_type"),
                    "file_size": x.get("file_size"), "posted_date": (x.get("posted_date") or "")[:10],
                    "summary": x.get("summary"), "download_url": x.get("download_url")} for x in docs]
            f = folder_for(r)
            return self.send(json.dumps({"docs": out, "folder": str(f), "saved_already": f.exists(),
                                         "source_note": "SAM.gov attachments are available from the linked notice." if r.get("Source") == finder.SAM_SOURCE else ""}))
        if self.path == "/api/capability.pdf":
            return self.send(proposal.CAPABILITY_PDF.read_bytes(), "application/pdf")
        if self.path == "/api/status":
            last = json.loads(STATE.read_text()).get("last_captured_date") if STATE.exists() else None
            return self.send(json.dumps({**{k: v for k, v in job.items() if k != "proc"}, "last_run": last,
                                         "sources": finder.enabled_sources()}))
        self.send("not found", "text/plain", 404)

    def do_POST(self):
        if self.path == "/api/config":
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
            cfg = ROOT / "data" / "search_config.json"
            if body.get("reset"):
                cfg.unlink(missing_ok=True)
            else:
                keys = ("phrases", "rental_terms", "exclude_terms", "negative_terms", "naics")
                clean = {k: [str(x).strip() for x in body.get(k, []) if str(x).strip()] for k in keys}
                if not clean["phrases"] and not clean["naics"]:
                    return self.send(json.dumps({"error": "need phrases or NAICS"}), code=400)
                cfg.parent.mkdir(exist_ok=True)
                cfg.write_text(json.dumps(clean, indent=1), encoding="utf-8")
            return self.send(json.dumps({"saved": True}))
        if self.path == "/api/ai/batch":
            if batch["running"]:
                return self.send(json.dumps({"error": "a batch is already running"}), code=409)
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            keys = [k for k in dict.fromkeys(body.get("keys") or []) if isinstance(k, str)]
            if not keys:
                return self.send(json.dumps({"error": "select at least one contract"}), code=400)
            threading.Thread(target=run_batch, args=(keys,), daemon=True).start()
            return self.send(json.dumps({"started": True, "total": len(keys)}))
        if self.path == "/api/teams":
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            keys = body.get("keys") or []
            by_key = {r["opp_key"]: r for r in api_rows()}
            rows = [by_key[k] for k in keys if k in by_key]  # keep the table's order
            if not rows:
                return self.send(json.dumps({"error": "nothing to send"}), code=400)
            format = body.get("format") if body.get("format") in ("table", "brief") else "table"
            ok = post_teams(rows, body.get("title") or "Contract opportunities", body.get("subtitle") or "", format=format)
            if ok:
                info = teams.last_send_info()
                set_status(info["sent_keys"], "Sent", auto=True)
                set_status(info["duplicates"], "Sent", auto=True)
            return self.send(json.dumps(teams_log), code=200 if ok else 502)
        if self.path == "/api/tracker/refresh":
            data = tracker.refresh()
            return self.send(json.dumps({"count": data.get("count", 0), "synced_at": data.get("synced_at"),
                                         "error": data.get("error")}), code=200 if not data.get("error") else 502)
        if self.path == "/api/briefs":
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            keys = body.get("keys") or []
            by_key = {r["opp_key"]: r for r in api_rows()}
            rows = [by_key[k] for k in keys if k in by_key] if keys else list(by_key.values())
            if not rows:
                return self.send(json.dumps({"error": "nothing to copy"}), code=400)
            set_status([r["opp_key"] for r in rows], "Copied", auto=True)
            return self.send(json.dumps({"text": teams.brief_markdown(rows)}))
        if self.path == "/api/lead-status":
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            keys = [k for k in body.get("keys") or [] if isinstance(k, str)]
            status = body.get("status")
            if status not in STATUSES or not keys:
                return self.send(json.dumps({"error": f"status must be one of {STATUSES}"}), code=400)
            return self.send(json.dumps({"changed": set_status(keys, status, bool(body.get("auto")))}))
        if self.path == "/api/ai/batch/stop":
            batch["stop"] = True
            return self.send(json.dumps({"stopping": True}))
        if self.path.startswith("/api/ai?"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            k, kind = qs.get("k", [""])[0], qs.get("kind", [""])[0]
            if kind not in ("bidcheck", "draft"):
                return self.send(json.dumps({"error": "kind must be bidcheck or draft"}), code=400)
            if (ai_jobs.get(k) or {}).get("running"):
                return self.send(json.dumps({"error": "already running"}), code=409)
            threading.Thread(target=run_ai, args=(k, kind), daemon=True).start()
            return self.send(json.dumps({"started": True}))
        if self.path.startswith("/api/docs/save?"):
            k = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("k", [""])[0]
            try:
                return self.send(json.dumps(save_docs(k)))
            except Exception as e:
                return self.send(json.dumps({"error": f"Download halted: {e}"}), code=502)
        if self.path == "/api/stop":
            if job["proc"]:
                job["proc"].kill()
                job["error"] = "Stopped by you. Matches found so far are saved; Search now resumes."
            return self.send(json.dumps({"stopped": True}))
        if self.path == "/api/rescore":
            if job["running"]:
                return self.send(json.dumps({"error": "already running"}), code=409)
            threading.Thread(target=run_finder, args=(None, True), daemon=True).start()
            return self.send(json.dumps({"started": True}))
        if self.path.startswith("/api/run"):
            if job["running"]:
                return self.send(json.dumps({"error": "already running"}), code=409)
            days = None
            if "days=" in self.path:
                try:
                    days = max(1, min(60, int(self.path.split("days=")[1])))
                except ValueError:
                    pass
            threading.Thread(target=run_finder, args=(days,), daemon=True).start()
            return self.send(json.dumps({"started": True}))
        self.send("not found", "text/plain", 404)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"Dashboard: http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()

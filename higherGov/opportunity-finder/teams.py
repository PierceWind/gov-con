"""Post opportunity tables to Microsoft Teams through a Power Automate Workflows webhook.

Set TEAMS_WEBHOOK_URL in gov-con/.env.local (Teams channel or chat > Workflows >
"Post to a channel/chat when a webhook request is received"). Without it, nothing is sent.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import proposal
import teams_graph

ROWS_PER_CARD = 10  # Rich briefs need more room than the compact table did.
BRIEFS_PER_CARD = 2
MAX_TEXT = 420
BRIEF_INTRO = "HI Team, kindly check if we can bid on this. Thank you~"
BRIEF_FOOTER = "Let me know if this isn't applicable or already been suggested, thanks!"
_last_send = {"destination": "", "sent_keys": [], "sent_solicitations": [],
              "duplicates": [], "duplicate_solicitations": [], "unverified": [], "cards": 0}


def send_target():
    return (proposal.env_value("TEAMS_SEND_TARGET") or "gov").strip().lower()


def webhook(target=None):
    target = (target or send_target()).strip().lower()
    if target in {"test", "personal"}:
        return proposal.env_value("TEAMS_TEST_WEBHOOK_URL")
    return proposal.env_value("TEAMS_WEBHOOK_URL")


def duplicate_check_configured():
    return teams_graph.enabled() and teams_graph.configured()


def last_send_info():
    return {k: list(v) if isinstance(v, list) else v for k, v in _last_send.items()}


def _solicitation(row):
    return " ".join(str(row.get("Solicitation #") or row.get("Solicitation") or row.get("source_id") or "").split())


def _filter_duplicates(rows):
    if not teams_graph.enabled():
        return list(rows), [], []
    send, duplicates, unverified = [], [], []
    for row in rows:
        solicitation = _solicitation(row)
        if not solicitation:
            unverified.append(row)
        elif teams_graph.has_solicitation(solicitation):
            duplicates.append(row)
        else:
            send.append(row)
    strict = (proposal.env_value("TEAMS_DUPLICATE_CHECK_STRICT") or "on").lower() in {"1", "true", "on", "yes"}
    if not strict:
        send.extend(unverified)
        unverified = []
    return send, duplicates, unverified


def _verdict(r):
    b = r.get("bid") or {}
    return f"{b.get('verdict')} {b.get('score')}" if b.get("verdict") else "-"


def _cell(text, url=None, bold=False, color=None):
    tb = {"type": "TextBlock", "text": f"[{text}]({url})" if url else str(text or "-"), "wrap": True, "size": "Small"}
    if bold:
        tb["weight"] = "Bolder"
    if color:
        tb["color"] = color
    return {"type": "TableCell", "items": [tb]}


def _clip(value, limit=MAX_TEXT):
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _list_text(values, limit=MAX_TEXT):
    if isinstance(values, str):
        return _clip(values, limit)
    values = [" ".join(str(x).split()) for x in (values or []) if str(x).strip()]
    return _clip("; ".join(values), limit)


def _recommendation(r, bid):
    explicit = r.get("Recommendation") or r.get("Recommendation / decision")
    if explicit:
        return _clip(explicit)
    verdict = bid.get("verdict")
    if verdict:
        label = {"Bid": "GO", "Maybe": "REVIEW", "No bid": "NO-GO"}.get(verdict, verdict.upper())
        reason = bid.get("one_line")
        return _clip(f"{label} — {reason}" if reason else label)
    return "Not bid-checked yet"


def _brief_items(r):
    bid = r.get("bid") or {}
    items = [{"type": "TextBlock", "text": f"Recommendation: {_recommendation(r, bid)}",
              "wrap": True, "size": "Small", "spacing": "Small"}]
    summary = r.get("Summary") or bid.get("scope")
    if summary:
        items.append({"type": "TextBlock", "text": f"Summary: {_clip(summary)}",
                      "wrap": True, "size": "Small", "spacing": "None", "isSubtle": True})
    requirements = r.get("Key requirements") or r.get("Requirements") or bid.get("must_have")
    if requirements:
        items.append({"type": "TextBlock", "text": f"Key requirements: {_list_text(requirements)}",
                      "wrap": True, "size": "Small", "spacing": "None", "isSubtle": True})
    gaps = bid.get("gaps")
    if gaps:
        items.append({"type": "TextBlock", "text": f"Confirm before bidding: {_list_text(gaps, 300)}",
                      "wrap": True, "size": "Small", "spacing": "None", "color": "Warning"})
    deadline = bid.get("deadline") or r.get("Response Deadline")
    source = r.get("Source") or ""
    solicitation = r.get("Solicitation #") or ""
    meta = " · ".join(x for x in (f"Due: {_clip(deadline, 180)}" if deadline else "",
                                   f"Solicitation: {_clip(solicitation, 120)}" if solicitation else "",
                                   f"Source: {source}" if source else "") if x)
    if meta:
        items.append({"type": "TextBlock", "text": meta, "wrap": True, "size": "Small",
                      "spacing": "None", "isSubtle": True})
    return items


def _title_cell(r):
    title = r.get("Title") or "Untitled opportunity"
    link = r.get("Link") or r.get("HigherGov")
    tb = {"type": "TextBlock", "text": f"[{title}]({link})" if link else title,
          "wrap": True, "size": "Small", "weight": "Bolder"}
    return {"type": "TableCell", "items": [tb, *_brief_items(r)]}


def _first(r, *keys, default=""):
    for key in keys:
        value = r.get(key)
        if value not in (None, "", [], {}):
            return value
    return default


def _items(value):
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [str(x).strip() for x in (value or []) if str(x).strip()]


def _source_label(r):
    source = r.get("Source") or "Source not stated"
    link = r.get("Link") or r.get("HigherGov") or ""
    if source == "HigherGov" and "sam.gov" in link.lower():
        return "SAM.gov, found through HigherGov"
    return source


def _brief_value(r, bid, *keys, default="Not stated"):
    value = _first(r, *keys, default="")
    if value:
        return _clip(value, 700)
    return _clip(_first(bid, *keys, default=default), 700)


def _bullet_block(label, values, max_items=None, item_limit=360):
    values = _items(values)
    if max_items and len(values) > max_items:
        values = values[:max_items] + [f"({len(values) - max_items} additional items in the bid check)"]
    if not values:
        return [f"**{label}:** Not stated"]
    return [f"**{label}:**"] + [f"- {_clip(item, item_limit)}" for item in values]


def _brief_markdown(r, max_bullets=None):
    bid = r.get("bid") or {}
    title = _clip(r.get("Title") or "Untitled opportunity", 220)
    agency = _clip(r.get("Agency") or "Not stated", 220)
    solicitation = _clip(r.get("Solicitation #") or "Not stated", 240)
    location = _clip(r.get("Place of Performance") or "Not stated", 220)
    due = _brief_value(r, bid, "Due", "Response Deadline", "deadline")
    source = _source_label(r)
    link = r.get("Link") or r.get("HigherGov") or "Not stated"
    score = bid.get("score")
    reason = bid.get("one_line") or r.get("Recommendation") or "Not bid-checked yet."
    ai_view = f"Fit {score}/100, {_clip(reason, 900)}" if score is not None else _clip(reason, 900)
    scope = _first(r, "Summary", "Scope", default="") or bid.get("scope") or "Not stated"
    requirements = _first(r, "Key requirements", "Requirements", default="") or bid.get("must_have")
    gaps = _first(r, "Eligibility / gaps", "Eligibility", "Gaps", default="") or bid.get("gaps")
    red_flags = _first(r, "Risks / red flags", "Risks", "Red flags", default="") or bid.get("red_flags")
    competition = bid.get("competition") or r.get("Competition")
    price = bid.get("price_signal") or r.get("Price signal")
    questions = _first(r, "Questions due", "Questions Due", default="") or "Not stated"
    start = _first(r, "Start date", "Start Date", default="") or bid.get("start_date") or "Not stated"
    note = _first(r, "Note", "Notes", default="")
    lines = [
        f"## {title}",
        f"**Agency:** {agency}",
        f"**Solicitation:** {solicitation}",
        f"**Location:** {location}",
        f"**Due:** {due}",
        f"**Questions due:** {questions}",
        f"**Start date:** {start}",
        f"**Source:** {source}",
        f"**AI view:** {ai_view}",
        f"**Scope:** {_clip(scope, 1200)}",
    ]
    lines += _bullet_block("Key requirements", requirements, max_items=max_bullets)
    lines += _bullet_block("Eligibility / gaps", gaps, max_items=max_bullets)
    lines += _bullet_block("Risks / red flags", red_flags, max_items=max_bullets)
    if competition:
        lines.append(f"**Competition:** {_clip(competition, 900)}")
    if price:
        lines.append(f"**Price signal:** {_clip(price, 900)}")
    if note:
        lines.append(f"**Notes:** {_clip(note, 900)}")
    lines.append(f"**Official link:** {link}")
    return "\n".join(lines)


def brief_markdown(rows):
    """Return the copy/paste brief format used for Teams and email."""
    return "\n\n---\n\n".join([BRIEF_INTRO, *[_brief_markdown(r) for r in rows], BRIEF_FOOTER])


def _brief_card(title, subtitle, rows):
    body = [
        {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True},
        {"type": "TextBlock", "text": subtitle, "isSubtle": True, "wrap": True, "spacing": "None"},
        {"type": "TextBlock", "text": BRIEF_INTRO, "wrap": True, "spacing": "Medium"},
    ]
    for index, row in enumerate(rows):
        body.append({"type": "TextBlock", "text": _brief_markdown(row, max_bullets=6),
                     "wrap": True, "spacing": "Medium"})
        if index != len(rows) - 1:
            body.append({"type": "TextBlock", "text": "---", "isSubtle": True, "wrap": True})
    body.append({"type": "TextBlock", "text": BRIEF_FOOTER, "isSubtle": True, "wrap": True, "spacing": "Medium"})
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                         "type": "AdaptiveCard", "version": "1.5", "msteams": {"width": "Full"}, "body": body},
        }],
    }


def _card(title, subtitle, rows):
    head = ["Match", "Bid", "Title", "Agency", "Deadline", "Place", "Contact"]
    body_rows = [{"type": "TableRow", "style": "accent",
                  "cells": [_cell(h, bold=True) for h in head]}]
    for r in rows:
        v = (r.get("bid") or {}).get("verdict")
        body_rows.append({"type": "TableRow", "cells": [
            _cell(f"{r.get('Match %')}%"),
            _cell(_verdict(r), color={"Bid": "Good", "Maybe": "Warning", "No bid": "Attention"}.get(v)),
            _title_cell(r),
            _cell(r.get("Agency")),
            _cell(r.get("Response Deadline")),
            _cell(r.get("Place of Performance")),
            _cell(r.get("Contact")),
        ]})
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.5",
                "msteams": {"width": "Full"},
                "body": [
                    {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True},
                    {"type": "TextBlock", "text": subtitle, "isSubtle": True, "wrap": True, "spacing": "None"},
                    {"type": "Table", "gridStyle": "accent", "firstRowAsHeaders": True,
                     "columns": [{"width": w} for w in (1, 1, 5, 3, 2, 2, 3)], "rows": body_rows},
                ],
            },
        }],
    }


def send_rows(rows, title, subtitle="", format="table"):
    """Post rows as compact tables or detailed bid briefs after duplicate checks."""
    url = webhook()
    if not url:
        target = send_target()
        expected = "TEAMS_TEST_WEBHOOK_URL" if target in {"test", "personal"} else "TEAMS_WEBHOOK_URL"
        raise RuntimeError(f"No {expected} in gov-con/.env.local.")
    if not rows:
        return 0
    send, duplicates, unverified = _filter_duplicates(rows)
    _last_send.update(destination=send_target(),
                      sent_keys=[r.get("opp_key") or _solicitation(r) for r in send],
                      sent_solicitations=[_solicitation(r) for r in send if _solicitation(r)],
                      duplicates=[r.get("opp_key") or _solicitation(r) for r in duplicates],
                      duplicate_solicitations=[_solicitation(r) for r in duplicates if _solicitation(r)],
                      unverified=[r.get("opp_key") or _solicitation(r) or r.get("Title") or "Untitled opportunity" for r in unverified],
                      cards=0)
    if not send:
        return 0
    per_card = BRIEFS_PER_CARD if format == "brief" else ROWS_PER_CARD
    chunks = [send[i:i + per_card] for i in range(0, len(send), per_card)]
    for n, chunk in enumerate(chunks, 1):
        part = f" (part {n} of {len(chunks)})" if len(chunks) > 1 else ""
        payload = _brief_card(title + part, subtitle or f"{len(rows)} contracts", chunk) if format == "brief" else _card(title + part, subtitle or f"{len(rows)} contracts", chunk)
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                if r.status >= 300:
                    raise RuntimeError(f"Teams returned HTTP {r.status}")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Teams returned HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"Could not reach Teams: {e.reason}") from None
    _last_send["cards"] = len(chunks)
    return len(chunks)

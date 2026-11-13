"""Small Microsoft Graph client used to prevent duplicate Teams posts.

The existing Teams webhook can send messages but cannot read them. This module
uses a delegated Microsoft Graph access token to search Teams messages and
restricts matches to the configured Gov Contracting chat.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

import proposal

GRAPH = "https://graph.microsoft.com/v1.0"


class GraphConfigError(RuntimeError):
    pass


def enabled():
    return (proposal.env_value("TEAMS_DUPLICATE_CHECK") or "off").lower() in {"1", "true", "on", "yes"}


def access_token():
    return proposal.env_value("TEAMS_GRAPH_ACCESS_TOKEN")


def configured():
    return bool(access_token() and (proposal.env_value("TEAMS_SEARCH_CHAT_ID") or chat_name()))


def chat_name():
    return proposal.env_value("TEAMS_SEARCH_CHAT_NAME") or "Gov Contracting"


def _headers():
    token = access_token()
    if not token:
        raise GraphConfigError("Set TEAMS_GRAPH_ACCESS_TOKEN to enable Teams duplicate checking.")
    return {"Authorization": f"Bearer {token}", "Accept": "application/json", "Content-Type": "application/json"}


def _request(method, url, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        if exc.code in (401, 403):
            raise GraphConfigError(f"Microsoft Graph denied Teams message search (HTTP {exc.code}). Check delegated permissions and token expiry.") from None
        raise RuntimeError(f"Microsoft Graph returned HTTP {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach Microsoft Graph: {exc.reason}") from None


def _chat_id():
    explicit = proposal.env_value("TEAMS_SEARCH_CHAT_ID")
    if explicit:
        return explicit

    wanted = chat_name().casefold()
    url = f"{GRAPH}/me/chats?$top=50"
    while url:
        page = _request("GET", url)
        for chat in page.get("value") or []:
            labels = [chat.get("topic"), chat.get("displayName")]
            if any(str(label or "").strip().casefold() == wanted for label in labels):
                if chat.get("id"):
                    return chat["id"]
        url = page.get("@odata.nextLink")
    raise GraphConfigError(f"Could not find the Teams chat named {chat_name()!r}. Set TEAMS_SEARCH_CHAT_ID if needed.")


def has_solicitation(solicitation):
    """Return True when the solicitation number exists in the target Teams chat."""
    value = " ".join(str(solicitation or "").split())
    if not value:
        return False
    target_chat = _chat_id()
    payload = {"requests": [{"entityTypes": ["chatMessage"],
                              "query": {"queryString": f'"{value}"'},
                              "from": 0, "size": 25}]}
    result = _request("POST", f"{GRAPH}/search/query", payload)
    for container in result.get("value") or []:
        for hit in container.get("hitsContainers") or []:
            for item in hit.get("hits") or []:
                resource = item.get("resource") or {}
                if resource.get("chatId") == target_chat:
                    return True
    return False

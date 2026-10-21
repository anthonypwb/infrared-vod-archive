"""Small, dependency-free shared primitives. Never log remote response bodies."""
import json
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


class ArchiveError(Exception):
    pass


def utcnow():
    return datetime.now(timezone.utc)


def timestamp():
    return utcnow().isoformat()


def valid_uuid(value):
    import uuid
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def allowed_url(url, hosts):
    p = urlparse(url)
    if (p.scheme != "https" or p.username or p.password or p.port not in (None, 443)
            or not any(p.hostname == h or (h.startswith(".") and (p.hostname or "").endswith(h)) for h in hosts)):
        raise ArchiveError("Unexpected network destination")
    return url


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ArchiveError("Unexpected redirect; request stopped")


def request(url, *, method="GET", headers=None, data=None, limit=8_000_000, timeout=30):
    req = Request(url, method=method, headers=headers or {}, data=data)
    try:
        response = build_opener(NoRedirect()).open(req, timeout=timeout)
    except HTTPError as exc:
        response = exc
    with response:
        body = response.read(limit + 1)
        if len(body) > limit:
            raise ArchiveError("Response exceeds configured size limit")
        return response.code, dict(response.headers.items()), body


def json_request(url, **kwargs):
    status, headers, body = request(url, **kwargs)
    if status not in (200, 201):
        raise ArchiveError(f"API request failed: HTTP {status}")
    return json.loads(body), headers


def read_config():
    config = json.loads(Path("config/archive.json").read_text())
    if (config["destination_handle"] != "@archiveinfrared" or config["source_channel"] != "infrared"
            or config["source_channel_id"] != 129849):
        raise ArchiveError("Source or destination configuration changed; inspect before running")
    return config


def reserve(state, key, minutes, cap, now=None):
    month = (now or utcnow()).strftime("%Y-%m")
    ledger = state.setdefault("budget", {}).setdefault(month, {})
    if key in ledger:
        raise ArchiveError("Duplicate budget reservation")
    if sum(ledger.values()) + minutes > cap:
        raise ArchiveError("Monthly archive runtime allowance reached")
    ledger[key] = minutes
    return month


def settle(state, month, key, elapsed_seconds):
    # Estimate rounded job minutes, including 30 seconds for checkout/teardown.
    # This is a project allowance, not a substitute for account billing limits.
    ledger = state["budget"][month]
    ledger[key] = min(ledger[key], max(1, math.ceil((elapsed_seconds + 30) / 60)))


BASE_TAGS = [
    "Haz",
    "Haz Al-Din",
    "Haz Al Din",
    "InfraHaz",
    "Infrared",
    "Infrared Show",
    "American Communist Party",
    "ACP",
    "Communism",
    "Marxism",
    "Marxism-Leninism",
    "Communist Party",
    "Political Commentary",
    "Political Philosophy",
    "American Politics",
    "MAGA Communism",
    "Infrared livestream",
    "full livestream"
]


def upload_metadata(vod):
    clean_title = re.sub(r"[<>\x00-\x1f]", "", vod["title"]).strip()
    title = f"{vod['date']} | {clean_title} | Infrared Archive"[:100]
    return {
        "snippet": {"title": title, "categoryId": "25", "tags": list(BASE_TAGS), "description":
            "Full Infrared Show livestream with Haz Al-Din.\n\n"
            f"Stream: {clean_title}\nDate: {vod['date']}\n\n"
            "Watch Infrared: https://kick.com/infrared\n"
            f"Original VOD: {vod['source_url']}\n\n"
            f"Archive source ID: {vod['id']}\n"},
        "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
    }

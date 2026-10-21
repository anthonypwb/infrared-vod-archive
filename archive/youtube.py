"""OAuth-backed resumable upload. Session URLs/tokens never enter durable state."""
import json
import os
import re
import time
from urllib.parse import urlencode
from urllib.error import URLError
from .core import ArchiveError, allowed_url, json_request, request

API = "https://www.googleapis.com/youtube/v3/"
CHUNK = 8 * 1024 * 1024


def duration_seconds(value):
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", value)
    if not match:
        raise ArchiveError("Unexpected YouTube duration")
    hours, minutes, seconds = (float(v or 0) for v in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def next_offset(headers, size):
    value = next((v for k, v in headers.items() if k.lower() == "range"), None)
    if value is None:
        return 0
    match = re.fullmatch(r"bytes=0-(\d+)", value)
    if not match or int(match[1]) >= size:
        raise ArchiveError("Unexpected resumable upload offset")
    return int(match[1]) + 1


class YouTube:
    def __init__(self):
        self.access_token = None
        self.expires = 0
        self.channel_id = None
        self.uploads_playlist = None

    def auth(self):
        if time.monotonic() >= self.expires:
            fields = {"grant_type": "refresh_token", "client_id": os.environ["YOUTUBE_CLIENT_ID"],
                      "client_secret": os.environ["YOUTUBE_CLIENT_SECRET"],
                      "refresh_token": os.environ["YOUTUBE_REFRESH_TOKEN"]}
            data, _ = json_request("https://oauth2.googleapis.com/token", method="POST",
                                   headers={"Content-Type": "application/x-www-form-urlencoded"},
                                   data=urlencode(fields).encode())
            self.access_token = data["access_token"]
            self.expires = time.monotonic() + max(30, int(data["expires_in"]) - 120)
        return {"Authorization": "Bearer " + self.access_token}

    def get(self, resource, **params):
        result, _ = json_request(API + resource + "?" + urlencode(params), headers=self.auth())
        return result

    def verify_channel(self, expected_id, handle):
        if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", expected_id or ""):
            raise ArchiveError("Expected archive channel ID has not been configured")
        mine = self.get("channels", part="id,contentDetails,status", mine="true").get("items", [])
        handle_items = self.get("channels", part="id", forHandle=handle).get("items", [])
        if (len(mine) != 1 or mine[0]["id"] != expected_id
                or len(handle_items) != 1 or handle_items[0]["id"] != expected_id):
            raise ArchiveError("OAuth account does not match @archiveinfrared")
        if mine[0].get("status", {}).get("longUploadsStatus") != "allowed":
            raise ArchiveError("YouTube channel is not enabled for long uploads")
        self.channel_id = expected_id
        self.uploads_playlist = mine[0]["contentDetails"]["relatedPlaylists"]["uploads"]

    def recent_match(self, vod_id):
        token, matches = None, []
        for _ in range(3):
            params = {"part": "snippet,contentDetails", "playlistId": self.uploads_playlist, "maxResults": 50}
            if token:
                params["pageToken"] = token
            result = self.get("playlistItems", **params)
            for item in result.get("items", []):
                description = item.get("snippet", {}).get("description", "")
                if f"Archive source ID: {vod_id}" in description.splitlines():
                    matches.append(item["contentDetails"]["videoId"])
            token = result.get("nextPageToken")
            if not token:
                break
        if len(set(matches)) > 1:
            raise ArchiveError("Multiple uploads carry this source ID; manual reconciliation required")
        return matches[0] if matches else None

    def upload(self, path, metadata, gate):
        if not self.channel_id:
            raise ArchiveError("Destination identity was not checked")
        size = path.stat().st_size
        gate()
        # The caller checkpoints 'uploading' BEFORE this non-idempotent request.
        status, headers, _ = request(
            "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status&notifySubscribers=false",
            method="POST", headers={**self.auth(), "Content-Type": "application/json; charset=UTF-8",
                                     "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)},
            data=json.dumps(metadata).encode())
        if status not in (200, 201):
            raise ArchiveError(f"Upload initialization failed: HTTP {status}; reconcile before retry")
        session = next((v for k, v in headers.items() if k.lower() == "location"), None)
        if not session:
            raise ArchiveError("Upload session missing; reconcile before retry")
        allowed_url(session, ("www.googleapis.com", "youtube.googleapis.com"))
        offset, failures = 0, 0
        with path.open("rb") as source:
            while offset < size:
                gate()
                source.seek(offset)
                block = source.read(CHUNK)
                try:
                    code, response_headers, body = request(session, method="PUT", data=block, timeout=60,
                        headers={**self.auth(), "Content-Type": "video/mp4", "Content-Length": str(len(block)),
                                 "Content-Range": f"bytes {offset}-{offset + len(block) - 1}/{size}"})
                except (OSError, URLError):
                    code, response_headers, body = 503, {}, b""
                if code in (200, 201):
                    result = json.loads(body)
                    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", result.get("id", "")):
                        raise ArchiveError("Upload completed without a valid video ID")
                    return result["id"]
                if code == 308:
                    new_offset = next_offset(response_headers, size)
                    failures = failures + 1 if new_offset <= offset else 0
                    if failures > 5:
                        raise ArchiveError("Resumable upload made no progress")
                    offset = new_offset
                    continue
                if code not in (401, 429, 500, 502, 503, 504) or failures >= 5:
                    raise ArchiveError(f"Upload interrupted: HTTP {code}; reconcile before retry")
                failures += 1
                if code == 401:
                    self.expires = 0
                time.sleep(min(2 ** failures, 30))
                gate()
                # Query the existing session; never create a second upload on retry.
                code, response_headers, body = request(session, method="PUT", data=b"", headers={
                    **self.auth(), "Content-Length": "0", "Content-Range": f"bytes */{size}"})
                if code in (200, 201):
                    result = json.loads(body)
                    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", result.get("id", "")):
                        raise ArchiveError("Upload completed without a valid video ID")
                    return result["id"]
                if code == 308:
                    offset = next_offset(response_headers, size)
                elif code not in (429, 500, 502, 503, 504):
                    raise ArchiveError("Upload status unknown; reconcile before retry")
        raise ArchiveError("Upload completion was not confirmed")

    def verify_video(self, vod):
        # This ID is saved from the upload response or a source-marker match.
        # Descriptions remain editable after that durable association is made.
        if not self.channel_id or not re.fullmatch(r"[A-Za-z0-9_-]{11}", vod.get("youtube_id", "")):
            raise ArchiveError("Upload verification requires a saved video ID and checked channel")
        items = self.get("videos", part="snippet,status,processingDetails,contentDetails", id=vod["youtube_id"]).get("items", [])
        if len(items) != 1:
            return "pending", "Upload not yet visible through the API"
        item = items[0]
        snippet, status = item["snippet"], item["status"]
        if item.get("id") != vod["youtube_id"] or snippet.get("channelId") != self.channel_id:
            raise ArchiveError("Uploaded video identity mismatch")
        source_markers = [line.strip() for line in snippet.get("description", "").splitlines()
                          if line.strip().startswith("Archive source ID:")]
        if any(marker != f"Archive source ID: {vod['id']}" for marker in source_markers):
            raise ArchiveError("Uploaded video has a conflicting archive source ID")
        processing = item.get("processingDetails", {}).get("processingStatus")
        if processing in ("failed", "terminated") or status.get("uploadStatus") in ("failed", "rejected", "deleted"):
            return "blocked", "YouTube rejected or failed to process the upload"
        if processing != "succeeded" or status.get("uploadStatus") != "processed":
            return "pending", "YouTube is still processing the upload"
        if status.get("privacyStatus") != "public":
            return "blocked", "YouTube did not make the upload public; inspect channel/API restrictions"
        actual = duration_seconds(item["contentDetails"]["duration"])
        expected = vod["media"]["duration_seconds"]
        if abs(actual - expected) > max(3, expected * .001):
            return "blocked", "YouTube duration does not match the complete source"
        return "archived", "Public upload and full duration verified"

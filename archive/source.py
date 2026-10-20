"""Discover exact Infrared transcript links and copy complete, unencrypted HLS."""
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse
from .core import ArchiveError, allowed_url, request, utcnow, valid_uuid

SITE = "https://infrared-transcripts.pages.dev"
MEDIA_HOSTS = ("kick.com", ".kick.com", ".cloudfront.net")
HEADERS = {"User-Agent": "InfraredArchive/1.0", "Referer": "https://kick.com/"}
DATE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")


def ensure_media_tools():
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return
    # Install only for an actual transfer, after its runtime reservation is saved.
    # Polling and verification runs remain dependency-free.
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise ArchiveError("Install FFmpeg and FFprobe before running media transfer")
    for command in (["sudo", "apt-get", "update", "-qq"],
                    ["sudo", "apt-get", "install", "-y", "--no-install-recommends", "ffmpeg"]):
        result = subprocess.run(command, capture_output=True, timeout=180)
        if result.returncode:
            raise ArchiveError("FFmpeg installation failed on the archive runner")


def page_url(vod_id):
    if not valid_uuid(vod_id):
        raise ArchiveError("Invalid source VOD ID")
    return f"{SITE}/en/kick/infrared/{vod_id}/"


def get_text(url, media=False):
    allowed_url(url, MEDIA_HOSTS if media else ("infrared-transcripts.pages.dev",))
    status, _, body = request(url, headers=HEADERS)
    if status != 200:
        raise ArchiveError(f"Source unavailable: HTTP {status}")
    return body.decode("utf-8")


class Inventory(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = {}
        self.current = None
        self.frames = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("li", "tr", "article"):
            self.frames.append({"tag": tag, "text": [], "ids": []})
        if tag == "a":
            href = urljoin(SITE, attrs.get("href", ""))
            match = re.fullmatch(re.escape(SITE) + r"/en/kick/infrared/([0-9a-f-]{36})/?", href)
            if match and valid_uuid(match[1]):
                self.current = match[1]
                self.links.setdefault(self.current, {"id": self.current, "title": "", "date": None,
                                                     "source_url": page_url(self.current)})
                for frame in self.frames:
                    frame["ids"].append(self.current)

    def handle_data(self, text):
        for frame in self.frames:
            frame["text"].append(text)
        if self.current:
            self.links[self.current]["title"] += text

    def handle_endtag(self, tag):
        if tag == "a":
            self.current = None
        if self.frames and self.frames[-1]["tag"] == tag:
            frame = self.frames.pop()
            dates = set(DATE.findall(" ".join(frame["text"])))
            if len(dates) == 1 and len(set(frame["ids"])) == 1:
                self.links[frame["ids"][0]]["date"] = dates.pop()


def parse_inventory(html):
    parser = Inventory()
    parser.feed(html)
    for vod in parser.links.values():
        vod["title"] = " ".join(vod["title"].split())
        dates = DATE.findall(vod["title"])
        if not vod["date"] and len(set(dates)) == 1:
            vod["date"] = dates[0]
    return list(parser.links.values())


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.video = False
        self.h1 = False
        self.title = ""
        self.dates = set()
        self.sources = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "video":
            self.video = True
        if tag == "h1":
            self.h1 = True
            self.title = ""
        if (tag == "video" or (tag == "source" and self.video)) and attrs.get("src"):
            self.sources.add(attrs["src"])
        if tag == "time" and attrs.get("datetime"):
            self.dates.update(DATE.findall(attrs["datetime"]))
        if tag == "meta" and (attrs.get("property") == "article:published_time"
                              or attrs.get("name") in ("date", "datePublished")):
            self.dates.update(DATE.findall(attrs.get("content", "")))

    def handle_endtag(self, tag):
        if tag == "video":
            self.video = False
        if tag == "h1":
            self.h1 = False

    def handle_data(self, text):
        if self.h1:
            self.title += text


def read_page(vod):
    parser = Page()
    parser.feed(get_text(page_url(vod["id"])))
    if parser.title.strip():
        vod["title"] = " ".join(parser.title.split())
    if not vod.get("date") and len(parser.dates) == 1:
        vod["date"] = next(iter(parser.dates))
    if len(parser.sources) != 1:
        raise ArchiveError("Source page has absent or ambiguous video")
    master = allowed_url(next(iter(parser.sources)), MEDIA_HOSTS)
    if not urlparse(master).path.endswith(".m3u8"):
        raise ArchiveError("Unsupported source format")
    return master


def eligible_date(value, max_age, today=None):
    if not value:
        return False
    try:
        age = ((today or utcnow().date()) - date.fromisoformat(value)).days
        return 0 <= age <= max_age
    except ValueError:
        return False


def resolve(master):
    text = get_text(master, media=True)
    if not text.startswith("#EXTM3U"):
        raise ArchiveError("Not an HLS playlist")
    if "#EXT-X-STREAM-INF" not in text:
        return master, text
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    variants = []
    for i, line in enumerate(lines[:-1]):
        if line.startswith("#EXT-X-STREAM-INF:"):
            # This implementation requires muxed audio/video, as used by Kick.
            if re.search(r'(?:^|,)AUDIO=', line.split(":", 1)[1]):
                continue
            bandwidth = re.search(r"(?:^|,)BANDWIDTH=(\d+)", line.split(":", 1)[1])
            resolution = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
            if bandwidth and resolution and not lines[i + 1].startswith("#"):
                variants.append((int(resolution[1]) * int(resolution[2]), int(bandwidth[1]),
                                 allowed_url(urljoin(master, lines[i + 1]), MEDIA_HOSTS)))
    if not variants:
        raise ArchiveError("No supported muxed source-quality rendition")
    selected = max(variants)[2]
    return selected, get_text(selected, media=True)


def parse_playlist(text, base):
    if not text.startswith("#EXTM3U") or "#EXT-X-ENDLIST" not in text:
        raise ArchiveError("Live or incomplete source playlist")
    unsupported = ("#EXT-X-KEY", "#EXT-X-BYTERANGE", "#EXT-X-DISCONTINUITY", "#EXT-X-MAP", "#EXT-X-GAP")
    if any(item in text for item in unsupported):
        raise ArchiveError("Unsupported HLS layout; manual handling required")
    rows, duration = [], None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            duration = float(line.split(":", 1)[1].split(",")[0])
            if not math.isfinite(duration) or not 0 < duration <= 120:
                raise ArchiveError("Invalid segment duration")
        elif line and not line.startswith("#"):
            if duration is None:
                raise ArchiveError("Segment missing duration")
            rows.append({"duration": duration, "url": allowed_url(urljoin(base, line), MEDIA_HOSTS)})
            duration = None
    if not rows or duration is not None:
        raise ArchiveError("Empty or truncated source playlist")
    return rows


def probe_file(path, expected):
    completed = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
                               capture_output=True, timeout=30)
    if completed.returncode:
        raise ArchiveError("Output media probe failed")
    info = json.loads(completed.stdout)
    streams = {item["codec_type"]: item for item in info["streams"]}
    if not all(kind in streams for kind in ("video", "audio")):
        raise ArchiveError("Output is missing audio or video")
    actual = float(info["format"]["duration"])
    if not math.isfinite(actual) or abs(actual - expected) > max(2, expected * .001):
        raise ArchiveError("Source and output durations differ")
    # Decode short beginning/middle/end samples without a full-stream re-encode.
    for start in (0, max(0, actual / 2 - 3), max(0, actual - 6)):
        sample = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-ss", str(start),
                                 "-i", str(path), "-t", "6", "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"],
                                capture_output=True, timeout=45)
        if sample.returncode:
            raise ArchiveError("Media sample decode failed")
    return {"duration_seconds": actual, "width": streams["video"]["width"],
            "height": streams["video"]["height"], "audio_codec": streams["audio"]["codec_name"],
            "video_codec": streams["video"]["codec_name"], "sample_decode_passed": True}


def acquire(rows, path, config, gate):
    """Feed checked TS segments to ffmpeg over stdin, retaining one output file."""
    expected = sum(row["duration"] for row in rows)
    if expected > config["max_source_duration_seconds"]:
        raise ArchiveError("Source exceeds YouTube duration limit")
    free = shutil.disk_usage(path.parent).free
    cap = min(config["max_output_bytes"], free - config["disk_reserve_bytes"])
    if cap <= 0:
        raise ArchiveError("Insufficient temporary disk space")
    digest = hashlib.sha256()
    with tempfile.TemporaryFile() as err:
        process = subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-y",
                                    "-protocol_whitelist", "pipe", "-f", "mpegts", "-i", "pipe:0",
                                    "-map", "0:v:0", "-map", "0:a:0", "-c", "copy", "-f", "mp4", str(path)],
                                   stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err)
        total = 0
        try:
            for row in rows:
                gate()
                status, _, body = request(row["url"], headers=HEADERS, limit=100_000_000)
                if status != 200 or not body:
                    raise ArchiveError(f"Source segment unavailable: HTTP {status}")
                # Reject HTML/error bodies and unsupported media instead of silently skipping.
                if len(body) < 188 or body[0] != 0x47 or (len(body) > 188 and body[188] != 0x47):
                    raise ArchiveError("Source segment is not MPEG-TS")
                total += len(body)
                if total > cap or (path.exists() and path.stat().st_size > cap):
                    raise ArchiveError("Full-quality source exceeds temporary storage limit")
                if shutil.disk_usage(path.parent).free < config["disk_reserve_bytes"]:
                    raise ArchiveError("Temporary disk reserve reached")
                digest.update(body)
                process.stdin.write(body)
            process.stdin.close()
            if process.wait(timeout=90):
                raise ArchiveError("Lossless remux failed")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    if path.stat().st_size > cap:
        raise ArchiveError("Output exceeds temporary storage limit")
    return {**probe_file(path, expected), "source_sha256": digest.hexdigest(),
            "source_bytes": total, "output_bytes": path.stat().st_size, "segments": len(rows)}

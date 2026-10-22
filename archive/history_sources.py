"""Strict public-source parsing for historical imports. No access workarounds."""
import hashlib
import html
import json
import math
import re
import shutil
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from urllib.request import Request, build_opener
from .core import ArchiveError, NoRedirect, allowed_url, request
from .source import probe_file

SITE = 'https://infrared-transcripts.pages.dev'
CHANNELS = ('https://rumble.com/c/InfraredArchive', 'https://rumble.com/user/swaggghili')
HEADERS = {'User-Agent': 'InfraredArchive/1.0'}
DATE = re.compile(r'(?<!\d)(20\d{2})[-./](\d{1,2})[-./](\d{1,2})(?!\d)')
MEDIA_HOSTS = ('rumble.com', '.rumble.com', '1a-1791.com', '.1a-1791.com', 'rmbl.ws', '.rmbl.ws')


def fetch(url, hosts, missing=False):
    allowed_url(url, hosts)
    status, _, body = request(url, headers=HEADERS, limit=12_000_000, timeout=30)
    if status == 404 and missing: return None
    if status != 200: raise ArchiveError(f'Source access failed: HTTP {status}')
    text = body.decode('utf8')
    if '<title>Just a moment' in text or 'cf-chl-' in text:
        raise ArchiveError('Source requires browser verification; no bypass attempted')
    return text


def dates(text):
    found = set()
    for y, m, d in DATE.findall(text):
        try: found.add(date(int(y), int(m), int(d)).isoformat())
        except ValueError: pass
    for m, d, y in re.findall(r'(?<!\d)(\d{1,2})/(\d{1,2})/(20\d{2})(?!\d)', text):
        try: found.add(date(int(y), int(m), int(d)).isoformat())
        except ValueError: pass
    return found


def title_key(title):
    title = DATE.sub(' ', title)
    title = re.sub(r'(?<!\d)\d{1,2}/\d{1,2}/20\d{2}(?!\d)', ' ', title)
    return ' '.join(re.findall(r'[^\W_]+', html.unescape(title).casefold()))


def identity(day, title):
    return hashlib.sha256((day + '\n' + title_key(title)).encode()).hexdigest()


class Links(HTMLParser):
    def __init__(self, rumble=False):
        super().__init__(); self.rumble = rumble; self.current = None; self.rows = []
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag != 'a': return
        href = a.get('href') or ''
        valid = ('videostream__link' in (a.get('class') or '').split() if self.rumble else
                 bool(re.fullmatch(r'/en/(?:kick|youtube)/[a-z0-9-]+/[a-zA-Z0-9_-]+/?', href)))
        if valid: self.current = {'url': href, 'title': a.get('title', ''), 'text': []}
    def handle_data(self, value):
        if self.current: self.current['text'].append(value)
    def handle_endtag(self, tag):
        if tag == 'a' and self.current:
            row = self.current; self.current = None
            row['title'] = row['title'] or ' '.join(' '.join(row.pop('text')).split())
            row.pop('text', None); self.rows.append(row)


def transcript_index(html_text):
    parser = Links(); parser.feed(html_text)
    output = {}
    for row in parser.rows:
        # The leading date is often a YouTube repost date. A date in the title wins.
        match = re.match(r'^(20\d{2}-\d{2}-\d{2})\s+(.+)$', row['title'])
        if not match: continue
        title = match[2]; embedded = dates(title)
        if len(embedded) > 1: continue
        day = next(iter(embedded)) if embedded else match[1]
        if not title_key(title): continue
        url = urljoin(SITE, row['url']).rstrip('/') + '/'
        output[url] = {'url': url, 'title': title, 'date': day, 'key': identity(day, title)}
    if not output: raise ArchiveError('Transcript index is absent or its layout changed')
    return list(output.values())


def catalog(config, gate):
    if tuple(config['channels']) != CHANNELS: raise ArchiveError('Historical source allowlist changed')
    output = {}
    for channel in CHANNELS:
        seen_pages = set(); completed = False
        for number in range(1, config['max_catalog_pages_per_channel'] + 1):
            gate(); text = fetch(f'{channel}?page={number}', ('rumble.com',), missing=number > 1)
            if text is None:
                completed = True; break
            parser = Links(rumble=True); parser.feed(text)
            if not parser.rows:
                # Never interpret a challenge, malformed page, or JS shell as an empty catalog.
                raise ArchiveError('Rumble catalog has no verifiable video entries')
            page_urls = []
            for row in parser.rows:
                url = urljoin('https://rumble.com', row['url']).split('?')[0]
                allowed_url(url, ('rumble.com',))
                if not re.fullmatch(r'/v[a-z0-9]+-[^/]+\.html', urlparse(url).path):
                    raise ArchiveError('Unexpected Rumble video link')
                day = dates(row['title'])
                output.setdefault(url, {'source_url': url, 'source_channel_url': channel,
                                       'title': row['title'],
                                       'date': next(iter(day)) if len(day) == 1 else None})
                page_urls.append(url)
            digest = hashlib.sha256('\n'.join(page_urls).encode()).hexdigest()
            if digest in seen_pages: raise ArchiveError('Rumble pagination repeated; chronology is not verified')
            seen_pages.add(digest)
        if not completed: raise ArchiveError('Rumble catalog page limit reached; chronology is not verified')
    return list(output.values())


def match_transcript(row, index):
    key = title_key(row['title'])
    matches = [t for t in index if title_key(t['title']) == key
               and (row.get('date') is None or row['date'] == t['date'])]
    # An exact title without a source date is allowed only when the index has one unique match.
    return matches[0] if len(matches) == 1 else None


def clock(value):
    h, m, s = map(int, value.split(':'))
    if m >= 60 or s >= 60: raise ArchiveError('Invalid transcript timestamp')
    return h * 3600 + m * 60 + s


class Transcript(HTMLParser):
    def __init__(self):
        super().__init__(); self.stack = []; self.segments = []; self.title = []; self.day = []
    def handle_starttag(self, tag, attrs):
        a = dict(attrs); frame = {'tag': tag, 'kind': None, 'text': []}
        if tag == 'h1' and a.get('data-pagefind-meta') == 'title': frame['kind'] = 'title'
        if a.get('data-pagefind-meta') == 'date': frame['kind'] = 'date'
        if 'transcript-item' in (a.get('class') or '').split():
            match = re.fullmatch(r'(\d{2,}:\d{2}:\d{2})-(\d{2,}:\d{2}:\d{2})', a.get('title', ''))
            if not match: raise ArchiveError('Transcript item has no complete timestamp range')
            frame.update(kind='segment', start=clock(match[1]), end=clock(match[2]))
        if tag not in ('meta','link','img','br','hr','input','source','track','wbr'):
            self.stack.append(frame)
    def handle_data(self, value):
        for frame in self.stack:
            if frame['kind']: frame['text'].append(value)
    def handle_endtag(self, tag):
        if not self.stack: return
        if self.stack[-1]['tag'] != tag: return
        frame = self.stack.pop(); text = ' '.join(' '.join(frame['text']).split())
        if frame['kind'] == 'title': self.title.append(text)
        if frame['kind'] == 'date': self.day.append(text)
        if frame['kind'] == 'segment':
            self.segments.append({'start': frame['start'], 'end': frame['end'], 'text': text})


def parse_transcript(text, expected):
    p = Transcript(); p.feed(text)
    if len(p.title) != 1 or title_key(p.title[0]) != title_key(expected['title']):
        raise ArchiveError('Transcript title does not match the source')
    if len(p.day) != 1: raise ArchiveError('Transcript date missing or ambiguous')
    embedded = dates(p.title[0]); days = embedded or dates(p.day[0])
    if days != {expected['date']}: raise ArchiveError('Transcript date does not match the source')
    if not p.segments or any(f['kind'] == 'segment' for f in p.stack):
        raise ArchiveError('Transcript missing or incomplete')
    previous = -1
    for item in p.segments:
        if item['start'] < previous or item['end'] < item['start'] or not item['text']:
            raise ArchiveError('Transcript timeline malformed')
        previous = item['start']
    canonical = json.dumps(p.segments, sort_keys=True, ensure_ascii=False)
    return {'segments': p.segments, 'sha256': hashlib.sha256(canonical.encode()).hexdigest(),
            'url': expected['url'], 'title': p.title[0], 'date': expected['date']}


def check_coverage(transcript, duration, config):
    parts = transcript['segments']
    if not math.isfinite(duration) or duration <= 0: raise ArchiveError('Source duration unavailable')
    if parts[0]['start'] > config['max_transcript_start_seconds']:
        raise ArchiveError('Transcript does not cover the beginning')
    if abs(duration - max(x['end'] for x in parts)) > config['max_transcript_tail_seconds']:
        raise ArchiveError('Transcript and video lengths do not match')
    previous_end = 0
    for item in parts:
        if item['start'] - previous_end > config['max_transcript_gap_seconds']:
            raise ArchiveError('Transcript contains a large unverified gap')
        previous_end = max(previous_end, item['end'])
    words = sum(len(x['text'].split()) for x in parts)
    if words < duration / 60 * config['minimum_words_per_minute']:
        raise ArchiveError('Transcript is too sparse to screen the full recording')


def inspect_video(row):
    page = fetch(row['source_url'], ('rumble.com',))
    embed_ids = set(re.findall(r'rumble\.com/embed/(?:[0-9a-z]+\.)?(v[0-9a-z]+)', page))
    embed_ids.update(re.findall(r'Rumble\(\s*"play"\s*,\s*\{[^}]*[\'"]?video[\'"]?\s*:\s*[\'"](v[0-9a-z]+)', page))
    if len(embed_ids) != 1: raise ArchiveError('Rumble player identity is missing or ambiguous')
    embed = next(iter(embed_ids))
    raw = fetch(f'https://rumble.com/embedJS/u3/?request=video&ver=2&v={embed}', ('rumble.com',))
    info = json.loads(raw)
    if info.get('live') != 0: raise ArchiveError('Rumble recording is not a completed video')
    author = (info.get('author') or {}).get('url', '').rstrip('/')
    if author != row['source_channel_url']: raise ArchiveError('Rumble uploader identity mismatch')
    title = html.unescape(info.get('title', ''))
    if title_key(title) != title_key(row['title']): raise ArchiveError('Rumble title changed after discovery')
    duration = float(info.get('duration', 0))
    if not math.isfinite(duration) or duration <= 0: raise ArchiveError('Rumble duration is missing')
    formats = []
    for kind, values in (info.get('ua') or {}).items():
        if kind in ('tar', 'timeline', 'audio', 'hls'): continue
        for f in (values.values() if isinstance(values, dict) else values):
            meta = f.get('meta') or {}; url = f.get('url', '')
            if urlparse(url).path.endswith('.mp4') and meta.get('h') and meta.get('w'):
                allowed_url(url, MEDIA_HOSTS)
                formats.append({'url': url, 'width': int(meta['w']), 'height': int(meta['h']),
                                'size': int(meta.get('size') or 0)})
    if not formats: raise ArchiveError('No verified direct MP4 format')
    best = max(formats, key=lambda x:(x['width']*x['height'], x['size']))
    return {'duration': duration, 'embed_id': embed, 'format': best}


def download(video, path, config, gate):
    expected = video['duration']; fmt = video['format']
    if expected > config['max_source_duration_seconds']: raise ArchiveError('Historical source is too long')
    cap = min(config['max_output_bytes'], shutil.disk_usage(path.parent).free - config['disk_reserve_bytes'])
    if cap <= 0 or fmt['size'] > cap: raise ArchiveError('Historical source exceeds storage allowance')
    allowed_url(fmt['url'], MEDIA_HOSTS)
    digest = hashlib.sha256(); count = 0
    with build_opener(NoRedirect()).open(Request(fmt['url'], headers=HEADERS), timeout=30) as src:
        if src.status != 200: raise ArchiveError('Historical media download failed')
        length = int(src.headers.get('Content-Length') or 0)
        if not length or length > cap: raise ArchiveError('Historical download has no bounded full length')
        with path.open('wb') as out:
            while True:
                gate(); block = src.read(4*1024*1024)
                if not block: break
                count += len(block)
                if count > cap or shutil.disk_usage(path.parent).free < config['disk_reserve_bytes']:
                    raise ArchiveError('Historical download storage limit reached')
                digest.update(block); out.write(block)
    if count != length or (fmt['size'] and abs(count - fmt['size']) > 1024):
        raise ArchiveError('Historical media download is incomplete')
    return {**probe_file(path, expected), 'source_sha256': digest.hexdigest(),
            'source_bytes': count, 'output_bytes': count}

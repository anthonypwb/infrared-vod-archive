"""Read-only source access check. Never downloads media or contacts YouTube."""
import json
from html.parser import HTMLParser
from .core import request


class Probe(HTMLParser):
    def __init__(self):
        super().__init__()
        self.videos = []
        self.links = []
        self.title = False
        self.text = []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'title': self.title = True
        if tag == 'a':
            href = attrs.get('href', '')
            if 'videostream__link' in attrs.get('class', '').split():
                self.videos.append(href)
            if 'page=' in href: self.links.append(href)
    def handle_data(self, text):
        if self.title: self.text.append(text)
    def handle_endtag(self, tag):
        if tag == 'title': self.title = False


def main():
    for url in ('https://rumble.com/c/InfraredArchive?page=1',
                'https://rumble.com/user/swaggghili?page=1',
                'https://infrared-transcripts.pages.dev/en/introduction/'):
        try:
            code, _, body = request(url, headers={'User-Agent': 'InfraredArchive/1.0'}, timeout=25)
            p = Probe(); p.feed(body.decode('utf8', 'replace'))
            print(json.dumps({'url': url, 'http_status': code, 'bytes': len(body),
                             'title': ''.join(p.text), 'video_count': len(p.videos),
                             'examples': p.videos[:3], 'pagination': p.links[-5:]}))
            if code == 200 and p.videos:
                # Small structural sample to build a parser from observed HTML.
                text = body.decode('utf8')
                pos = text.find('videostream__link')
                print('CARD_SAMPLE', text[max(0,pos-250):pos+1700])
        except Exception as exc:
            print(json.dumps({'url': url, 'error': type(exc).__name__}))

if __name__ == '__main__': main()

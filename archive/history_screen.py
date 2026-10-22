"""Conservative lexical screening; never claims to certify platform compliance."""
import hashlib
import json
import re
import unicodedata
from pathlib import Path


def normalize(text):
    text = unicodedata.normalize('NFKD', text).casefold()
    text = ''.join(c for c in text if unicodedata.category(c) not in ('Mn', 'Cf'))
    text = re.sub(r'(?<=[a-z0-9])!(?=[a-z0-9])', 'i', text)
    return text.translate(str.maketrans({'0': 'o', '1': 'i', '3': 'e', '4': 'a',
                                       '5': 's', '7': 't', '@': 'a', '$': 's'}))


def close_word(left, right, limit):
    if abs(len(left) - len(right)) > limit: return False
    row = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        nxt = [i]
        for j, b in enumerate(right, 1):
            nxt.append(min(nxt[-1] + 1, row[j] + 1, row[j-1] + (a != b)))
        if min(nxt) > limit: return False
        row = nxt
    return row[-1] <= limit


class Filter:
    def __init__(self, policy=None):
        self.policy = policy or json.loads(Path('config/history-filter.json').read_text())
        self.digest = hashlib.sha256(json.dumps(self.policy, sort_keys=True).encode()).hexdigest()
        self.terms = sorted(set(self.policy['terms'] + self.policy['variants']))
        self.patterns = []
        for term in self.terms:
            letters = re.sub('[^a-z]', '', normalize(term))
            # Match split/obfuscated letters too; require whole-word boundaries.
            pattern = r'(?<!\w)' + r'[\W_]*'.join(map(re.escape, letters)) + r'(?:s|es)?(?!\w)'
            self.patterns.append((term, re.compile(pattern)))
        self.near = [t for t in self.terms if t.isalpha()
                     and len(t) >= self.policy['near_match_min_length']]

    def scan(self, text):
        raw = unicodedata.normalize('NFKC', text).casefold()
        for marker in self.policy['masked_markers']:
            if marker.casefold() in raw:
                return {'term': 'masked_language', 'match_type': 'masked'}
        if re.search(r'\[\s*_+\s*\]', raw):
            return {'term': 'masked_language', 'match_type': 'masked'}
        if re.search(r'\b[a-z]+[*•_]{1,}[a-z]*\b', raw):
            return {'term': 'masked_language', 'match_type': 'masked'}
        clean = normalize(text)
        for term, pattern in self.patterns:
            if pattern.search(clean): return {'term': term, 'match_type': 'exact_or_variant'}
        # Longer words only: short fuzzy matches would turn "can"/"cant" into profanity.
        for word in set(re.findall('[a-z]+', clean)):
            for term in self.near:
                limit = (self.policy['long_match_max_edits'] if len(term) >= self.policy['long_match_min_length']
                         else self.policy['near_match_max_edits'])
                if close_word(word, term, limit):
                    return {'term': term, 'match_type': 'close_spelling'}
        return None

    def screen(self, transcript, title=''):
        previous = ''
        for item in [{'text': title, 'start': None}] + transcript['segments']:
            match = self.scan(previous + ' ' + item['text'])
            if match:
                return {'status': 'rejected_language', 'filter_sha256': self.digest,
                        'match': {**match, 'start_seconds': item['start']}}
            previous = item['text'][-200:] if item['start'] is not None else ''
        return {'status': 'passed_filter', 'filter_sha256': self.digest}

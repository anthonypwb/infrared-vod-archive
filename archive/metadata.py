"""Apply transcript-backed metadata plans after the archival attempt has finished."""
import hashlib
import json
import os
from pathlib import Path
import re
import signal
from .core import ArchiveError, BASE_TAGS, json_request, timestamp, upload_metadata, valid_uuid
from .state import StateStore
from .youtube import API, YouTube

START = '[Transcript-based overview]'
END = '[/Transcript-based overview]'
FIELDS = ('title', 'description', 'tags', 'categoryId', 'defaultLanguage', 'defaultAudioLanguage')


def validate(plan, vod):
    uid = vod['id']
    if (not valid_uuid(uid) or plan.get('schema_version') != 1
            or plan.get('vod_id') != uid or plan.get('youtube_id') != vod.get('youtube_id')
            or plan.get('transcript_url') != f'https://infrared-transcripts.pages.dev/en/kick/infrared/{uid}/'
            or not re.fullmatch(r'[a-f0-9]{64}', plan.get('transcript_sha256', ''))):
        raise ArchiveError('Metadata plan identity or provenance mismatch')
    summary, tags = plan.get('summary'), plan.get('tags')
    if (not isinstance(summary, str) or not 40 <= len(summary) <= 1500
            or any(x in summary for x in ('<', '>', START, END, 'Archive source ID:'))):
        raise ArchiveError('Invalid metadata summary')
    if (not isinstance(tags, list) or not 1 <= len(tags) <= 15
            or any(not isinstance(t, str) or not t.strip() or len(t) > 60
                   or any(c in t for c in '<>\n\r') for t in tags)
            or sum(len(t) + 3 for t in tags) > 350):
        raise ArchiveError('Invalid metadata tags')
    evidence = plan.get('evidence')
    if not isinstance(evidence, list) or len(evidence) < 2:
        raise ArchiveError('Metadata requires transcript evidence')
    for e in evidence:
        if (not isinstance(e, dict) or not isinstance(e.get('start'), (int, float))
                or not 0 <= e['start'] <= vod['media']['duration_seconds']
                or not isinstance(e.get('text'), str) or not 10 <= len(e['text']) <= 600):
            raise ArchiveError('Invalid metadata evidence')


def fingerprint(snippet):
    return hashlib.sha256(json.dumps(snippet, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def prepare(snippet, plan, vod, baseline=False):
    description = snippet.get('description', '')
    markers = [s.strip() for s in description.splitlines() if s.strip().startswith('Archive source ID:')]
    if any(s != f"Archive source ID: {vod['id']}" for s in markers):
        raise ArchiveError('Conflicting source marker')
    # Never replace an existing generated section or a subsequent human edit.
    if not baseline and (START in description or END in description):
        raise ArchiveError('Overview already present; reconcile without overwriting')
    result = {k: snippet[k] for k in FIELDS if k in snippet}
    if baseline:
        # Replace the old generated template, but retain custom descriptions and overviews.
        old_intro = "Full Infrared livestream, archived with the creator's permission."
        if not description.strip() or description.startswith(old_intro):
            overview = description[description.index(START):] if START in description else ''
            result['description'] = upload_metadata(vod)['snippet']['description'].rstrip()
            if overview:
                result['description'] += '\n\n' + overview
        else:
            result['description'] = re.sub(r"(?i)archived with the creator['’]s permission\.?", '', description).strip()
    else:
        block = f"{START}\n{plan['summary']}\n\nTranscript: {plan['transcript_url']}\n{END}"
        result['description'] = description.rstrip() + '\n\n' + block
    tags = list(snippet.get('tags', []))
    for tag in list(BASE_TAGS) + ([] if baseline else plan['tags']):
        if tag.casefold() not in {t.casefold() for t in tags}:
            tags.append(tag)
    if sum(len(t) + 3 for t in tags) > 500 or len(result['description'].encode()) > 5000:
        raise ArchiveError('Existing metadata leaves insufficient space')
    result['tags'] = tags
    return result


def apply_plan(youtube, store, state, plan, baseline=False):
    vod = state['vods'].get(plan.get('vod_id'))
    if not vod or vod.get('status') != 'archived':
        return
    if not baseline:
        validate(plan, vod)
    elif not valid_uuid(vod['id']) or not re.fullmatch(r'[A-Za-z0-9_-]{11}', vod.get('youtube_id', '')):
        raise ArchiveError('Baseline identity mismatch')
    record = vod.setdefault('metadata_baseline_v1' if baseline else 'metadata', {})
    if record.get('status') in ('applied', 'conflict'):
        return
    items = youtube.get('videos', part='snippet', id=vod['youtube_id']).get('items', [])
    if (len(items) != 1 or items[0]['id'] != vod['youtube_id']
            or items[0]['snippet'].get('channelId') != youtube.channel_id):
        raise ArchiveError('Metadata destination mismatch')
    item = items[0]
    current = {k: item['snippet'][k] for k in FIELDS if k in item['snippet']}
    digest = fingerprint(current)
    if record.get('status') == 'applying':
        if digest == record.get('target_sha256'):
            record.update(status='applied', applied_at=timestamp())
            store.save(state)
            return
        if digest != record.get('original_sha256'):
            record.update(status='conflict', note='Live metadata changed; automatic overwrite withheld')
            store.save(state)
            return
    target = prepare(current, plan, vod, baseline)
    if fingerprint(target) == digest:
        record.update(status='applied', applied_at=timestamp())
        store.save(state)
        return
    record.update(status='applying', original_sha256=digest, target_sha256=fingerprint(target),
                  transcript_sha256=plan.get('transcript_sha256'), started_at=timestamp())
    store.save(state)
    headers = {**youtube.auth(), 'Content-Type': 'application/json'}
    if not item.get('etag'):
        raise ArchiveError('Metadata update requires an ETag')
    headers['If-Match'] = item['etag']
    json_request(API + 'videos?part=snippet', method='PUT', headers=headers,
                 data=json.dumps({'id': vod['youtube_id'], 'snippet': target}).encode())
    # Read back before declaring success; a lost response is reconciled on the next run.
    after = youtube.get('videos', part='snippet', id=vod['youtube_id']).get('items', [])
    if len(after) != 1 or fingerprint({k: after[0]['snippet'][k] for k in FIELDS if k in after[0]['snippet']}) != record['target_sha256']:
        raise ArchiveError('Metadata read-back not confirmed')
    record.update(status='applied', applied_at=timestamp())
    store.save(state)


def main():
    if os.environ.get('ARCHIVE_METADATA_ENABLED') != 'true':
        print('Metadata updates await the one-time Google permission upgrade.')
        return
    def timeout(*_):
        raise ArchiveError('Metadata follow-up time limit reached')
    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(60)
    try:
        store = StateStore()
        state = store.load()
        if state.get('budget_pause'):
            return
        youtube = YouTube()
        youtube.verify_channel(os.environ.get('YOUTUBE_CHANNEL_ID'), '@archiveinfrared')
        count = 0
        # Basic channel tags and template cleanup do not wait for a transcript.
        for vod in state['vods'].values():
            if vod.get('status') != 'archived' or vod.get('metadata_baseline_v1', {}).get('status') in ('applied', 'conflict'):
                continue
            apply_plan(youtube, store, state, {'vod_id': vod['id']}, baseline=True)
            count += 1
            if count >= 2:
                return
        for path in sorted(Path('metadata/plans').glob('*.json')):
            plan = json.loads(path.read_text())
            vod = state['vods'].get(plan.get('vod_id'), {})
            if vod.get('status') != 'archived' or vod.get('metadata', {}).get('status') in ('applied', 'conflict'):
                continue
            count += 1
            try:
                apply_plan(youtube, store, state, plan)
            except Exception as exc:
                print('Metadata follow-up deferred:', type(exc).__name__)
                break
            if count >= 2:
                break
    except Exception as exc:
        print('Metadata follow-up deferred:', type(exc).__name__)
    finally:
        signal.alarm(0)


if __name__ == '__main__':
    main()

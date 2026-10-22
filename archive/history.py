"""Separate weekly historical import, strict transcript gate, one upload maximum."""
import argparse
from collections import Counter
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import signal
import tempfile
import time
from .budget import HEADROOM, sync_usage
from .core import ArchiveError, read_config, reserve, settle, timestamp, upload_metadata, utcnow
from .history_screen import Filter
from . import history_sources as sources
from .source import discover_kick, ensure_media_tools
from .state import StateStore
from .worker import automatic_candidates, can_attempt, merge_inventory, reconcile
from .youtube import YouTube


def read_history_config():
    cfg = json.loads(Path('config/history.json').read_text())
    if (cfg.get('schema_version') != 1 or cfg.get('uploads_per_calendar_week') != 1
            or tuple(cfg.get('channels', [])) != sources.CHANNELS
            or cfg.get('transcript_index') != sources.SITE + '/en/introduction/'):
        raise ArchiveError('Historical configuration identity or weekly limit changed')
    return cfg


def weekly_slot(history, now=None):
    now = now or utcnow()
    # A rolling seven-day check at a fixed weekly start would skip every second
    # run because the preceding upload started later than its workflow did.
    week_start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=now.weekday())
    for row in history.get('vods', {}).values():
        if row.get('status') in ('uploading', 'upload_unknown'):
            return False
        if row.get('upload_started_at'):
            if datetime.fromisoformat(row['upload_started_at']) >= week_start:
                return False
    return True


def history_admitted(state, cfg, config, now=None):
    now = now or utcnow(); month = now.strftime('%Y-%m')
    total = sum(state.get('budget', {}).get(month, {}).values())
    history_used = sum(state.get('history', {}).get('budget', {}).get(month, {}).values())
    reserve_minutes = config['run_reservation_minutes']
    return (total + reserve_minutes + HEADROOM + cfg['kick_reserve_minutes'] <= config['monthly_runtime_minutes']
            and history_used + reserve_minutes <= cfg['monthly_history_minutes'])


def known_archive_keys(state):
    rows = list(state.get('vods', {}).values()) + list(state.get('history', {}).get('vods', {}).values())
    return {sources.identity(v['date'], v['title']) for v in rows
            if v.get('date') and v.get('title') and (v.get('youtube_id') or v.get('upload_started_at'))}


def matched_candidates(rows, index, state):
    history = state.setdefault('history', {}); records = history.setdefault('catalog', {})
    groups = {}; existing = known_archive_keys(state)
    for row in rows:
        record = records.setdefault(row['source_url'], {})
        record.update({k:v for k,v in row.items() if k != 'master_url'})
        match = sources.match_transcript(row, index)
        if not match:
            record.update(status='skipped_no_unique_transcript', checked_at=timestamp()); continue
        day = match['date']; key = match['key']
        if key in existing:
            record.update(status='skipped_duplicate', checked_at=timestamp()); continue
        record.update(date=day, transcript_url=match['url'], original_key=key)
        group = groups.setdefault(key, {'row': {**row, 'date': day}, 'transcript': match,
                                        'key': key, 'records': []})
        group['records'].append(record)
    return sorted(groups.values(), key=lambda x:(x['row']['date'], x['key']))


def screen_candidate(candidate, policy, cfg):
    expected = candidate['transcript']; row = candidate['row']
    html = sources.fetch(expected['url'], ('infrared-transcripts.pages.dev',))
    transcript = sources.parse_transcript(html, expected)
    result = policy.screen(transcript, row['title'])
    result.update(transcript_url=expected['url'], transcript_sha256=transcript['sha256'], checked_at=timestamp())
    if result['status'] == 'passed_filter':
        video = sources.inspect_video(row)
        sources.check_coverage(transcript, video['duration'], cfg)
        result.update(duration_seconds=video['duration'], rumble_embed_id=video['embed_id'])
        return result, transcript, video
    return result, transcript, None


def write_report(report):
    Path('history-report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as out:
            out.write('## Historical archive\n\n```json\n' + json.dumps(report, indent=2) + '\n```\n')


def run(mode='auto'):
    cfg = read_history_config(); policy = Filter()
    report = {'filter_sha256': policy.digest, 'media_downloaded': False, 'uploaded': False}
    if mode == 'validate':
        report.update(status='configuration_valid', filter_terms=len(policy.terms), enabled=cfg['enabled'])
        write_report(report); return
    if not cfg['enabled']:
        report.update(status='disabled_source_access_unverified', reason=cfg['blocked_reason'])
        write_report(report); return
    config = read_config(); store = StateStore(); state = store.load()
    history = state.setdefault('history', {'vods': {}, 'catalog': {}, 'budget': {}})
    started = time.monotonic(); key = os.environ.get('GITHUB_RUN_ID', 'local') + '-' + os.environ.get('GITHUB_RUN_ATTEMPT', '1')
    month = None

    def timeout(signum, frame): raise ArchiveError('Historical run time allowance reached')
    def gate():
        if time.monotonic() - started >= (config['run_reservation_minutes'] - 5) * 60:
            raise ArchiveError('Historical run time allowance reached')
    signal.signal(signal.SIGALRM, timeout); signal.alarm(120)
    try:
        sync_usage(store, state, config)
        if not history_admitted(state, cfg, config):
            report.update(status='deferred_budget', reason='Historical allowance or Kick reserve reached')
            return
        month = reserve(state, key, config['run_reservation_minutes'], config['monthly_runtime_minutes'])
        history.setdefault('budget', {}).setdefault(month, {})[key] = config['run_reservation_minutes']
        store.save(state)
        signal.alarm((config['run_reservation_minutes'] - 5) * 60)
        youtube = YouTube(); youtube.verify_channel(os.environ.get('YOUTUBE_CHANNEL_ID'), config['destination_handle'])
        # Use the existing reconciliation logic, limited to historical records.
        wrapper = {'vods': history.setdefault('vods', {})}
        class HistoryStore:
            def save(self, _): store.save(state)
        reconcile(wrapper, youtube, HistoryStore())
        if not weekly_slot(history):
            report.update(status='deferred_weekly_limit_or_unknown_upload'); return
        kick = discover_kick(config); merge_inventory(state, kick); store.save(state)
        current = {x['id'] for x in kick}
        urgent = [v for v in state['vods'].values() if v['id'] in current and can_attempt(v, config)]
        if automatic_candidates(state, urgent, config):
            report.update(status='deferred_new_kick_vods', waiting=len(urgent)); return
        index = sources.transcript_index(sources.fetch(cfg['transcript_index'], ('infrared-transcripts.pages.dev',)))
        rows = sources.catalog(cfg, gate)
        candidates = matched_candidates(rows, index, state)
        report.update(source_entries=len(rows), transcript_entries=len(index), matched_candidates=len(candidates))
        store.save(state)
        checked = 0
        for candidate in candidates:
            gate()
            if all(r.get('status') in ('rejected_language', 'skipped_unverified')
                   and r.get('filter_sha256') == policy.digest
                   for r in candidate['records']): continue
            if checked >= cfg['max_transcripts_per_run']:
                report['status'] = 'scan_limit_reached'; return
            checked += 1
            try:
                result, transcript, video = screen_candidate(candidate, policy, cfg)
            except ArchiveError as exc:
                for record in candidate['records']:
                    record.update(status='skipped_unverified', reason=str(exc), checked_at=timestamp(),
                                  filter_sha256=policy.digest)
                store.save(state); continue
            for record in candidate['records']: record.update(result)
            store.save(state)
            if result['status'] != 'passed_filter': continue
            row = candidate['row']; uid = 'rumble-' + candidate['key'][:24]
            vod = history['vods'].setdefault(uid, {'id': uid, 'attempts': 0})
            if vod.get('attempts', 0) >= config['max_transfer_attempts']:
                report.update(status='oldest_candidate_needs_attention', candidate_id=uid); return
            if sources.identity(row['date'], row['title']) in known_archive_keys(state): continue
            existing = youtube.recent_match(uid)
            if existing:
                vod.update(row, status='existing_archive', youtube_id=existing); store.save(state); continue
            vod.update(row, status='downloading', source_provider='rumble', screening=result,
                       transcript_url=transcript['url'], attempts=vod['attempts'] + 1)
            store.save(state)
            try:
                ensure_media_tools(); gate()
                with tempfile.TemporaryDirectory(prefix='infrared-history-') as tmp:
                    output = Path(tmp) / 'vod.mp4'
                    vod['media'] = sources.download(video, output, config, gate)
                    report['media_downloaded'] = True
                    # Re-fetch and screen the exact transcript immediately before publication.
                    fresh = sources.parse_transcript(sources.fetch(transcript['url'], ('infrared-transcripts.pages.dev',)),
                                                     candidate['transcript'])
                    if fresh['sha256'] != transcript['sha256'] or policy.screen(fresh, row['title'])['status'] != 'passed_filter':
                        raise ArchiveError('Transcript changed before upload; no upload started')
                    sources.check_coverage(fresh, vod['media']['duration_seconds'], cfg)
                    gate()
                    vod.update(status='uploading', upload_started_at=timestamp())
                    store.save(state)  # Durable duplicate/weekly barrier BEFORE creating an upload.
                    metadata = upload_metadata(vod)
                    metadata['snippet']['description'] += '\nTranscript: ' + transcript['url'] + '\n'
                    vod['youtube_id'] = youtube.upload(output, metadata, gate)
                    vod['status'] = 'uploaded_pending'; store.save(state)
                    report.update(status='uploaded_pending', uploaded=True, youtube_id=vod['youtube_id'],
                                  original_date=vod['date'], title=vod['title'])
                    reconcile(wrapper, youtube, HistoryStore())
                    return
            except Exception:
                if vod['status'] == 'uploading': vod['status'] = 'upload_unknown'
                elif vod['status'] == 'downloading': vod['status'] = 'retry'
                store.save(state); raise
        report['status'] = 'no_eligible_video'
    except Exception as exc:
        report.update(status='blocked', reason=str(exc) if isinstance(exc, ArchiveError) else type(exc).__name__)
    finally:
        signal.alarm(0)
        if month:
            settle(state, month, key, time.monotonic() - started)
            history['budget'][month][key] = state['budget'][month][key]
        history['last_run_at'] = timestamp()
        history['last_report'] = dict(report)
        report['catalog_statuses'] = dict(Counter(v.get('status', 'discovered') for v in history.get('catalog', {}).values()))
        store.save(state); write_report(report)


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('mode', choices=('auto', 'validate'), default='validate', nargs='?')
    args = parser.parse_args(); run(args.mode)

if __name__ == '__main__': main()

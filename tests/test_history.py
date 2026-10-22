import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from archive.core import ArchiveError, read_config
from archive.history import history_admitted, matched_candidates, read_history_config, run, weekly_slot
from archive.history_screen import Filter
from archive.history_sources import check_coverage, identity, match_transcript, parse_transcript, transcript_index
from archive.budget import job_minutes


NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)
EXPECTED = {'title': 'The history of industry', 'date': '2021-05-03',
            'url': 'https://infrared-transcripts.pages.dev/en/youtube/infrared/example/'}


def page(title='The history of industry', day='2021-05-03', body=None):
    body = body or '<div class="transcript-item" title="00:00:00-00:00:12">Today we discuss industry.</div>'
    return '<h1 data-pagefind-meta="title">' + title + '</h1><div data-pagefind-meta="date">' + day + '</div>' + body


class ScreeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.filter = Filter()

    def test_every_configured_term_and_plural_is_rejected(self):
        for term in self.filter.terms:
            with self.subTest(term=term): self.assertIsNotNone(self.filter.scan('Someone said ' + term + '.'))
        for text in ('crackers', 'retards', 'nips', 'bitches', 'pussies'):
            with self.subTest(text=text): self.assertIsNotNone(self.filter.scan(text))

    def test_masked_split_unicode_and_near_spellings_are_rejected(self):
        for text in ('f.a.g.g.o.t', 'f a g g o t', 'r3tarded', 'sh!t', 'f**k', '[ __ ]',
                     '[censored]', 'ＦＵＣＫ', 'fa\u200bggot', 'retarted', 'motherfuker', 'faggut',
                     'fuck!', 'shit!', 'bitch!!'):
            with self.subTest(text=text): self.assertIsNotNone(self.filter.scan(text))

    def test_neutral_identity_words_and_short_substrings_are_not_blanket_banned(self):
        for text in ('Marxism communism philosophy history', 'gay trans Jewish Muslim disabled autistic',
                     'shipping passage', 'can cannot', 'The history of industry'):
            with self.subTest(text=text): self.assertIsNone(self.filter.scan(text))

    def test_quote_is_rejected_and_only_transcript_not_sidebar_is_screened(self):
        p = parse_transcript(page() + '<nav>faggot</nav>', EXPECTED)
        self.assertEqual(self.filter.screen(p)['status'], 'passed_filter')
        p['segments'][0]['text'] = 'He said "faggot", which was wrong.'
        self.assertEqual(self.filter.screen(p)['status'], 'rejected_language')

    def test_title_is_screened_and_timestamp_is_saved(self):
        p = parse_transcript(page(), EXPECTED)
        self.assertEqual(self.filter.screen(p, 'A shit title')['status'], 'rejected_language')
        p['segments'][0]['start'] = 90; p['segments'][0]['text'] = 'fuck'
        self.assertEqual(self.filter.screen(p)['match']['start_seconds'], 90)

    def test_flagged_phrase_crossing_transcript_lines_is_rejected(self):
        p = {'segments':[{'start':0,'text':'He said kill'}, {'start':1,'text':'yourself.'}]}
        self.assertEqual(self.filter.screen(p)['status'], 'rejected_language')


class TranscriptTests(unittest.TestCase):
    def test_missing_partial_malformed_or_mismatched_transcript_never_passes(self):
        invalid = [page(body='<p>There is no transcript.</p>'), page(title='Another video'),
                   page(day='2021-05-04'), page(body='<div class="transcript-item">Words</div>'),
                   page(body='<div class="transcript-item" title="00:00:05-00:00:02">Words</div>'),
                   page(body='<div class="transcript-item" title="00:00:00-00:00:12">Unclosed')]
        for text in invalid:
            with self.subTest(text=text):
                with self.assertRaises(ArchiveError): parse_transcript(text, EXPECTED)

    def test_original_title_date_overrides_repost_date(self):
        data = '<a href="/en/youtube/infrared-vods/example">2021-09-27 The history of industry [2021-05-03]</a>'
        index = transcript_index(data)
        self.assertEqual(index[0]['date'], '2021-05-03')
        transcript = parse_transcript(page(title='The history of industry [2021-05-03]', day='2021-09-27'), index[0])
        self.assertEqual(transcript['date'], '2021-05-03')

    def test_empty_class_attribute_does_not_break_real_site_markup(self):
        result = parse_transcript('<label class>Theme</label>' + page(), EXPECTED)
        self.assertEqual(len(result['segments']), 1)

    def test_site_heading_does_not_replace_video_title(self):
        result = parse_transcript('<header><h1>Infrared Content Catalog</h1></header>' + page(), EXPECTED)
        self.assertEqual(result['title'], EXPECTED['title'])
        with self.assertRaises(ArchiveError):
            parse_transcript('<h1>The history of industry</h1>' + page(title='Wrong video'), EXPECTED)

    def test_ambiguous_titles_or_conflicting_dates_do_not_match(self):
        index = [{**EXPECTED, 'key': identity(EXPECTED['date'], EXPECTED['title'])}]
        row = {'title': 'The history of industry [2021-05-03]', 'date': '2021-05-03'}
        self.assertEqual(match_transcript(row, index), index[0])
        self.assertIsNone(match_transcript({**row,'date':'2021-05-04'}, index))
        self.assertIsNone(match_transcript(row, index + [{**index[0], 'url': 'another'}]))

    def test_incomplete_coverage_fails(self):
        cfg = read_history_config()
        good = {'segments':[{'start':0,'end':120,'text':'word '*60}]}
        check_coverage(good, 120, cfg)
        for parts, duration in [([{'start':70,'end':120,'text':'word '*60}],120),
                                (good['segments'],300),
                                ([{'start':0,'end':10,'text':'word '*60},
                                  {'start':200,'end':300,'text':'word '*60}],300),
                                ([{'start':0,'end':120,'text':'Only three words'}],120)]:
            with self.assertRaises(ArchiveError): check_coverage({'segments':parts},duration,cfg)


class HistoryControlTests(unittest.TestCase):
    def test_oldest_first_across_channels_and_duplicates_suppressed(self):
        index = [{**EXPECTED,'key':identity(EXPECTED['date'],EXPECTED['title'])},
                 {**EXPECTED,'date':'2020-01-01','title':'Earlier stream','url':EXPECTED['url']+'earlier',
                  'key':identity('2020-01-01','Earlier stream')}]
        rows = [{'title':'The history of industry [2021-05-03]','date':'2021-05-03','source_url':'one'},
                {'title':'The history of industry [2021-05-03]','date':'2021-05-03','source_url':'duplicate'},
                {'title':'Earlier stream [2020-01-01]','date':'2020-01-01','source_url':'earlier'},
                {'title':'No transcript','date':'2019-01-01','source_url':'missing'}]
        state = {'vods':{}}
        got = matched_candidates(rows,index,state)
        self.assertEqual([g['row']['date'] for g in got],['2020-01-01','2021-05-03'])
        self.assertEqual(len(got[1]['records']),2)
        self.assertEqual(state['history']['catalog']['missing']['status'],'skipped_no_unique_transcript')
        state['vods']['kick-id'] = {'date':EXPECTED['date'],'title':EXPECTED['title'],'youtube_id':'uploaded'}
        self.assertEqual(len(matched_candidates(rows,index,state)),1)

    def test_calendar_week_and_uncertain_upload_suppress_new_uploads(self):
        history = {'vods':{'one':{'status':'archived','upload_started_at':'2026-10-20T00:00:00+00:00'}}}
        self.assertFalse(weekly_slot(history,NOW))
        history['vods']['one']['upload_started_at'] = '2026-10-17T15:00:00+00:00'
        self.assertTrue(weekly_slot(history,NOW))
        self.assertTrue(weekly_slot(history,NOW.replace(day=24,hour=14,minute=37)))
        history['vods']['one']['status'] = 'upload_unknown'
        self.assertFalse(weekly_slot(history,NOW))

    def test_shared_cap_history_cap_and_kick_reserve(self):
        cfg, config = read_history_config(), read_config()
        state = {'budget':{'2026-10':{'jobs':770}},'history':{'budget':{'2026-10':{'history':205}}}}
        self.assertTrue(history_admitted(state,cfg,config,NOW))
        state['budget']['2026-10']['jobs'] = 771
        self.assertFalse(history_admitted(state,cfg,config,NOW))
        state['budget']['2026-10']['jobs'] = 500
        state['history']['budget']['2026-10']['history'] = 206
        self.assertFalse(history_admitted(state,cfg,config,NOW))

    def test_disabled_workflow_cannot_access_sources_or_upload(self):
        cfg = read_history_config(); cfg['enabled'] = False
        with patch('archive.history.read_history_config',return_value=cfg), \
             patch('archive.history.StateStore') as store, patch('archive.history.YouTube') as yt, \
             patch('archive.history.sources.catalog') as cat, patch('archive.history.write_report') as report:
            run('auto'); store.assert_not_called(); yt.assert_not_called(); cat.assert_not_called()
            self.assertEqual(report.call_args.args[0]['status'],'disabled_source_access_unverified')

    def test_active_historical_runner_is_counted_without_breaking_kick_accounting(self):
        job = {'run_id':2,'run_attempt':1,'name':'history','status':'in_progress',
               'workflow_name':'Archive historical VODs','started_at':NOW.isoformat()}
        self.assertEqual(job_minutes(job,NOW.replace(day=1),1,1,45),45)
        self.assertEqual(job_minutes(job,NOW.replace(day=1),2,1,45),0)


if __name__ == '__main__': unittest.main()

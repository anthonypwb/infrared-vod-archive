import copy
import unittest
from unittest.mock import Mock, patch
from archive.core import ArchiveError
from archive.metadata import apply_plan, prepare, validate

UID = '59dcb3eb-94d6-4c33-86be-3fcff5de6ba4'


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.vod = {'id': UID, 'youtube_id': 'uaTfcuLH40E', 'status': 'archived',
                    'media': {'duration_seconds': 1000}}
        self.plan = {'schema_version': 1, 'vod_id': UID, 'youtube_id': 'uaTfcuLH40E',
                     'transcript_url': f'https://infrared-transcripts.pages.dev/en/kick/infrared/{UID}/',
                     'transcript_sha256': 'a'*64, 'summary': 'The speaker discusses economic history and current international affairs.',
                     'tags': ['economic history'],
                     'evidence': [{'start': 10, 'text': 'Economic history discussion'},
                                  {'start': 100, 'text': 'International affairs discussion'}]}
        self.snippet = {'title': 'User edited title', 'categoryId': '25',
                        'description': 'User edited description', 'tags': ['existing'],
                        'defaultLanguage': 'en', 'defaultAudioLanguage': 'en'}

    def test_preserves_manual_fields_and_merges(self):
        validate(self.plan, self.vod)
        target = prepare(self.snippet, self.plan, self.vod)
        self.assertTrue(target['description'].startswith('User edited description'))
        self.assertEqual(target['title'], self.snippet['title'])
        self.assertEqual(target['tags'], ['existing', 'economic history'])
        self.assertEqual(target['defaultAudioLanguage'], 'en')

    def test_rejects_wrong_video_and_conflicting_marker_and_oversize(self):
        plan = dict(self.plan, youtube_id='WRONG')
        with self.assertRaises(ArchiveError): validate(plan, self.vod)
        with self.assertRaises(ArchiveError):
            prepare(dict(self.snippet, description='Archive source ID: different'), self.plan, self.vod)
        with self.assertRaises(ArchiveError):
            prepare(dict(self.snippet, description='a'*4900), self.plan, self.vod)

    def test_lost_response_is_reconciled_without_duplicate_write(self):
        yt, store = Mock(), Mock()
        yt.channel_id = 'channel'
        yt.auth.return_value = {}
        def item(snippet):
            return {'items': [{'id': self.vod['youtube_id'], 'etag': 'etag',
                               'snippet': dict(snippet, channelId='channel')}]}
        state = {'vods': {UID: self.vod}}
        yt.get.return_value = item(self.snippet)
        with patch('archive.metadata.json_request', side_effect=OSError):
            with self.assertRaises(OSError): apply_plan(yt, store, state, self.plan)
        self.assertEqual(self.vod['metadata']['status'], 'applying')
        yt.get.return_value = item(prepare(self.snippet, self.plan, self.vod))
        with patch('archive.metadata.json_request') as update:
            apply_plan(yt, store, state, self.plan)
            update.assert_not_called()
        self.assertEqual(self.vod['metadata']['status'], 'applied')

    def test_different_channel_cannot_be_edited(self):
        yt = Mock(channel_id='expected')
        yt.get.return_value = {'items': [{'id': self.vod['youtube_id'],
                                        'snippet': dict(self.snippet, channelId='other')}]}
        with patch('archive.metadata.json_request') as update:
            with self.assertRaises(ArchiveError):
                apply_plan(yt, Mock(), {'vods': {UID: self.vod}}, self.plan)
            update.assert_not_called()

    def test_success_only_updates_snippet_and_checks_readback(self):
        yt, store = Mock(channel_id='channel'), Mock()
        yt.auth.return_value = {}
        target = prepare(self.snippet, self.plan, self.vod)
        yt.get.side_effect = [{'items': [{'id': self.vod['youtube_id'], 'etag': 'etag',
                                       'snippet': dict(self.snippet, channelId='channel')}]},
                              {'items': [{'snippet': target}]}]
        with patch('archive.metadata.json_request') as update:
            apply_plan(yt, store, {'vods': {UID: self.vod}}, self.plan)
            self.assertTrue(update.call_args.args[0].endswith('part=snippet'))
            self.assertEqual(update.call_args.kwargs['headers']['If-Match'], 'etag')
        self.assertEqual(self.vod['metadata']['status'], 'applied')


if __name__ == '__main__':
    unittest.main()

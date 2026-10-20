import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from archive.core import ArchiveError, allowed_url, read_config, reserve, settle, upload_metadata
from archive.source import Page, acquire, eligible_date, parse_inventory, parse_playlist, resolve
from archive.worker import can_attempt, merge_inventory, reconcile, weekly_count
from archive.youtube import YouTube, duration_seconds, next_offset

VOD = "4e0647b7-fce2-442b-a503-9c359c8d0afc"
BASE = "https://stream.kick.com/recording/playlist.m3u8"
NOW = datetime(2026, 10, 20, 22, tzinfo=timezone.utc)


class DiscoveryTests(unittest.TestCase):
    def test_site_heading_does_not_prefix_stream_title(self):
        page = Page()
        page.feed('<h1>Infrared Content Catalog</h1><main><h1>Stream title</h1></main>')
        self.assertEqual(page.title, 'Stream title')

    def test_only_exact_infrared_kick_paths_and_matching_date(self):
        html = f'''<ul><li>2026-10-20 <a href="/en/kick/infrared/{VOD}/">Stream</a></li>
        <li><a href="/en/youtube/infrared/{VOD}/">Other</a></li>
        <li><a href="https://evil.example/en/kick/infrared/{VOD}/">Wrong</a></li></ul>'''
        rows = parse_inventory(html)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["date"], "2026-10-20")
        self.assertEqual(rows[0]["title"], "Stream")

    def test_undated_and_stale_sources_not_eligible(self):
        self.assertFalse(eligible_date(None, 21, NOW.date()))
        self.assertFalse(eligible_date("2026-09-16", 21, NOW.date()))
        self.assertFalse(eligible_date("2026-10-21", 21, NOW.date()))
        self.assertTrue(eligible_date("2026-10-19", 21, NOW.date()))

    def test_source_hosts_are_restricted(self):
        for url in ("http://kick.com/a", "https://kick.com.evil.test/a", "https://a.kick.com:8443/a",
                    "https://user:pass@kick.com/a", "https://127.0.0.1/a"):
            with self.assertRaises(ArchiveError):
                allowed_url(url, ("kick.com", ".kick.com"))

    def test_full_quality_selected_without_downscaling(self):
        master = '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720\n720.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080\n1080.m3u8\n'
        with patch("archive.source.get_text", side_effect=[master, "playlist"]):
            url, _ = resolve(BASE)
        self.assertTrue(url.endswith("1080.m3u8"))

    def test_incomplete_encrypted_and_missing_segments_rejected(self):
        for text in ("#EXTM3U\n#EXTINF:6,\na.ts\n",
                     "#EXTM3U\n#EXT-X-KEY:METHOD=AES-128\n#EXTINF:6,\na.ts\n#EXT-X-ENDLIST",
                     "#EXTM3U\n#EXT-X-GAP\n#EXTINF:6,\na.ts\n#EXT-X-ENDLIST",
                     "#EXTM3U\n#EXTINF:NaN,\na.ts\n#EXT-X-ENDLIST"):
            with self.assertRaises(ArchiveError):
                parse_playlist(text, BASE)


class StateTests(unittest.TestCase):
    def test_budget_reservation_survives_failure_and_blocks_excess(self):
        state = {}
        month = reserve(state, "first", 45, 50, NOW)
        with self.assertRaises(ArchiveError):
            reserve(state, "second", 45, 50, NOW)
        settle(state, month, "first", 90)
        self.assertEqual(state["budget"][month]["first"], 2)
        reserve(state, "second", 45, 50, NOW)
        with self.assertRaises(ArchiveError):
            reserve(state, "second", 1, 50, NOW)

    def test_known_uploads_never_reenter_transfer_queue(self):
        cfg = read_config()
        for status in ("archived", "uploaded_pending", "upload_unknown", "uploading", "existing_archive"):
            self.assertFalse(can_attempt({"status": status, "date": "2026-10-20", "attempts": 0}, cfg, NOW))

    def test_refresh_does_not_reset_upload_state(self):
        state = {"vods": {VOD: {"status": "archived", "date": "2026-10-20", "attempts": 1}}}
        merge_inventory(state, [{"id": VOD, "date": "2026-10-20", "title": "New label"}])
        self.assertEqual(state["vods"][VOD]["status"], "archived")

    def test_delayed_transcript_can_be_new_without_bulk_backfill(self):
        row = {"id": VOD, "date": "2026-10-19", "title": "Delayed indexing"}
        initial = {"vods": {}}
        merge_inventory(initial, [row])
        self.assertFalse(initial["vods"][VOD]["new_since_monitoring"])
        running = {"vods": {}, "monitoring_started_at": NOW.isoformat()}
        merge_inventory(running, [row])
        self.assertTrue(running["vods"][VOD]["new_since_monitoring"])

    def test_uncertain_upload_is_reconciled_without_reupload(self):
        state = {"pilot_verified": False, "vods": {VOD: {"id": VOD, "status": "uploading", "is_pilot": True}}}
        youtube, store = Mock(), Mock()
        youtube.recent_match.return_value = None
        reconcile(state, youtube, store)
        self.assertEqual(state["vods"][VOD]["status"], "upload_unknown")
        youtube.upload.assert_not_called()
        youtube.recent_match.return_value = "abcdefghijk"
        youtube.verify_video.return_value = ("archived", "Verified")
        reconcile(state, youtube, store)
        self.assertTrue(state["pilot_verified"])
        self.assertEqual(state["vods"][VOD]["youtube_id"], "abcdefghijk")


class YouTubeTests(unittest.TestCase):
    def youtube(self):
        yt = YouTube()
        yt.channel_id = "UC" + "a" * 22
        yt.auth = lambda: {"Authorization": "Bearer redacted"}
        return yt

    def test_wrong_channel_blocks_before_upload(self):
        yt = YouTube()
        yt.get = Mock(side_effect=[{"items": [{"id": "UC" + "b" * 22}]}, {"items": [{"id": "UC" + "a" * 22}]}])
        with self.assertRaises(ArchiveError):
            yt.verify_channel("UC" + "a" * 22, "@archiveinfrared")

    def test_private_video_not_marked_archived(self):
        yt = self.youtube()
        yt.get = Mock(return_value={"items": [{"snippet": {"channelId": yt.channel_id,
            "description": f"Archive source ID: {VOD}"}, "status": {"uploadStatus": "processed", "privacyStatus": "private"},
            "processingDetails": {"processingStatus": "succeeded"}, "contentDetails": {"duration": "PT1H"}}]})
        status, _ = yt.verify_video({"id": VOD, "youtube_id": "abcdefghijk", "media": {"duration_seconds": 3600}})
        self.assertEqual(status, "blocked")

    def test_resumable_failure_queries_same_session_and_does_not_insert_twice(self):
        yt = self.youtube()
        replies = [(200, {"Location": "https://www.googleapis.com/upload/test"}, b""),
                   (503, {}, b""), (308, {}, b""), (200, {}, b'{"id":"abcdefghijk"}')]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "video.mp4"
            path.write_bytes(b"abc")
            with patch("archive.youtube.request", side_effect=replies) as req, patch("archive.youtube.time.sleep"):
                self.assertEqual(yt.upload(path, {}, lambda: None), "abcdefghijk")
            self.assertEqual(sum(call.kwargs.get("method") == "POST" for call in req.call_args_list), 1)
            self.assertEqual(req.call_args_list[2].kwargs["headers"]["Content-Range"], "bytes */3")

    def test_duration_and_offsets(self):
        self.assertEqual(duration_seconds("PT2H3M4S"), 7384)
        self.assertEqual(next_offset({"Range": "bytes=0-99"}, 200), 100)
        with self.assertRaises(ArchiveError):
            next_offset({"Range": "bytes=0-201"}, 200)


class MediaIntegrationTests(unittest.TestCase):
    def test_lossless_remux_keeps_audio_video_and_duration(self):
        with tempfile.TemporaryDirectory() as temp:
            source, output = Path(temp) / "test.ts", Path(temp) / "out.mp4"
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10",
                            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "3",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-f", "mpegts", str(source)],
                           check=True, capture_output=True, timeout=30)
            with patch("archive.source.request", return_value=(200, {}, source.read_bytes())):
                report = acquire([{"duration": 3, "url": BASE}], output, read_config(), lambda: None)
            self.assertEqual(report["video_codec"], "h264")
            self.assertEqual(report["audio_codec"], "aac")
            self.assertTrue(report["sample_decode_passed"])
            self.assertLess(abs(report["duration_seconds"] - 3), .1)


if __name__ == "__main__":
    unittest.main()

import json
import copy
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from archive.core import ArchiveError, allowed_url, read_config, reserve, settle, upload_metadata
from archive.source import (Page, acquire, discover_kick, eligible_date, kick_json, parse_inventory,
                            parse_kick_videos, parse_playlist, read_kick_source, resolve)
from archive.worker import automatic_candidates, can_attempt, discover, merge_inventory, reconcile, weekly_count
from archive.youtube import YouTube, duration_seconds, next_offset

VOD = "4e0647b7-fce2-442b-a503-9c359c8d0afc"
BASE = "https://stream.kick.com/recording/playlist.m3u8"
NOW = datetime(2026, 10, 20, 22, tzinfo=timezone.utc)


class DiscoveryTests(unittest.TestCase):
    def kick_fixture(self):
        return json.loads((Path(__file__).parent / "fixtures/kick-videos.json").read_text())

    def test_direct_kick_discovery_works_without_a_transcript(self):
        payload = self.kick_fixture()
        with patch("archive.source.kick_json", side_effect=[{"slug": "infrared", "id": 129849}, payload]) as api, \
                patch("archive.worker.get_text") as transcript:
            rows = discover(read_config())
        self.assertEqual(rows[0]["id"], "4a6c5907-0018-4293-9ab5-8df1990edc8f")
        self.assertEqual(rows[0]["date"], "2026-10-20")
        self.assertEqual(rows[0]["title"], "WORLD REVOLUTION")
        self.assertEqual(rows[0]["source_url"], f"https://kick.com/infrared/videos/{rows[0]['id']}")
        self.assertEqual(read_kick_source(rows[0], rows), payload[0]["source"])
        self.assertEqual(api.call_count, 2)
        transcript.assert_not_called()

    def test_wrong_channel_and_changed_schema_are_rejected(self):
        cfg = read_config()
        for identity in ({"slug": "someone-else", "id": 129849}, {"slug": "infrared", "id": 1}):
            with patch("archive.source.kick_json", return_value=identity), self.assertRaises(ArchiveError):
                discover_kick(cfg)
        row = self.kick_fixture()[0]
        row["channel_id"] = 1
        for payload in ([row], {"data": []}, None):
            with self.assertRaises(ArchiveError):
                parse_kick_videos(payload, 129849)

    def test_live_private_pruned_deleted_and_unknown_status_are_skipped(self):
        for field, value in (("is_live", True), ("is_live", None), ("is_private", True),
                             ("is_pruned", True), ("deleted_at", "2026-10-20"), ("status", "private")):
            row = self.kick_fixture()[0]
            (row if field == "is_live" else row["video"])[field] = value
            self.assertEqual(parse_kick_videos([row], 129849), [])

    def test_bad_playback_hosts_dates_ids_and_conflicts_are_rejected(self):
        original = self.kick_fixture()[0]
        for field, value in (("source", "https://evil.example/master.m3u8"),
                             ("start_time", "not-a-date"), ("session_title", ""), ("uuid", "bad-id")):
            row = copy.deepcopy(original)
            (row["video"] if field == "uuid" else row)[field] = value
            with self.assertRaises(ArchiveError):
                parse_kick_videos([row], 129849)
        changed = copy.deepcopy(original)
        changed["source"] = "https://stream.kick.com/different.m3u8"
        with self.assertRaises(ArchiveError):
            parse_kick_videos([original, changed], 129849)
        with self.assertRaises(ArchiveError):
            read_kick_source({"id": VOD}, parse_kick_videos([original], 129849))

    def test_source_denial_stops_without_transcript_fallback(self):
        with patch("archive.source.request", return_value=(403, {}, b"denied")) as request:
            with self.assertRaisesRegex(ArchiveError, "HTTP 403"):
                discover_kick(read_config())
            self.assertEqual(request.call_count, 1)
        with self.assertRaises(ArchiveError):
            kick_json("https://kick.com/api/v2/channels/other/videos")

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

    def test_available_backlog_has_no_artificial_age_cutoff(self):
        self.assertTrue(eligible_date("2026-09-21", None, NOW.date()))
        for value in (None, "not-a-date", "2026-10-21"):
            self.assertFalse(eligible_date(value, None, NOW.date()))

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
    def test_backfill_processes_oldest_current_unarchived_recordings_first(self):
        cfg = read_config()
        rows = DiscoveryTests().kick_fixture()
        inventory = parse_kick_videos(rows, cfg["source_channel_id"])
        state = {"vods": {}, "monitoring_started_at": NOW.isoformat()}
        merge_inventory(state, inventory)
        pilot_id = "59dcb3eb-94d6-4c33-86be-3fcff5de6ba4"
        state["vods"][pilot_id].update(status="archived", youtube_id="abcdefghijk", attempts=1)
        eligible = [v for v in state["vods"].values() if can_attempt(v, cfg, NOW)]
        queue = automatic_candidates(state, eligible, cfg)
        self.assertEqual(queue[0]["id"], "42e52ed4-772d-467f-aeb6-23dbdc63cc45")
        self.assertEqual(queue[0]["date"], "2026-09-21")
        self.assertEqual([v["started_at"] for v in queue], sorted(v["started_at"] for v in queue))
        self.assertNotIn(pilot_id, [v["id"] for v in queue])
        self.assertIsNone(cfg["max_new_uploads_per_week"])
        self.assertEqual(cfg["monthly_runtime_minutes"], 300)

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
        state = {"vods": {VOD: {"status": "archived", "date": "2026-10-20", "attempts": 1,
                               "youtube_id": "existing", "source_url": f"https://infrared-transcripts.pages.dev/en/kick/infrared/{VOD}/"}}}
        merge_inventory(state, [{"id": VOD, "date": "2026-10-20", "title": "New label",
                                 "source_url": f"https://kick.com/infrared/videos/{VOD}", "master_url": BASE}])
        self.assertEqual(state["vods"][VOD]["status"], "archived")
        self.assertEqual(state["vods"][VOD]["youtube_id"], "existing")
        self.assertEqual(state["vods"][VOD]["attempts"], 1)
        self.assertIn("infrared-transcripts", state["vods"][VOD]["transcript_url"])
        self.assertNotIn("master_url", state["vods"][VOD])

    def test_switching_discovery_does_not_trigger_bulk_historical_backfill(self):
        row = {"id": VOD, "date": "2026-10-19", "title": "Existing Kick recording"}
        initial = {"vods": {}}
        merge_inventory(initial, [row])
        self.assertFalse(initial["vods"][VOD]["new_since_monitoring"])
        running = {"vods": {}, "monitoring_started_at": NOW.isoformat()}
        same_day = {"id": "4a6c5907-0018-4293-9ab5-8df1990edc8f", "date": "2026-10-20", "title": "Latest"}
        merge_inventory(running, [row, same_day])
        self.assertFalse(running["vods"][VOD]["new_since_monitoring"])
        self.assertFalse(running["vods"][same_day["id"]]["new_since_monitoring"])
        self.assertEqual([v["id"] for v in automatic_candidates(running, list(running["vods"].values()))],
                         [same_day["id"]])

    def test_newly_appearing_recordings_and_prior_attempts_remain_eligible(self):
        running = {"vods": {}, "monitoring_started_at": NOW.isoformat(),
                   "kick_monitoring_started_at": NOW.isoformat()}
        merge_inventory(running, [{"id": VOD, "date": "2026-10-19", "title": "Late appearing VOD"}])
        vod = running["vods"][VOD]
        self.assertTrue(vod["new_since_monitoring"])
        self.assertEqual(automatic_candidates(running, [vod]), [vod])
        vod.update(new_since_monitoring=False, attempts=1)
        self.assertEqual(automatic_candidates(running, [vod]), [vod])

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
        yt.get = Mock(return_value={"items": [{"id": "abcdefghijk", "snippet": {"channelId": yt.channel_id,
            "description": f"Archive source ID: {VOD}"}, "status": {"uploadStatus": "processed", "privacyStatus": "private"},
            "processingDetails": {"processingStatus": "succeeded"}, "contentDetails": {"duration": "PT1H"}}]})
        status, _ = yt.verify_video({"id": VOD, "youtube_id": "abcdefghijk", "media": {"duration_seconds": 3600}})
        self.assertEqual(status, "blocked")

    def video_fixture(self, yt, description=""):
        return {"id": "abcdefghijk", "snippet": {"channelId": yt.channel_id, "description": description},
                "status": {"uploadStatus": "processed", "privacyStatus": "public"},
                "processingDetails": {"processingStatus": "succeeded"},
                "contentDetails": {"duration": "PT1H"}}

    def test_known_pilot_verifies_after_description_edit_without_reupload(self):
        for description in ("", "Updated description", f"Archive source ID: {VOD}"):
            yt = self.youtube()
            yt.get = Mock(return_value={"items": [self.video_fixture(yt, description)]})
            yt.upload = Mock()
            state = {"pilot_verified": False, "vods": {VOD: {"id": VOD, "youtube_id": "abcdefghijk",
                "media": {"duration_seconds": 3600}, "status": "uploaded_pending", "is_pilot": True}}}
            reconcile(state, yt, Mock())
            self.assertTrue(state["pilot_verified"])
            self.assertEqual(state["vods"][VOD]["status"], "archived")
            yt.upload.assert_not_called()

    def test_known_upload_rejects_wrong_video_channel_or_conflicting_source(self):
        yt = self.youtube()
        vod = {"id": VOD, "youtube_id": "abcdefghijk", "media": {"duration_seconds": 3600}}
        for field, value in (("id", "other1234567"), ("channelId", "UC" + "b" * 22),
                             ("description", "Archive source ID: other-source")):
            item = self.video_fixture(yt)
            (item if field == "id" else item["snippet"])[field] = value
            yt.get = Mock(return_value={"items": [item]})
            with self.assertRaises(ArchiveError):
                yt.verify_video(vod)

    def test_edited_description_still_requires_processing_publicity_and_full_duration(self):
        yt = self.youtube()
        vod = {"id": VOD, "youtube_id": "abcdefghijk", "media": {"duration_seconds": 3600}}
        for section, key, value, expected in (
                ("processingDetails", "processingStatus", "processing", "pending"),
                ("status", "uploadStatus", "rejected", "blocked"),
                ("status", "privacyStatus", "private", "blocked"),
                ("contentDetails", "duration", "PT10M", "blocked")):
            item = self.video_fixture(yt)
            item[section][key] = value
            yt.get = Mock(return_value={"items": [item]})
            self.assertEqual(yt.verify_video(vod)[0], expected)

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

import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from archive.source import acquire
from archive.core import ArchiveError, read_config


class RemuxDiagnosticsTests(unittest.TestCase):
    def test_broken_pipe_retains_redacted_error_and_segment(self):
        process = Mock(returncode=1)
        process.stdin.write.side_effect = BrokenPipeError
        process.poll.return_value = 1
        def launch(*args, **kwargs):
            kwargs['stderr'].write(b'Invalid data at https://example.com/media?token=secret\n')
            return process
        body = (bytes([0x47]) + b'0'*187)*2
        with tempfile.TemporaryDirectory() as tmp, \
                patch('archive.source.subprocess.Popen', side_effect=launch), \
                patch('archive.source.request', return_value=(200, {}, body)), \
                patch('archive.source.shutil.disk_usage', return_value=Mock(free=20_000_000_000)):
            with self.assertRaises(ArchiveError) as caught:
                acquire([{'duration': 10, 'url': 'https://example.com/segment'}],
                        Path(tmp)/'vod.mp4', read_config(), lambda: None)
        diagnostic = caught.exception.diagnostic
        self.assertEqual(diagnostic['segment_index'], 1)
        self.assertEqual(diagnostic['exit_code'], 1)
        self.assertEqual(diagnostic['source_bytes'], len(body))
        self.assertIn('Invalid data', diagnostic['stderr_tail'])
        self.assertNotIn('token=secret', diagnostic['stderr_tail'])


if __name__ == '__main__':
    unittest.main()

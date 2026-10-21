import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch
from archive.budget import admit, has_room, job_minutes, pause, reset_month, sync_usage
from archive.core import ArchiveError, read_config

NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)
START = NOW.replace(day=1)


class BudgetTests(unittest.TestCase):
    def job(self, **changes):
        return dict({'id': 7, 'run_id': 1, 'run_attempt': 1, 'name': 'archive',
                     'workflow_name': 'Archive Infrared VODs', 'status': 'completed',
                     'conclusion': 'success', 'started_at': '2026-10-20T00:00:00Z',
                     'completed_at': '2026-10-20T00:01:01Z'}, **changes)

    def test_job_rounding_prior_month_cross_month_and_queued_cancelled(self):
        self.assertEqual(job_minutes(self.job(), START, 2, 1, 45), 2)
        self.assertEqual(job_minutes(self.job(started_at='2026-09-20T00:00:00Z',
                                             completed_at='2026-09-20T00:02:00Z'), START, 2, 1, 45), 0)
        self.assertEqual(job_minutes(self.job(started_at='2026-09-30T23:59:00Z',
                                             completed_at='2026-10-01T00:02:00Z'), START, 2, 1, 45), 3)
        self.assertEqual(job_minutes(self.job(started_at=None, completed_at=None,
                                             conclusion='cancelled'), START, 2, 1, 45), 0)

    def test_current_attempt_reserved_once_and_other_active_jobs_bounded(self):
        job = self.job(status='in_progress', completed_at=None)
        self.assertEqual(job_minutes(job, START, 1, 1, 45), 0)
        self.assertEqual(job_minutes(job, START, 2, 1, 45), 45)
        job.update(workflow_name='Archive checks', name='tests')
        self.assertEqual(job_minutes(job, START, 2, 1, 45), 5)
        job['workflow_name'] = 'Unbounded job'
        with self.assertRaises(ArchiveError):
            job_minutes(job, START, 2, 1, 45)

    def test_monthly_usage_includes_ci_and_all_attempts_and_uses_completed_cache(self):
        store = Mock()
        state = {'budget': {'2026-10': {'worker-1': 17}}}
        runs = [{'id': 1, 'run_attempt': 2, 'status': 'completed', 'updated_at': NOW.isoformat()}]
        jobs = [self.job(completed_at='2026-10-20T00:17:00Z'),
                self.job(id=8, run_attempt=2, completed_at='2026-10-20T00:02:00Z'),
                self.job(id=9, name='tests', completed_at='2026-10-20T00:01:00Z')]
        with patch('archive.budget.collection', side_effect=[iter(runs), iter(jobs)]):
            self.assertEqual(sync_usage(store, state, read_config(), NOW), 20)
        self.assertEqual(state['budget']['2026-10']['github_other_jobs'], 3)
        with patch('archive.budget.collection', side_effect=[iter(runs)]) as calls:
            self.assertEqual(sync_usage(store, state, read_config(), NOW), 20)
            self.assertEqual(calls.call_count, 1)

    def test_crash_reservations_are_never_refunded_by_usage_sync(self):
        state = {'budget': {'2026-10': {'crashed': 45}}}
        with patch('archive.budget.collection', return_value=iter([])):
            self.assertEqual(sync_usage(Mock(), state, read_config(), NOW), 45)

    def test_budget_boundary_and_insufficient_budget_disables_schedule(self):
        cfg = read_config()
        self.assertTrue(has_room(950, cfg))
        self.assertFalse(has_room(951, cfg))
        store, state = Mock(), {}
        with patch('archive.budget.sync_usage', return_value=951), \
                patch('archive.budget.set_archive_enabled') as enabled:
            self.assertFalse(admit(store, state, cfg))
            enabled.assert_called_once_with(store, False)
        self.assertIn('budget_pause', state)

    def test_unreadable_usage_stops_transfers_and_disables_schedule(self):
        store, state = Mock(), {}
        with patch('archive.budget.sync_usage', side_effect=ArchiveError('API unavailable')), \
                patch('archive.budget.set_archive_enabled') as enabled:
            with self.assertRaises(ArchiveError):
                admit(store, state, read_config())
            enabled.assert_called_once_with(store, False)

    def test_reset_only_reenables_a_previous_month_budget_pause(self):
        for paused, expected in ((None, False), ({'month': '2026-10'}, False),
                                 ({'month': '2026-09'}, True)):
            store = Mock()
            store.load.return_value = {'budget_pause': paused} if paused else {}
            with patch('archive.state.StateStore', return_value=store), \
                    patch('archive.budget.utcnow', return_value=NOW), \
                    patch('archive.budget.set_archive_enabled') as enabled:
                reset_month()
                self.assertEqual(enabled.called, expected)
                if expected:
                    enabled.assert_called_once_with(store, True)


if __name__ == '__main__':
    unittest.main()

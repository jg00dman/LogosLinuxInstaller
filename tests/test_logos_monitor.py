import threading
import unittest
from unittest.mock import Mock, patch

from ou_dedetai.logos import LogosManager


class TestIssueMonitor(unittest.TestCase):
    def setUp(self):
        self.app = Mock()
        self.app.conf._logos_appdata_dir = '/logos'
        self.manager = LogosManager(self.app)
        self.addCleanup(self.manager.stop_issue_monitoring)

    def run_and_cancel(self, appdata):
        self.app.conf._logos_appdata_dir = appdata
        entered_wait = threading.Event()
        token = self.manager._issue_monitor_stop
        real_wait = token.wait
        def wait(timeout):
            entered_wait.set()
            return real_wait(timeout)
        with patch.object(token, 'wait', side_effect=wait), \
                patch('ou_dedetai.logos.get_logos_user_id', return_value=None):
            worker = threading.Thread(target=self.manager.monitor_for_potential_issues, args=(token,), daemon=True)
            worker.start()
            try:
                self.assertTrue(entered_wait.wait(2))
                self.manager.stop_issue_monitoring()
                worker.join(2)
                self.assertFalse(worker.is_alive())
            finally:
                token.set()
                worker.join(2)
        self.app.pop_up.assert_not_called()

    def test_cancel_while_waiting_for_appdata(self):
        self.run_and_cancel(None)

    def test_cancel_while_waiting_for_signin(self):
        self.run_and_cancel('/logos')

    def test_stop_logos_cancels_monitor(self):
        self.manager.stop()
        self.assertTrue(self.manager._issue_monitor_stop.is_set())

    def test_end_processes_cancels_monitor(self):
        self.manager.end_processes()
        self.assertTrue(self.manager._issue_monitor_stop.is_set())

    def test_restart_does_not_reactivate_old_monitor(self):
        first = self.manager._issue_monitor_stop
        with patch('ou_dedetai.logos.wine.get_wine_release', return_value=(None, '')), \
                patch('ou_dedetai.logos.wine.check_wine_rules', return_value=(True, None)), \
                patch('ou_dedetai.logos.wine.wineserver_kill'):
            self.manager.start()
            second = self.manager._issue_monitor_stop
            self.manager.start()
        third = self.manager._issue_monitor_stop
        self.assertTrue(first.is_set())
        self.assertTrue(second.is_set())
        self.assertFalse(third.is_set())
        monitors = [c for c in self.app.start_thread.call_args_list
                    if c.args[0] == self.manager.monitor_for_potential_issues]
        self.assertEqual(len(monitors), 2)
        self.assertIs(monitors[0].args[1], second)
        self.assertIs(monitors[1].args[1], third)
        self.assertTrue(all(c.kwargs['daemon_bool'] for c in monitors))

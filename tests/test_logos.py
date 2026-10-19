import subprocess
import threading
import unittest
from unittest.mock import Mock, patch

from ou_dedetai.logos import LogosManager, State


class TestIndexing(unittest.TestCase):
    def setUp(self):
        self.app = Mock()
        self.app.conf.logos_indexer_exe_windows_path = 'C:\\LogosIndexer.exe'
        self.manager = LogosManager(self.app)
        self.process = Mock()
        self.process.wait.return_value = 0
        self.app.start_thread.side_effect = lambda task, **kwargs: task()
        for name, value in [('run_wine_application', self.process), ('wineserver_kill', None)]:
            patcher = patch('ou_dedetai.logos.wine.' + name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch('ou_dedetai.logos.system.get_pids', return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)

    def messages(self):
        return [c.args[0] for c in self.app.status.call_args_list]

    def test_waits_for_process_not_launcher_thread(self):
        running = threading.Event()
        release = threading.Event()
        def wait(timeout):
            running.set()
            self.assertTrue(release.wait(2))
            return 0
        self.process.wait.side_effect = wait
        thread = threading.Thread(target=self.manager.index)
        thread.start()
        try:
            self.assertTrue(running.wait(2))
            self.assertEqual(self.manager.indexing_state, State.RUNNING)
            self.assertNotIn('Indexing has finished.', self.messages())
        finally:
            release.set()
            thread.join(2)
        self.assertIn('Indexing has finished.', self.messages())
        self.assertEqual(self.manager.indexing_state, State.STOPPED)

    def test_nonzero_exit_is_failure(self):
        self.process.wait.return_value = 7
        self.manager.index()
        self.assertIn('Indexing failed (exit code 7).', self.messages())
        self.assertNotIn('Indexing has finished.', self.messages())

    def test_spawn_failure(self):
        with patch('ou_dedetai.logos.wine.run_wine_application', return_value=None):
            self.manager.index()
        self.assertIn('Indexing failed: Could not start the indexer', self.messages())
        self.assertEqual(self.manager.indexing_state, State.STOPPED)

    def test_progress_while_waiting(self):
        self.process.wait.side_effect = [subprocess.TimeoutExpired('indexer', 1), 0]
        self.manager.index()
        self.assertTrue(any('Indexing is running' in m for m in self.messages()))

    def test_waits_for_remaining_indexer(self):
        child = Mock(returncode=0)
        def wait(children, timeout):
            self.assertNotIn('Indexing has finished.', self.messages())
            self.assertEqual(children, [child])
            return [child], []
        with patch('ou_dedetai.logos.system.get_pids', return_value=[child]), \
                patch('ou_dedetai.logos.psutil.wait_procs', side_effect=wait):
            self.manager.index()
        self.assertIn('Indexing has finished.', self.messages())

    def test_cancelled_is_not_success(self):
        def wait(timeout):
            self.manager._indexing_cancelled.set()
            return -9
        self.process.wait.side_effect = wait
        self.manager.index()
        self.assertIn('Indexing stopped.', self.messages())
        self.assertNotIn('Indexing has finished.', self.messages())

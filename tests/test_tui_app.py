import unittest
from unittest.mock import Mock, patch
from ou_dedetai.tui_app import TUI


class TestRuntimeMenu(unittest.TestCase):
    def test_runtime_menu_runs_on_worker(self):
        app = Mock()
        with patch('ou_dedetai.runtime.select_runtime') as select:
            TUI.main_menu_select(app, 'Select Wine runtime…')
            app.start_thread.assert_called_once()
            app.start_thread.call_args.args[0]()
        select.assert_called_once_with(app)
        app._ui_call.assert_called_once_with(app.ui.go_to_main_menu)

import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, PropertyMock, patch

from ou_dedetai import constants, runtime, utils, wine
from ou_dedetai.app import UserExitedFromAsk
from ou_dedetai.config import Config, EphemeralConfiguration, LegacyConfiguration, PersistentConfiguration
from ou_dedetai.logos import State


class TestRuntimeSelection(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.install = self.root / 'install'
        self.install.mkdir()
        self.config_path = self.root / 'config.json'
        self.app = Mock()
        self.app._wine_selection_lock = threading.Lock()
        self.app.logos.existing_processes = {}
        self.app.logos.logos_state = State.STOPPED
        self.app.logos.indexing_state = State.STOPPED
        self.conf = object.__new__(Config)
        self.app.conf = self.conf
        self.conf.app = self.app
        self.conf._raw = PersistentConfiguration(install_dir=str(self.install), faithlife_product='Logos',
                                                faithlife_product_version='10', wine_binary='/old/wine')
        self.conf._overrides = EphemeralConfiguration.from_legacy(
            LegacyConfiguration(CONFIG_FILE=str(self.config_path)))
        self.conf._installed_faithlife_product_release = '40.0.0.0'
        self.conf._wine64_path = '/old/wine64'
        self.managed = Path(self.conf.installer_binary_dir)
        self.managed.mkdir(parents=True)

    def executable(self, path, version='wine-10.0'):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'#!/bin/sh\nprintf "%s\\n" "{version}"\n')
        path.chmod(0o755)
        return path

    def image(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'\x7fELF' + bytes(4) + b'AI\x02' + bytes(20))
        path.chmod(0o644)
        return path

    def activate_image(self, path):
        with patch.object(runtime, '_check_version'):
            runtime.activate_runtime(self.app, path)

    def test_downloaded_image_is_executable_selected_and_persisted(self):
        source = self.image(self.root / 'Downloads/wine.AppImage')
        self.activate_image(source)
        installed = self.managed / source.name
        self.assertTrue(os.access(installed, os.X_OK))
        self.assertFalse(os.access(source, os.X_OK))
        self.assertEqual(self.conf.wine_binary, str(installed))
        self.assertEqual(Path(self.conf.wine64_binary).resolve(), installed)
        self.assertEqual(Path(self.conf.wineserver_binary).resolve(), installed)
        data = json.loads(self.config_path.read_text())
        self.assertEqual(self.install / data['wine_binary'], installed)
        self.conf._raw = PersistentConfiguration.load_from_path(str(self.config_path))
        self.assertEqual(self.conf.wine_binary, str(installed))
        self.assertEqual(self.conf.wineserver_binary, str(self.managed / 'wineserver'))

    def test_existing_image_switch_updates_all_links(self):
        first = self.image(self.managed / 'first.AppImage')
        second = self.image(self.managed / 'second.AppImage')
        self.activate_image(first)
        self.activate_image(second)
        for name in ('wine', 'wine64', 'wineserver', 'winetricks', self.conf.wine_appimage_link_file_name):
            self.assertEqual((self.managed / name).resolve(), second)
        self.assertTrue(first.exists())

    def test_system_wine_without_wine64(self):
        binary = self.executable(self.root / 'system/bin/wine')
        server = self.executable(binary.parent / 'wineserver')
        runtime.activate_runtime(self.app, binary)
        self.assertEqual(self.conf.wine64_binary, str(binary))
        self.assertEqual(self.conf.wineserver_binary, str(server))
        self.assertEqual(wine.get_wine_env(self.app)['WINE'], str(binary))
        self.assertEqual(wine.get_wine_env(self.app)['WINESERVER'], str(server))

    def test_custom_build_server_layout(self):
        binary = self.executable(self.root / 'build/loader/wine')
        server = self.executable(self.root / 'build/server/wineserver')
        runtime.activate_runtime(self.app, binary)
        self.assertEqual(self.conf.wineserver_binary, str(server))

    def test_explicit_server_and_path_with_spaces(self):
        binary = self.executable(self.root / 'my build/wine')
        server = self.executable(self.root / 'separate server/wineserver')
        runtime.activate_runtime(self.app, binary, server)
        self.assertEqual(self.conf.wineserver_binary, str(server))

    def test_external_prefix_similar_to_install_is_not_relativised(self):
        binary = self.executable(self.root / 'install-custom/wine')
        self.executable(binary.parent / 'wineserver')
        runtime.activate_runtime(self.app, binary)
        self.assertEqual(self.conf._raw.wine_binary, str(binary))

    def test_image_to_local_and_back(self):
        image = self.image(self.managed / 'wine.AppImage')
        self.activate_image(image)
        binary = self.executable(self.root / 'local/wine')
        server = self.executable(binary.parent / 'wineserver')
        runtime.activate_runtime(self.app, binary)
        self.assertEqual(self.conf.wine_binary, str(binary))
        self.assertEqual(self.conf.wineserver_binary, str(server))
        self.activate_image(image)
        self.assertEqual(self.conf.wine_binary, str(image))

    def test_validation_failure_preserves_previous_selection(self):
        source = self.image(self.root / 'Downloads/new.AppImage')
        with patch.object(runtime, '_check_version', side_effect=ValueError('bad runtime')):
            with self.assertRaises(ValueError):
                runtime.activate_runtime(self.app, source)
        self.assertEqual(self.conf.wine_binary, '/old/wine')
        self.assertFalse((self.managed / source.name).exists())

    def test_save_failure_restores_links_and_selection(self):
        old = self.image(self.managed / 'old.AppImage')
        self.activate_image(old)
        before = self.config_path.read_bytes()
        new = self.image(self.root / 'Downloads/new.AppImage')
        with patch.object(self.conf, 'select_wine_runtime', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.activate_image(new)
        self.assertEqual(self.conf.wine_binary, str(old))
        self.assertEqual((self.managed / 'wine').resolve(), old)
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertFalse((self.managed / new.name).exists())

    def test_second_config_write_failure_rolls_back_first(self):
        binary = self.executable(self.root / 'local/wine')
        self.executable(binary.parent / 'wineserver')
        original = b'{"wine_binary": "/old/wine"}'
        self.config_path.write_bytes(original)
        replace = os.replace
        portable = self.install / f'{constants.BINARY_NAME}.json'
        def fail_portable(source, target):
            if Path(target) == portable:
                raise OSError('read-only portable config')
            return replace(source, target)
        with patch('ou_dedetai.config.os.replace', side_effect=fail_portable):
            with self.assertRaises(OSError):
                runtime.activate_runtime(self.app, binary)
        self.assertEqual(self.config_path.read_bytes(), original)
        self.assertEqual(self.conf.wine_binary, '/old/wine')

    def test_cancel_menu_changes_nothing(self):
        self.app.ask.return_value = 'Cancel'
        runtime.select_runtime(self.app)
        self.assertEqual(self.conf.wine_binary, '/old/wine')
        self.assertFalse(self.config_path.exists())

    def test_cancel_file_dialog_changes_nothing(self):
        self.app.ask.side_effect = ['Choose another AppImage…', UserExitedFromAsk()]
        runtime.select_runtime(self.app)
        self.assertFalse(self.config_path.exists())

    def test_unknown_version_requires_confirmation(self):
        binary = self.executable(self.root / 'local/wine', 'wine-99.1 custom')
        self.executable(binary.parent / 'wineserver')
        self.app.approve.return_value = False
        with self.assertRaises(UserExitedFromAsk):
            runtime.activate_runtime(self.app, binary)
        self.assertEqual(self.conf.wine_binary, '/old/wine')
        self.app.approve.return_value = True
        runtime.activate_runtime(self.app, binary)
        self.assertEqual(self.conf.wine_binary, str(binary))

    def test_running_indexer_blocks_switch(self):
        self.app.logos.existing_processes = {'indexer': [Mock()]}
        with self.assertRaises(ValueError):
            runtime.activate_runtime(self.app, Path('/anything'))
        self.assertFalse(self.config_path.exists())

    def test_existing_non_symlink_is_not_overwritten(self):
        (self.managed / 'wine').write_text('preserve me')
        source = self.image(self.root / 'Downloads/new.AppImage')
        with self.assertRaises(ValueError):
            self.activate_image(source)
        self.assertEqual((self.managed / 'wine').read_text(), 'preserve me')
        self.assertEqual(self.conf.wine_binary, '/old/wine')

    def test_discovery_includes_wine_and_external_steam_library(self):
        local = self.executable(self.root / 'local/bin/wine')
        home = self.root / 'home'
        manifest = home / '.local/share/Steam/steamapps/libraryfolders.vdf'
        manifest.parent.mkdir(parents=True)
        library = self.root / 'extra library'
        manifest.write_text(f'"libraryfolders" {{ "1" {{ "path" "{library}" }} }}')
        proton = self.executable(library / 'steamapps/common/Proton 10/files/bin/wine')
        image = self.image(self.managed / 'installed.AppImage')
        with patch.object(Path, 'home', return_value=home), patch.dict(os.environ, {'PATH': str(local.parent)}):
            found = runtime.discover_runtimes(self.app)
        self.assertIn(local, found['System/local Wine'])
        self.assertIn(proton, found['Local Proton'])
        self.assertIn(image, found['Installed AppImages'])

    def test_recommended_update_uses_activation(self):
        self.conf._raw.wine_binary_code = 'AppImage'
        with patch.object(Config, 'wine_appimage_recommended_file_name', new_callable=PropertyMock,
                          return_value='recommended.AppImage'), \
                patch.object(utils, 'compare_recommended_appimage_version', return_value=(0, '')), \
                patch('ou_dedetai.network.download_recommended_appimage') as download, \
                patch.object(runtime, 'activate_runtime') as activate:
            utils.update_to_latest_recommended_appimage(self.app)
        download.assert_called_once_with(self.app)
        activate.assert_called_once_with(self.app, self.managed / 'recommended.AppImage')

    def test_installed_menu_marks_current_and_selects_another(self):
        old = self.image(self.managed / 'old.AppImage')
        new = self.image(self.managed / 'new.AppImage')
        self.activate_image(old)
        self.app.ask.side_effect = ['Installed AppImages', str(new)]
        with patch.object(runtime, '_check_version'):
            runtime.select_runtime(self.app)
        options = self.app.ask.call_args_list[-1].args[1]
        self.assertIn(str(old) + ' [current]', options)
        self.assertEqual(self.conf.wine_binary, str(new))

    def test_custom_menu_can_select_separate_server(self):
        binary = self.executable(self.root / 'custom/loader')
        server = self.executable(self.root / 'elsewhere/server')
        self.app.ask.side_effect = ['Custom Wine path…', str(binary), str(server)]
        runtime.select_runtime(self.app)
        self.assertEqual(self.conf.wineserver_binary, str(server))

    def test_non_executable_local_wine_is_rejected(self):
        binary = self.executable(self.root / 'custom/wine')
        binary.chmod(0o644)
        with self.assertRaises(ValueError):
            runtime.activate_runtime(self.app, binary)
        self.assertEqual(self.conf.wine_binary, '/old/wine')

    def test_same_named_download_does_not_overwrite_installed_image(self):
        old = self.image(self.managed / 'same.AppImage')
        self.activate_image(old)
        before = old.read_bytes()
        new = self.image(self.root / 'Downloads/same.AppImage')
        new.write_bytes(before + b'different build')
        with self.assertRaises(ValueError):
            self.activate_image(new)
        self.assertEqual(old.read_bytes(), before)
        self.assertEqual(self.conf.wine_binary, str(old))

    def test_timeout_preserves_selection(self):
        import subprocess
        binary = self.executable(self.root / 'custom/wine')
        self.executable(binary.parent / 'wineserver')
        with patch.object(runtime.subprocess, 'run', side_effect=subprocess.TimeoutExpired('wine', 15)):
            with self.assertRaises(subprocess.TimeoutExpired):
                runtime.activate_runtime(self.app, binary)
        self.assertEqual(self.conf.wine_binary, '/old/wine')

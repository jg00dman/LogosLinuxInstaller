import unittest
from unittest.mock import Mock
from ou_dedetai.config import Config, PersistentConfiguration


class TestExplicitRuntime(unittest.TestCase):
    def test_selected_loader_is_not_replaced_by_cached_wine64(self):
        config = object.__new__(Config)
        config._raw = PersistentConfiguration(wine_binary='/custom/wine', wine_server='/custom/server/wineserver')
        config._overrides = Mock(install_dir='/installation')
        config._wine64_path = '/previous/wine64'
        self.assertEqual(config.wine64_binary, '/custom/wine')
        self.assertEqual(config.wineserver_binary, '/custom/server/wineserver')

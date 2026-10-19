import unittest
from ou_dedetai.main import get_parser


class TestRuntimeArgument(unittest.TestCase):
    def test_select_wine_argument(self):
        args = get_parser().parse_args(['--select-wine'])
        self.assertTrue(args.select_wine)
        self.assertIsNone(args.set_appimage)

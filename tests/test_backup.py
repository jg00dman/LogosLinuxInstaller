import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
import ou_dedetai.backup as backup
from . import REPODIR
from . import TESTDATADIR


class TestBackup(unittest.TestCase):
    def setUp(self):
        self.app = Mock()
        self.app.conf = Mock()

    @unittest.skip("Not tested.")
    def test_copy_data(self):
        pass

    def test_get_all_backups(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            name = 'Logos'
            self.app.conf.faithlife_product = name
            self.app.conf.backup_dir = d
            backup_dirs = []
            for n in ('1', '2', '3'):
                nd = d / f"{name}-{n}"
                nd.mkdir(parents=True)
                backup_dirs.append(str(nd))
            
            b = backup.BackupTask(self.app)
            bdirs = b._get_all_backups()
            self.assertEqual(bdirs, backup_dirs)

    def test_get_dir_group_size(self):
        size_dir = 4096

        cmd = ['du', '-sb', str(TESTDATADIR)]
        size_du = int(subprocess.check_output(cmd).decode().split()[0])
        size_testdata = size_du + size_dir  # add in 'data' dir

        cmd = ['du', '-sb', str(REPODIR / 'snap')]
        size_du = int(subprocess.check_output(cmd).decode().split()[0])
        size_snap = size_du + size_dir*3  # add in 'snap', 'bin', 'gui' dirs

        self.app.conf.backup_dir = Path('.')
        b = backup.BackupTask(self.app)
        dirs = [TESTDATADIR, REPODIR / 'snap']
        size = b._get_dir_group_size(dirs)
        self.assertEqual(size_testdata + size_snap, size)

    def test_set_dest_dir(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self.app.conf.backup_dir = d / 'backups'
            self.app.conf._logos_appdata_dir = d / 'Logos'
            b = backup.BackupTask(self.app)
            self.assertTrue(b.destination_dir)
            self.assertEqual(b.destination_dir.parent, Path(self.app.conf.backup_dir))


class TestRestore(unittest.TestCase):
    def setUp(self):
        self.app = Mock()
        self.app.conf = Mock()

    def test_set_dest_dir(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self.app.conf.backup_dir = d / 'backups'
            self.app.conf._logos_appdata_dir = d / 'Logos'
            self.app.conf.logos_exe = self.app.conf._logos_appdata_dir / 'Logos.exe'
            r = backup.RestoreTask(self.app)
            self.assertTrue(r.destination_dir)
            self.assertEqual(r.destination_dir, Path(self.app.conf._logos_appdata_dir))

    def test_set_src_dir(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            name = 'Logos'
            self.app.conf.faithlife_product = name
            self.app.conf.backup_dir = d / 'backups'
            nd = self.app.conf.backup_dir / f"{name}-3"
            nd.mkdir(parents=True)
            src_dir = d / 'backups' / f"{name}-3"
            self.app.conf._logos_appdata_dir = d / name
            self.app.conf.logos_exe = self.app.conf._logos_appdata_dir / f'{name}.exe'
            r = backup.RestoreTask(self.app)
            r._source_dir = src_dir
            self.assertTrue(r.source_dir)
            self.assertEqual(r.source_dir, src_dir)


class TestSafeCopy(unittest.TestCase):
    def setUp(self):
        import threading
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = Mock()
        self.app.conf.backup_dir = self.root / 'backups'
        self.app.conf.faithlife_product = 'Logos'
        self.app.conf._logos_appdata_dir = self.root / 'live'
        self.app.logos.existing_processes = {}
        self.app.exit.side_effect = RuntimeError
        self.live = self.root / 'live'
        (self.live / 'Data').mkdir(parents=True)
        (self.live / 'Data' / 'book').write_text('original')
        self.source = self.root / 'backups' / 'Logos-test'
        (self.source / 'Data').mkdir(parents=True)
        (self.source / 'Data' / 'book').write_text('restored')
        self.app.ask.return_value = str(self.source)

        def start(task, *args, **kwargs):
            thread = threading.Thread(target=task, args=args)
            thread.start()
            return thread
        self.app.start_thread.side_effect = start

    def test_successful_restore(self):
        backup.restore(self.app)
        self.assertEqual((self.live / 'Data' / 'book').read_text(), 'restored')
        self.assertFalse(list(self.live.glob('.oudedetai-copy-*')))
        self.assertIn('Finished restore', self.app.status.call_args.args[0])

    def test_no_space_preserves_original(self):
        from unittest.mock import patch
        with patch.object(backup.utils, 'enough_disk_space', return_value=False):
            backup.restore(self.app)
        self.assertEqual((self.live / 'Data' / 'book').read_text(), 'original')
        self.assertIn('failed', self.app.info.call_args.args[0])

    def test_worker_error_is_not_success(self):
        from unittest.mock import patch
        with patch.object(backup.shutil, 'copytree', side_effect=OSError('disk failure')):
            backup.restore(self.app)
        self.assertEqual((self.live / 'Data' / 'book').read_text(), 'original')
        self.assertFalse(any('Finished' in c.args[0] for c in self.app.status.call_args_list))

    def test_verification_error_preserves_original(self):
        from unittest.mock import patch
        with patch.object(backup.BackupBase, '_copy_and_verify', side_effect=OSError('verification failed')):
            backup.restore(self.app)
        self.assertEqual((self.live / 'Data' / 'book').read_text(), 'original')

    def test_replace_failure_rolls_back(self):
        from unittest.mock import patch
        rename = Path.rename
        def fail_new(path, target):
            if path.name == 'Data' and path.parent.name.startswith('.oudedetai-copy-'):
                raise OSError('cannot install copy')
            return rename(path, target)
        with patch.object(Path, 'rename', fail_new):
            backup.restore(self.app)
        self.assertEqual((self.live / 'Data' / 'book').read_text(), 'original')

    def test_failed_backup_not_offered(self):
        from unittest.mock import patch
        with patch.object(backup.shutil, 'copytree', side_effect=OSError('disk failure')):
            backup.backup(self.app)
        self.assertEqual(backup.RestoreTask(self.app)._get_all_backups(), [str(self.source)])

    def test_completed_backup_is_offered(self):
        backup.backup(self.app)
        candidates = backup.RestoreTask(self.app)._get_all_backups()
        self.assertEqual(len(candidates), 2)
        copied = next(Path(p) for p in candidates if p != str(self.source))
        self.assertEqual((copied / 'Data' / 'book').read_text(), 'original')

    def test_empty_backup_list_is_explained(self):
        backup.shutil.rmtree(self.source)
        backup.restore(self.app)
        self.assertIn('No completed backups', self.app.info.call_args.args[0])

    def test_running_logos_blocks_copy(self):
        process = Mock()
        process.is_running.return_value = True
        self.app.logos.existing_processes = {'Logos': [process]}
        backup.restore(self.app)
        self.app.start_thread.assert_not_called()

    def test_corrupt_copy_is_detected(self):
        from unittest.mock import patch
        copy_dirs = backup.BackupBase._copy_dirs
        def corrupt(task, sources, destination):
            copy_dirs(task, sources, destination)
            (destination / 'Data' / 'book').write_text('corrupt')
        with patch.object(backup.BackupBase, '_copy_dirs', corrupt):
            backup.restore(self.app)
        self.assertEqual((self.live / 'Data' / 'book').read_text(), 'original')
        self.assertIn('verification failed', self.app.info.call_args.args[0])

    def test_failed_rollback_retains_originals(self):
        from unittest.mock import patch
        rename = Path.rename
        def fail_new_and_rollback(path, target):
            if path.parent.name.startswith('.oudedetai-copy-') or path.parent.name == 'originals':
                raise OSError('rename failed')
            return rename(path, target)
        with patch.object(Path, 'rename', fail_new_and_rollback):
            backup.restore(self.app)
        recovered = list(self.live.glob('.oudedetai-copy-*/originals/Data/book'))
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].read_text(), 'original')

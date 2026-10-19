import abc
import queue
import hashlib
import tempfile
from concurrent.futures import Future
import logging
import shutil
from pathlib import Path
from typing import List, Optional
from typing import Tuple
from ou_dedetai import constants
from ou_dedetai import utils
from ou_dedetai.app import App


class BackupBase(abc.ABC):
    DATA_DIRS = ['Data', 'Documents', 'Users']

    def __init__(
        self,
        app: App,
        mode: str,
    ) -> None:
        self.app = app
        self.mode = mode
        self._destination_dir: Optional[Path] = None
        self._source_dir: Optional[Path] = None
        self.data_size = 1
        self.destination_disk_used_init: Optional[int] = None
        self.q: queue.Queue[int] = queue.Queue()
        if not self.app.approve(f"Use existing backups folder \"{self.app.conf.backup_dir}\"?"):
            # Reset backup dir.
            # The app will re-prompt next time the backup_dir is accessed
            app.conf._raw.backup_dir = None
        self.backup_dir = Path(self.app.conf.backup_dir).expanduser().resolve()
        try:
            self.backup_dir.mkdir(exist_ok=True, parents=True)
        except PermissionError:
            m = f"folder not accessible: {self.backup_dir}"
            if constants.RUNMODE == 'snap':
                m += f"{m}\n\nTry connecting removable media:\nsnap connect {constants.BINARY_NAME}:removable-media\n"
            self.app.exit(m)

    def _copy_dirs(
            self,
            src_dirs: List[str|Path] | Tuple[str|Path],
            dst_dir: Path|str,
        ) -> None:
        # logging.debug("starting _copy_dirs")
        for src in src_dirs:
            if not isinstance(src, Path):
                src = Path(src)
            logging.debug(f"copying \"{src}\" to \"{dst_dir}/{src.name}\"")
            shutil.copytree(src, Path(dst_dir) / src.name)

    def _get_all_backups(self) -> List[str]:
        all_backups = [
            str(d) 
            for d in self.backup_dir.glob('*') 
            if d.is_dir() and d.name.startswith(self.app.conf.faithlife_product) and not d.name.endswith('.incomplete')
        ]
        all_backups.sort()
        logging.debug(all_backups)
        return all_backups

    def _get_copy_percentage(self) -> int:
        disk_used = self._get_dest_disk_used()
        # This should already be set by run, but in case it isn't
        if not self.destination_disk_used_init:
            self.destination_disk_used_init = disk_used

        delta = disk_used - self.destination_disk_used_init
        percent = int(delta * 100 / self.data_size)
        # logging.debug(f"{percent=}")
        return percent

    def _get_dest_disk_used(self) -> int:
        return shutil.disk_usage(self.destination_dir).used

    def _get_dir_group_size(
        self,
        dirs: List[Path] | Tuple[Path],
    ) -> int:
        size = utils.get_folder_group_size(dirs)
        logging.debug(f"backup {size=}")
        return size

    def _get_source_subdirs(self) -> List[Path]:
        dirs = [self.source_dir / d for d in self.DATA_DIRS if (self.source_dir / d).is_dir()]
        if not dirs:
            self.app.exit(f"there are no files to {self.mode}")
        return dirs

    def _copy_and_verify(self, src_dirs: List[Path], stage: Path) -> None:
        self._copy_dirs(src_dirs, stage)
        for src in src_dirs:
            for original in src.rglob('*'):
                if original.is_file():
                    copied = stage / src.name / original.relative_to(src)
                    with original.open('rb') as source, copied.open('rb') as target:
                        source_hash = hashlib.file_digest(source, 'sha256').digest()
                        if source_hash != hashlib.file_digest(target, 'sha256').digest():
                            raise OSError(f"Copy verification failed: {original}")

    def _commit_restore(self, stage: Path, src_dirs: List[Path]) -> None:
        """Keep originals until all replacements succeed; retain recovery files if rollback fails."""
        originals = stage / 'originals'
        originals.mkdir()
        moved = []
        installed = []
        try:
            for src in src_dirs:
                destination = self.destination_dir / src.name
                if destination.exists():
                    destination.rename(originals / src.name)
                    moved.append(src.name)
                (stage / src.name).rename(destination)
                installed.append(src.name)
        except BaseException:
            try:
                for name in reversed(installed):
                    (self.destination_dir / name).rename(stage / name)
                for name in reversed(moved):
                    (originals / name).rename(self.destination_dir / name)
            except OSError as error:
                raise OSError(f"Restore rollback failed. Original data retained in {originals}: {error}") from error
            raise

    def _run(self) -> None:
        stage = None
        try:
            self.app.logos.get_logos_pids()
            if any(p.is_running() for group in self.app.logos.existing_processes.values() for p in group):
                self.app.info("Stop Logos and its indexer before backing up or restoring data.")
                return
            src_dirs = self._get_source_subdirs()
            self.app.status(f"Running {self.mode} from {self.source_dir} to {self.destination_dir}")
            if any(self.destination_dir.resolve().is_relative_to(src.resolve()) for src in src_dirs):
                raise OSError("Destination must not be inside the data being copied.")
            self.data_size = max(1, self._get_dir_group_size(src_dirs))
            self._verify_disk_space()
            self.destination_disk_used_init = self._get_dest_disk_used()
            stage = Path(tempfile.mkdtemp(prefix='.oudedetai-copy-', dir=self.destination_dir))
            result = Future()

            def copy():
                try:
                    self._copy_and_verify(src_dirs, stage)
                except BaseException as error:
                    result.set_exception(error)
                else:
                    result.set_result(None)

            worker = self.app.start_thread(copy)
            try:
                while worker.is_alive():
                    self.app.status("Copying and verifying…", self._get_copy_percentage())
                    worker.join(timeout=0.5)
            finally:
                # Do not remove staging while the copy thread is still writing.
                worker.join()
            result.result()
            self._commit_restore(stage, src_dirs)
            shutil.rmtree(stage)
            stage = None
            if self.mode == 'backup':
                completed = self.destination_dir.with_suffix('')
                self.destination_dir.rename(completed)
                self._destination_dir = completed
            self.app.status(f"Finished {self.mode}. {self.data_size} bytes copied and verified.")
        except (OSError, KeyboardInterrupt) as error:
            logging.exception("%s failed", self.mode)
            self.app.info(f"{self.mode.capitalize()} failed: {error}")
        finally:
            if stage is not None:
                # A failed rollback may leave the only original data here.
                originals = stage / 'originals'
                if originals.exists() and any(originals.iterdir()):
                    self.app.info(f"Recovery data retained at {originals}")
                else:
                    shutil.rmtree(stage, ignore_errors=True)

    def _verify_disk_space(self) -> None:
        if not utils.enough_disk_space(self.destination_dir, self.data_size):
            raise OSError(f"Not enough free disk space for {self.mode}.")

    @property
    def source_dir(self) -> Path:
        if not self._source_dir:
            self._source_dir = self._get_source_dir()
        return self._source_dir

    @abc.abstractmethod
    def _get_source_dir(self) -> Path:
        """Source path. Differs depending on backup/restore"""
        raise NotImplementedError

    @property
    def destination_dir(self) -> Path:
        if not self._destination_dir:
            self._destination_dir = self._get_destination_dir()
        return self._destination_dir

    @abc.abstractmethod
    def _get_destination_dir(self) -> Path:
        """Destination path. Differs depending on backup/restore"""
        raise NotImplementedError


class BackupTask(BackupBase):
    def __init__(self, app: App) -> None:
        super().__init__(app, 'backup')
        self.description = 'Use'

    def run(self) -> None:
        """Run the backup task."""
        self._run()

    def _get_source_dir(self) -> Path:
        if self.app.conf._logos_appdata_dir is None:
            self.app.exit("Cannot backup when product is not installed.")
        return Path(self.app.conf._logos_appdata_dir).expanduser().resolve()

    def _get_destination_dir(self) -> Path:
        """Destination path. Differs depending on backup/restore"""
        timestamp = utils.get_timestamp().replace('-', '')
        name = f"{self.app.conf.faithlife_product}-{timestamp}.incomplete"
        destination_dir = self.backup_dir / name
        logging.debug(f"Backup directory path: {destination_dir}.")

        if destination_dir.with_suffix('').exists():
            raise FileExistsError(f"Backup already exists: {destination_dir.with_suffix('')}")
        destination_dir.mkdir()
        return destination_dir


class RestoreTask(BackupBase):
    def __init__(self, app: App) -> None:
        super().__init__(app, 'restore')

    def run(self) -> None:
        """Run the restore task."""
        self._run()

    def _get_destination_dir(self) -> Path:
        if self.app.conf._logos_appdata_dir is None:
            self.app.exit("Cannot backup when product is not installed.")
        return Path(self.app.conf._logos_appdata_dir).expanduser().resolve()

    def _get_source_dir(self) -> Path:
        all_backups = self._get_all_backups()
        if not all_backups:
            raise OSError("No completed backups are available.")
        latest = all_backups.pop(-1)

        # Offer to restore the most recent backup.
        options = [latest, *all_backups]
        src_dir = self.app.ask("Choose backup folder to restore: ", options)

        return Path(src_dir)


def backup(app: App) -> None:
    backup = BackupTask(app)
    backup.run()


def restore(app: App) -> None:
    restore = RestoreTask(app)
    restore.run()

"""Select the Wine runtime for an existing installation, independently of the UI."""
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading

from ou_dedetai import constants, system, utils, wine
from ou_dedetai.app import App, UserExitedFromAsk


def discover_runtimes(app: App) -> dict[str, list[Path]]:
    managed = Path(app.conf.installer_binary_dir)
    images = sorted(p for p in managed.glob('*') if p.is_file() and not p.is_symlink()
                    and p.suffix.lower() == '.appimage')
    local = set()
    for directory in os.get_exec_path() + [str(Path.home() / 'bin'), '/usr/local/bin']:
        for name in ('wine', 'wine64'):
            path = Path(directory) / name
            if path.is_file() and os.access(path, os.X_OK) and path.parent.resolve() != managed.resolve():
                local.add(path.absolute())
    steam_roots = [Path.home() / '.steam/steam', Path.home() / '.local/share/Steam',
                   Path.home() / '.var/app/com.valvesoftware.Steam/.local/share/Steam']
    libraries = set(steam_roots)
    for root in steam_roots:
        manifest = root / 'steamapps/libraryfolders.vdf'
        if manifest.is_file():
            libraries.update(Path(p.replace('\\\\', '\\')) for p in
                             re.findall(r'"path"\s+"([^"]+)"', manifest.read_text()))
    proton = set()
    for root in libraries:
        builds = list((root / 'steamapps/common').glob('Proton*'))
        builds += list((root / 'compatibilitytools.d').glob('*'))
        for build in builds:
            for layout in ('files/bin', 'dist/bin'):
                for name in ('wine', 'wine64'):
                    path = build / layout / name
                    if path.is_file() and os.access(path, os.X_OK):
                        proton.add(path.absolute())
    return {'Installed AppImages': images, 'System/local Wine': sorted(local - proton),
            'Local Proton': sorted(proton)}


def _ensure_stopped(app: App) -> None:
    from ou_dedetai.logos import State
    if app.logos.logos_state in (State.STARTING, State.STOPPING) or app.logos.indexing_state in (
        State.STARTING, State.STOPPING
    ):
        raise ValueError('Wait for Logos and its indexer to stop before changing Wine.')
    app.logos.get_logos_pids()
    if any(p.is_running() for group in app.logos.existing_processes.values() for p in group):
        raise ValueError('Stop Logos and its indexer before changing Wine.')


def _check_version(app: App, binary: Path) -> None:
    result = subprocess.run([str(binary), '--version'], capture_output=True, text=True,
                            timeout=15, env=system.fix_ld_library_path(None))
    if result.returncode != 0:
        raise ValueError(f'Cannot run {binary}: {result.stderr.strip()}')
    match = re.search(r'wine-(\d+)\.(\d+)', result.stdout, re.IGNORECASE)
    if match:
        major, minor = map(int, match.groups())
        release = wine.WineRelease(major, minor, 'staging' if 'staging' in result.stdout.lower()
                                   else ('stable' if minor == 0 else 'devel'))
        allowed, reason = wine.check_wine_rules(release, app.conf.installed_faithlife_product_release,
                                                app.conf.faithlife_product_version)
        if allowed and major in (7, 8, 9, 10):
            return
        warning = reason if not allowed else 'This Wine version is not covered by the current compatibility rules.'
    else:
        warning = 'This runtime reports an unrecognised Wine version.'
    if not app.approve(f'{warning}\n{result.stdout.strip()}\nUse this developer runtime anyway?'):
        raise UserExitedFromAsk


def _server_for(binary: Path) -> Path | None:
    for path in (binary.parent / 'wineserver', binary.parent / 'server/wineserver',
                 binary.parent.parent / 'server/wineserver'):
        if path.is_file() and os.access(path, os.X_OK):
            return path.absolute()
    return None


def activate_runtime(app: App, selected: Path, server: Path | None = None) -> None:
    """Validate first; restore runtime links if saving the selection fails."""
    _ensure_stopped(app)
    selected = selected.expanduser().absolute()
    if not selected.is_file():
        raise ValueError(f'Wine runtime does not exist: {selected}')
    managed = Path(app.conf.installer_binary_dir)
    is_image = selected.suffix.lower() == '.appimage'
    snapshots: dict[Path, str | None] = {}
    created_image = None
    old_mode = None
    try:
        if is_image:
            if not utils.check_appimage(selected):
                raise ValueError(f'Not a supported AppImage: {selected}')
            managed.mkdir(parents=True, exist_ok=True)
            if selected.name == app.conf.wine_appimage_link_file_name:
                raise ValueError("Choose the versioned AppImage file, not the selected-runtime symlink.")
            destination = managed / selected.name
            if selected != destination:
                if destination.exists() or destination.is_symlink():
                    # Do not overwrite an existing runtime with a same-named download.
                    raise ValueError(f'{destination} already exists. '
                                     'Select that installed AppImage or rename the download.')
                created_image = destination
                shutil.copy2(selected, destination)
            selected = destination
            old_mode = selected.stat().st_mode
            selected.chmod(old_mode | 0o100)
            server = managed / 'wineserver'
        else:
            if not os.access(selected, os.X_OK):
                raise ValueError(f'Wine executable is not executable: {selected}')
            server = server or _server_for(selected)
            if server is None:
                raise ValueError('Cannot locate the matching wineserver; select it explicitly.')
            server = server.expanduser().absolute()
            if not server.is_file() or not os.access(server, os.X_OK):
                raise ValueError(f'wineserver is not executable: {server}')
        _check_version(app, selected)
        _ensure_stopped(app)
        if is_image:
            targets = {managed / app.conf.wine_appimage_link_file_name: './' + selected.name}
            targets.update({managed / name: './' + app.conf.wine_appimage_link_file_name
                            for name in ('wine', 'wine64', 'wineserver', 'winetricks')})
            for link in targets:
                if link.exists() and not link.is_symlink():
                    raise ValueError(f'Refusing to replace a non-symlink runtime file: {link}')
            for link, target in targets.items():
                snapshots[link] = os.readlink(link) if link.is_symlink() else None
                with tempfile.TemporaryDirectory(dir=managed) as temporary:
                    replacement = Path(temporary) / 'link'
                    replacement.symlink_to(target)
                    os.replace(replacement, link)
        assert server is not None
        app.conf.select_wine_runtime(str(selected), str(server), 'AppImage' if is_image else 'Custom')
    except BaseException:
        for link, target in snapshots.items():
            link.unlink(missing_ok=True)
            if target is not None:
                link.symlink_to(target)
        if created_image is not None:
            created_image.unlink(missing_ok=True)
        elif old_mode is not None:
            selected.chmod(old_mode)
        raise
    app.status(f'Selected Wine runtime: {selected}')


def select_runtime(app: App) -> None:
    # Prevent simultaneous selection dialogs from racing to update the runtime.
    if not hasattr(app, '_wine_selection_lock'):
        app._wine_selection_lock = threading.Lock()
    with app._wine_selection_lock:
        try:
            _ensure_stopped(app)
            choices = discover_runtimes(app)
            category = app.ask(f'Current Wine: {app.conf.wine_binary}\nSelect Wine runtime',
                               [*choices, 'Custom Wine path…', 'Choose another AppImage…', 'Cancel'])
            if category == 'Cancel':
                return
            server = None
            if category in choices:
                paths = choices[category]
                if not paths:
                    app.info(f'No runtimes found in {category}. Use a custom path or choose another AppImage.')
                    return
                labels = {str(p) + (' [current]' if p == Path(app.conf.wine_binary) else ''): p for p in paths}
                choice = app.ask(category, [*labels, 'Cancel'])
                if choice == 'Cancel':
                    return
                selected = labels[choice]
            else:
                selected = Path(app.ask('Choose Wine executable' if category == 'Custom Wine path…'
                                        else 'Choose an AppImage', [constants.PROMPT_OPTION_FILE]))
                if category == 'Choose another AppImage…' and selected.suffix.lower() != '.appimage':
                    raise ValueError('Select a Wine AppImage file.')
                if category == 'Custom Wine path…':
                    server = _server_for(selected)
                    if server is None:
                        server = Path(app.ask('Choose the matching wineserver executable',
                                              [constants.PROMPT_OPTION_FILE]))
            activate_runtime(app, selected, server)
        except UserExitedFromAsk:
            return
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            logging.exception('Wine selection failed')
            app.info(f'Wine selection failed: {error}')

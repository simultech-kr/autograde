import os
import stat
from pathlib import Path

import pytest

from autograde.settings import AppPaths


def test_app_paths_create_isolated_runtime_tree(tmp_path: Path) -> None:
    paths = AppPaths.from_value(tmp_path / "runtime").ensure()

    assert paths.database == tmp_path / "runtime" / "state.sqlite3"
    assert paths.cache.is_dir()
    assert paths.snapshots.is_dir()
    assert paths.workspaces.is_dir()
    assert paths.reports.is_dir()
    for path in (
        paths.root,
        paths.cache,
        paths.snapshots,
        paths.workspaces,
        paths.reports,
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_data_root_symlink_is_rejected_without_mutating_target(tmp_path: Path) -> None:
    target = tmp_path / "outside"
    target.mkdir(mode=0o755)
    root = tmp_path / "runtime-link"
    root.symlink_to(target, target_is_directory=True)

    with pytest.raises(OSError, match="symlink"):
        AppPaths.from_value(root).ensure()

    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert not (target / "cache").exists()


def test_app_paths_normalizes_existing_managed_directory_permissions(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    cache = root / "cache"
    cache.mkdir(parents=True)
    root.chmod(0o755)
    cache.chmod(0o777)

    paths = AppPaths.from_value(root).ensure()

    assert stat.S_IMODE(paths.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.cache.stat().st_mode) == 0o700


def test_app_paths_rejects_existing_managed_directory_symlink(tmp_path: Path) -> None:
    paths = AppPaths.from_value(tmp_path / "runtime")
    paths.root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, paths.cache)

    with pytest.raises(OSError, match="must not be a symlink"):
        paths.ensure()

"""Filesystem settings for one autograde installation."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppPaths:
    """Resolved paths for persistent state and generated artifacts."""

    root: Path

    @classmethod
    def from_value(cls, value: str | Path) -> "AppPaths":
        # Keep the configured path lexical until ``ensure`` has had a chance
        # to lstat and reject a managed-root symlink.  ``resolve`` here would
        # silently dereference it and make the later safety check ineffective.
        return cls(Path(value).expanduser().absolute())

    @property
    def database(self) -> Path:
        return self.root / "state.sqlite3"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def snapshots(self) -> Path:
        return self.root / "snapshots"

    @property
    def workspaces(self) -> Path:
        return self.root / "workspaces"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    @property
    def bundles(self) -> Path:
        """Content-addressed starter, submission, and instructor bundles."""

        return self.root / "bundles"

    @property
    def instructor_inputs(self) -> Path:
        """Materialized immutable assessment/data trees for bundle releases."""

        return self.root / "instructor-inputs"

    @property
    def platform_auth_secret(self) -> Path:
        return self.root / "platform-auth-secret"

    @property
    def platform_instructor_token(self) -> Path:
        return self.root / "platform-instructor-token"

    def ensure(self) -> "AppPaths":
        for path in (
            self.root,
            self.cache,
            self.snapshots,
            self.workspaces,
            self.reports,
            self.bundles,
            self.instructor_inputs,
        ):
            if os.path.lexists(path) and path.is_symlink():
                raise OSError(f"managed directory must not be a symlink: {path}")
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not path.is_dir():
                raise NotADirectoryError(f"managed path is not a directory: {path}")
            path.chmod(0o700)
        return self

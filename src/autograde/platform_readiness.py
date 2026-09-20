"""Cheap operational readiness; deliberately not a security certification."""
from __future__ import annotations

import os
import shutil
import sqlite3
import threading
from typing import Sequence

from .platform_bundle_worker import BundleSubmissionWorker
from .settings import AppPaths


class PortalReadiness:
    """Check both pools, existing SQLite and local storage without migrations.

    Opening SQLite in read-only mode avoids silently recreating a deleted DB.
    This is not a write probe, integrity scan, runtime probe or queue-age check.
    The default 1 GiB reserve is a readiness warning, not an upload quota.
    """

    def __init__(self, paths: AppPaths, workers: Sequence[BundleSubmissionWorker],
                 stopping: threading.Event, *, min_free_bytes: int = 1024 ** 3,
                 module_checks=None):
        if isinstance(min_free_bytes, bool) or not isinstance(min_free_bytes, int) or min_free_bytes < 0:
            raise ValueError("min_free_bytes must be a non-negative integer")
        self.paths = paths
        self.workers = workers
        self.stopping = stopping
        self.min_free_bytes = min_free_bytes
        self.module_checks = dict(module_checks or {})

    def _core_ready(self) -> bool:
        if self.stopping.is_set() or not self.workers or not all(worker.healthy for worker in self.workers):
            return False
        try:
            for path in (self.paths.root, self.paths.bundles, self.paths.workspaces,
                         self.paths.instructor_inputs):
                if path.is_symlink() or not path.is_dir() or not os.access(path, os.R_OK | os.W_OK | os.X_OK):
                    return False
                if shutil.disk_usage(path).free < self.min_free_bytes:
                    return False
            if self.paths.database.is_symlink() or not self.paths.database.is_file():
                return False
            connection = sqlite3.connect(self.paths.database.as_uri() + "?mode=ro", uri=True, timeout=0.1)
            try:
                connection.execute("SELECT state FROM bundle_submission_requests LIMIT 1").fetchone()
                connection.execute("SELECT assignment_id FROM bundle_assignment_releases LIMIT 1").fetchone()
            finally:
                connection.close()
        except (OSError, sqlite3.Error):
            return False
        return True

    def status(self):
        """Fixed module names and booleans only; never expose paths or secrets."""
        result = {'core': self._core_ready()}
        for name, check in self.module_checks.items():
            try:
                outcome = check()
                result[name] = outcome is None or outcome is True
            except (OSError, sqlite3.Error, ValueError):
                result[name] = False
        return result

    def __call__(self) -> bool:
        return all(self.status().values())

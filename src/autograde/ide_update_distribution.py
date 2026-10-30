"""Read-only publication of signed IDE releases, independent of student accounts.

The operator publishes into this dedicated directory. Trust is checked by the
launcher using its pinned publisher and institution keys, never by discovery.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import stat

from .platform_service import PlatformAPIError, PlatformFileResponse

MAX_UPDATE_FEED_BYTES = 32 * 1024 * 1024
MAX_UPDATE_ARTIFACT_BYTES = 2 * 1024 * 1024 * 1024
_ARTIFACT = re.compile(r"[0-9a-f]{64}\.agupdate\.gz\Z")


class IDEUpdateDistribution:
    """Serve only the public feed and content-addressed update artifacts."""

    def __init__(self, root: Path):
        self.root = Path(root).absolute()

    def download(self, channel: str, name: str, platform: str | None = None,
                 architecture: str | None = None) -> PlatformFileResponse:
        if channel not in {"pilot", "production"} or (
            name != "feed.json" and _ARTIFACT.fullmatch(name) is None
        ) or (platform, architecture) != (None, None) and (
            platform not in {"darwin", "linux", "win32"} or architecture not in {"arm64", "x64"}
        ):
            raise self._missing()
        parts = [channel] + ([platform, architecture] if platform else [])
        directory = self.root
        try:
            # Keep the lexical root: resolving it first would hide a linked
            # ancestor and let a re-pointed publication escape the managed tree.
            for ancestor in (directory, *directory.parents):
                self._directory(ancestor)
            for part in parts:
                directory /= part
                self._directory(directory)
            target = directory / name
            info = target.lstat()
            limit = MAX_UPDATE_FEED_BYTES if name == "feed.json" else MAX_UPDATE_ARTIFACT_BYTES
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= limit:
                raise ValueError("Invalid public update file")
            if name == "feed.json":
                # Reject accidental publication of unrelated JSON or keys. The
                # launcher still verifies every signature and all compatibility.
                with target.open("rb") as source:
                    feed = json.loads(source.read(MAX_UPDATE_FEED_BYTES + 1))
                if not isinstance(feed, dict) or feed.get("algorithm") != "Ed25519" or (
                    not isinstance(feed.get("document"), dict)
                    or feed["document"].get("kind") != "autograde.ide-update.v1"
                    or not isinstance(feed.get("keyId"), str)
                    or not isinstance(feed.get("signature"), str)
                ):
                    raise ValueError("Invalid public update feed")
        except FileNotFoundError as exc:
            raise self._missing() from exc
        except (OSError, ValueError, TypeError) as exc:
            raise PlatformAPIError(503, "ide_update_unavailable", "IDE update publication is unavailable") from exc
        return PlatformFileResponse(200, target,
            "application/json; charset=utf-8" if name == "feed.json" else "application/gzip",
            public_ide_update=True)

    @staticmethod
    def _directory(path: Path) -> None:
        if not stat.S_ISDIR(path.lstat().st_mode):
            raise ValueError("Public update directories cannot be symlinks")

    @staticmethod
    def _missing() -> PlatformAPIError:
        return PlatformAPIError(404, "not_found", "IDE update publication not found")

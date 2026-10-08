"""Pure helpers: path checks and shared-data (cross-seed) detection."""

from __future__ import annotations

import os
import posixpath


def is_under(path: str, root: str) -> bool:
    path = posixpath.normpath(path)
    root = posixpath.normpath(root)
    return path == root or path.startswith(root.rstrip("/") + "/")


def absolute_files(save_path: str, files: list[dict]) -> set[str]:
    return {posixpath.normpath(posixpath.join(save_path, f["name"])) for f in files}


def sharing_torrents(target_hash: str, file_sets: dict[str, set[str]]) -> set[str]:
    """Hashes of other torrents that use at least one of the target torrent's files (same path)."""
    mine = file_sets.get(target_hash, set())
    return {h for h, fs in file_sets.items() if h != target_hash and fs & mine}


def hardlinked_files(paths: set[str]) -> list[str]:
    """Files that have more than one hard link, i.e. are also used under another path (cross-seed by link)."""
    linked = []
    for p in sorted(paths):
        try:
            if os.stat(p).st_nlink > 1:
                linked.append(p)
        except FileNotFoundError:
            continue
    return linked

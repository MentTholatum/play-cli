from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from play_cli import sound_pack_storage as storage


def test_agent_failed_commit_write_never_unlinks_a_replacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    parent_fd = storage.open_directory(tmp_path)
    replacement_name: str | None = None

    def replace_with_keep(source: str, source_parent_fd: int) -> None:
        nonlocal replacement_name
        replacement_name = source
        os.rename(
            source,
            ".preserved-original",
            src_dir_fd=source_parent_fd,
            dst_dir_fd=source_parent_fd,
        )
        replacement_fd = os.open(
            source,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=source_parent_fd,
        )
        try:
            os.write(replacement_fd, b"KEEP")
        finally:
            os.close(replacement_fd)
        raise OSError(errno.EIO, "injected commit failure")

    def racing_replace(
        source: str,
        destination: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        del destination, dst_dir_fd
        replace_with_keep(source, src_dir_fd)

    def racing_noreplace(
        source_parent_fd: int,
        source: str,
        destination_parent_fd: int,
        destination: str,
    ) -> None:
        del destination_parent_fd, destination
        replace_with_keep(source, source_parent_fd)

    monkeypatch.setattr(os, "replace", racing_replace)
    monkeypatch.setattr(storage, "rename_noreplace", racing_noreplace)
    try:
        with pytest.raises(OSError, match="injected commit failure"):
            storage.write_atomic(parent_fd, "receipt.json", b"receipt")
    finally:
        os.close(parent_fd)

    assert replacement_name is not None
    assert (tmp_path / replacement_name).read_bytes() == b"KEEP"

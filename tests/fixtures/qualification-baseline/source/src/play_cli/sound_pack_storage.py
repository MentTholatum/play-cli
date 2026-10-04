from __future__ import annotations

import ctypes
import errno
import fcntl
import os
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, cast

_CLOSE_ON_EXEC = getattr(os, "O_CLOEXEC", 0)
_NO_FOLLOW = getattr(os, "O_NOFOLLOW", 0)
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | _NO_FOLLOW | _CLOSE_ON_EXEC
FILE_FLAGS = os.O_RDONLY | _NO_FOLLOW | _CLOSE_ON_EXEC
_DARWIN_RENAME_EXCL = 0x4
_LINUX_RENAME_NOREPLACE = 1


@dataclass(frozen=True)
class DirectoryEntry:
    name: str
    mode: int


@dataclass(frozen=True)
class StoredFile:
    path: str
    data: bytes


class CFunction(Protocol):
    argtypes: list[Any]
    restype: Any

    def __call__(self, *args: Any) -> int: ...


def absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def reject_existing_symlink_components(path: Path) -> None:
    absolute = absolute_path(path)
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise OSError(errno.ELOOP, f"Path component is a symlink: {current}")


def _walk_directory(
    path: Path, *, create_missing: bool, return_none_if_missing: bool
) -> int | None:
    absolute = absolute_path(path)
    descriptor: int | None = None
    try:
        descriptor = os.open(absolute.anchor, DIRECTORY_FLAGS)
        for component in absolute.parts[1:]:
            try:
                next_descriptor = open_directory_at(descriptor, component)
            except FileNotFoundError:
                if return_none_if_missing:
                    os.close(descriptor)
                    descriptor = None
                    return None
                if not create_missing:
                    raise
                try:
                    os.mkdir(component, dir_fd=descriptor)
                except FileExistsError:
                    pass
                next_descriptor = open_directory_at(descriptor, component)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise


def open_directory(path: Path) -> int:
    descriptor = _walk_directory(path, create_missing=False, return_none_if_missing=False)
    assert descriptor is not None
    return descriptor


def open_directory_if_present(path: Path) -> int | None:
    return _walk_directory(path, create_missing=False, return_none_if_missing=True)


def open_or_create_directory(path: Path) -> int:
    descriptor = _walk_directory(path, create_missing=True, return_none_if_missing=False)
    assert descriptor is not None
    return descriptor


def open_directory_at(parent_fd: int, name: str) -> int:
    return os.open(name, DIRECTORY_FLAGS, dir_fd=parent_fd)


def open_exclusive_lock_at(parent_fd: int, name: str) -> int:
    descriptor: int | None = None
    try:
        try:
            descriptor = os.open(
                name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | _NO_FOLLOW | _CLOSE_ON_EXEC,
                0o600,
                dir_fd=parent_fd,
            )
        except FileExistsError:
            descriptor = os.open(
                name,
                os.O_RDWR | _NO_FOLLOW | _CLOSE_ON_EXEC,
                dir_fd=parent_fd,
            )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(errno.EINVAL, f"Lock entry is not a regular file: {name}")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return descriptor
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise


def stat_at(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def list_directory(directory_fd: int) -> tuple[DirectoryEntry, ...]:
    entries: list[DirectoryEntry] = []
    for entry in os.scandir(directory_fd):
        entry_stat = entry.stat(follow_symlinks=False)
        entries.append(
            DirectoryEntry(
                name=entry.name,
                mode=entry_stat.st_mode,
            )
        )
    return tuple(sorted(entries, key=lambda entry: entry.name))


def read_file_at(directory_fd: int, name: str) -> bytes:
    descriptor = open_regular_file_at(directory_fd, name)
    try:
        return read_open_file(descriptor)
    finally:
        os.close(descriptor)


def open_regular_file_at(directory_fd: int, name: str) -> int:
    descriptor: int | None = None
    try:
        descriptor = os.open(name, FILE_FLAGS, dir_fd=directory_fd)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(errno.EINVAL, f"Entry is not a regular file: {name}")
        return descriptor
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise


def read_open_file(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 64 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def write_new_file_at(directory_fd: int, name: str, data: bytes) -> os.stat_result:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NO_FOLLOW | _CLOSE_ON_EXEC,
            0o600,
            dir_fd=directory_fd,
        )
        written = 0
        while written < len(data):
            count = os.write(descriptor, data[written:])
            if count == 0:
                raise OSError(errno.EIO, "Artifact write made no progress")
            written += count
        return os.fstat(descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def read_regular_file_at(root_fd: int, relative: PurePosixPath | str) -> bytes:
    relative_path = PurePosixPath(relative)
    directory_fd = os.dup(root_fd)
    try:
        for component in relative_path.parts[:-1]:
            next_descriptor = open_directory_at(directory_fd, component)
            os.close(directory_fd)
            directory_fd = next_descriptor
        return read_file_at(directory_fd, relative_path.name)
    finally:
        os.close(directory_fd)


def read_regular_file(root: Path, relative: PurePosixPath | str) -> bytes:
    root_fd = open_directory(root)
    try:
        return read_regular_file_at(root_fd, relative)
    finally:
        os.close(root_fd)


def scan_regular_files(root_fd: int) -> tuple[StoredFile, ...]:
    files: list[StoredFile] = []

    def visit(directory_fd: int, prefix: PurePosixPath) -> None:
        for entry in list_directory(directory_fd):
            relative = prefix / entry.name
            if stat.S_ISLNK(entry.mode):
                raise OSError(errno.ELOOP, f"Pack entry is a symlink: {relative}")
            if stat.S_ISDIR(entry.mode):
                nested_fd = open_directory_at(directory_fd, entry.name)
                try:
                    visit(nested_fd, relative)
                finally:
                    os.close(nested_fd)
                continue
            if not stat.S_ISREG(entry.mode):
                raise OSError(errno.EINVAL, f"Pack entry is not regular: {relative}")
            files.append(StoredFile(relative.as_posix(), read_file_at(directory_fd, entry.name)))

    visit(root_fd, PurePosixPath())
    return tuple(sorted(files, key=lambda item: item.path))


def write_atomic(parent_fd: int, name: str, data: bytes) -> None:
    pending_name = f"{name}.pending"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            pending_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NO_FOLLOW | _CLOSE_ON_EXEC,
            0o600,
            dir_fd=parent_fd,
        )
        written = 0
        while written < len(data):
            count = os.write(descriptor, data[written:])
            if count == 0:
                raise OSError(errno.EIO, "Receipt write made no progress")
            written += count
        os.close(descriptor)
        descriptor = None
        rename_noreplace(parent_fd, pending_name, parent_fd, name)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def copy_directory_nofollow(source: Path, destination_parent_fd: int, name: str) -> None:
    source_fd = open_directory(source)
    destination_fd: int | None = None

    def copy_children(source_directory_fd: int, destination_directory_fd: int) -> None:
        for entry in list_directory(source_directory_fd):
            if stat.S_ISLNK(entry.mode):
                raise OSError(errno.ELOOP, f"Pack entry is a symlink: {entry.name}")
            if stat.S_ISDIR(entry.mode):
                source_child_fd = open_directory_at(source_directory_fd, entry.name)
                destination_child_fd: int | None = None
                try:
                    os.mkdir(entry.name, dir_fd=destination_directory_fd)
                    destination_child_fd = open_directory_at(destination_directory_fd, entry.name)
                    copy_children(source_child_fd, destination_child_fd)
                finally:
                    os.close(source_child_fd)
                    if destination_child_fd is not None:
                        os.close(destination_child_fd)
                continue
            if not stat.S_ISREG(entry.mode):
                raise OSError(errno.EINVAL, f"Pack entry is not regular: {entry.name}")
            data = read_file_at(source_directory_fd, entry.name)
            output_fd: int | None = None
            try:
                output_fd = os.open(
                    entry.name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NO_FOLLOW | _CLOSE_ON_EXEC,
                    0o600,
                    dir_fd=destination_directory_fd,
                )
                written = 0
                while written < len(data):
                    count = os.write(output_fd, data[written:])
                    if count == 0:
                        raise OSError(errno.EIO, "Pack copy made no progress")
                    written += count
            finally:
                if output_fd is not None:
                    os.close(output_fd)

    try:
        os.mkdir(name, dir_fd=destination_parent_fd)
        destination_fd = open_directory_at(destination_parent_fd, name)
        copy_children(source_fd, destination_fd)
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        os.close(source_fd)


def _rename_spec(platform: str, library: object) -> tuple[CFunction, int]:
    if platform == "darwin":
        try:
            function = cast(CFunction, getattr(library, "renameatx_np"))
        except AttributeError as error:
            raise OSError(errno.ENOSYS, "renameatx_np is unavailable") from error
        return function, _DARWIN_RENAME_EXCL
    if platform.startswith("linux"):
        try:
            function = cast(CFunction, getattr(library, "renameat2"))
        except AttributeError as error:
            raise OSError(errno.ENOSYS, "renameat2 is unavailable") from error
        return function, _LINUX_RENAME_NOREPLACE
    raise OSError(errno.ENOSYS, "Atomic no-replace rename is unavailable")


def _call_rename(
    function: CFunction,
    source_parent_fd: int,
    source_name: str,
    destination_parent_fd: int,
    destination_name: str,
    flag: int,
) -> None:
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    result = function(
        source_parent_fd,
        os.fsencode(source_name),
        destination_parent_fd,
        os.fsencode(destination_name),
        flag,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def rename_noreplace(
    source_parent_fd: int,
    source_name: str,
    destination_parent_fd: int,
    destination_name: str,
    *,
    commit_guard: Callable[[], None] | None = None,
) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    function, flag = _rename_spec(sys.platform, library)
    if commit_guard is not None:
        commit_guard()
    _call_rename(
        function,
        source_parent_fd,
        source_name,
        destination_parent_fd,
        destination_name,
        flag,
    )

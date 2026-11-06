"""File-writing helpers shared by the exporters."""
from __future__ import annotations

import errno
import os
import secrets
import stat
from typing import IO, Callable


def write_whole(path: str, write: Callable[[IO], object], mode: str = "wb", fsync: bool = False,
                **open_kwargs) -> None:
    """Call `write(file)` (its return value is ignored) on a new file beside `path` and move
    it onto `path` once it is complete, so a failed write leaves an existing `path` as it
    was and no partial file behind. `mode` ("wb" or "w") and `open_kwargs` (encoding,
    newline) are `open`'s; `fsync` puts the data on disk before it replaces the file. The
    new file takes an existing target's mode. An existing target the user cannot write
    raises PermissionError, as `open` does. A target that is not a regular file (a device
    such as os.devnull, a pipe), a folder that cannot hold a second file, and a name the
    temporary file's extra characters make too long (ENAMETOOLONG, or EINVAL on Windows) are
    written to `path` directly. Hard links to the target are not preserved: the other
    names keep the old content."""
    target = os.path.realpath(path)
    folder, name = os.path.split(target)
    try:
        st_mode = os.stat(target).st_mode
    except OSError:
        st_mode = None
    if st_mode is not None and not stat.S_ISREG(st_mode):
        with open(path, mode, **open_kwargs) as f:
            write(f)
        return
    existing_mode = st_mode & 0o7777 if st_mode is not None else None
    if existing_mode is not None and not os.access(target, os.W_OK):
        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    while True:
        tmp = os.path.join(folder, f".{name}.{secrets.token_hex(4)}.tmp")
        try:
            fd = os.open(tmp, flags, 0o666)
            break
        except FileExistsError:
            continue
        except OSError as e:
            if not isinstance(e, PermissionError) and e.errno not in (errno.ENAMETOOLONG,
                                                                     errno.EINVAL):
                raise
            with open(target, mode, **open_kwargs) as f:
                write(f)
            return
    try:
        with os.fdopen(fd, mode, **open_kwargs) as f:
            write(f)
            if fsync:
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass                        # a file system that does not sync
        if existing_mode is not None:
            os.chmod(tmp, existing_mode)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise

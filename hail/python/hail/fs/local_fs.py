import os
from typing import List
from shutil import copy2, rmtree

from .fs import FS
from .stat_result import StatResult


class LocalFS(FS):
    def __init__(self):
        pass

    def open(self, path: str, mode: str = 'r', buffer_size: int = -1):
        if 'w' in mode:
            try:
                return open(path, mode, buffering=buffer_size)
            except FileNotFoundError:
                os.makedirs(os.path.dirname(path))
                return open(path, mode, buffering=buffer_size)
        return open(path, mode, buffering=buffer_size)

    def copy(self, src: str, dest: str):
        dst_w_file = dest
        if os.path.isdir(dst_w_file):
            dst_w_file = os.path.join(dest, os.path.basename(src))

        copy2(src, dst_w_file)
        stats = os.stat(src)

        os.chown(dst_w_file, stats.st_uid, stats.st_gid)

    def exists(self, path: str) -> bool:
        return os.path.exists(path)

    def is_file(self, path: str) -> bool:
        return os.path.isfile(path)

    def is_dir(self, path: str) -> bool:
        return os.path.isdir(path)

    def stat(self, path: str) -> StatResult:
        return StatResult.from_os_stat_result(path, os.stat(path))

    def ls(self, path: str) -> List[StatResult]:
        return [self.stat(os.path.join(path, file))
                for file in os.listdir(path)]

    def mkdir(self, path: str):
        os.mkdir(path)

    def remove(self, path: str):
        os.remove(path)

    def rmtree(self, path: str):
        rmtree(path)

    def supports_scheme(self, scheme: str) -> bool:
        return scheme == ""

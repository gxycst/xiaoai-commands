#!/usr/bin/env python3
"""Restore one explicitly chosen manager backup, safely preserving current access."""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys

from server import APIError, CommandsStore, MAX_BYTES, strict_json, validate_rules


BACKUP_NAME = re.compile(r"commands\.[0-9]{8}T[0-9]{6}\.[0-9]{6}Z\.[0-9a-f]{16}\.json\Z")


def read_backup(store: CommandsStore, backup_name: str):
    if not isinstance(backup_name, str) or not BACKUP_NAME.fullmatch(backup_name):
        raise APIError(422, "请指定管理器生成的完整备份文件名；不能包含目录、斜杠或 ..")
    with store.locked() as directory_fd:
        root_kwargs = {"dir_fd": directory_fd} if directory_fd is not None else {}
        directory_name = ".commands-backups" if directory_fd is not None else os.path.join(store.directory, ".commands-backups")
        backup_fd = None
        try:
            try:
                directory_info = os.stat(directory_name, follow_symlinks=False, **root_kwargs)
            except FileNotFoundError as exc:
                raise APIError(404, "未找到 .commands-backups 目录；不会自动创建或选择备份") from exc
            if not stat.S_ISDIR(directory_info.st_mode) or stat.S_ISLNK(directory_info.st_mode):
                raise APIError(409, "备份目录必须是实际目录，不能是符号链接")
            if directory_fd is not None:
                backup_fd = os.open(directory_name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), **root_kwargs)
                opened_directory = os.fstat(backup_fd)
                if (opened_directory.st_dev, opened_directory.st_ino) != (directory_info.st_dev, directory_info.st_ino):
                    raise APIError(409, "备份目录已变化，请重试")
            backup_path = backup_name if backup_fd is not None else os.path.join(directory_name, backup_name)
            backup_kwargs = {"dir_fd": backup_fd} if backup_fd is not None else {}
            try:
                info = os.stat(backup_path, follow_symlinks=False, **backup_kwargs)
                if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    raise APIError(409, "备份必须是普通文件，不能是符号链接")
                fd = os.open(backup_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), **backup_kwargs)
            except FileNotFoundError as exc:
                raise APIError(404, "指定备份不存在，请检查完整文件名") from exc
            with os.fdopen(fd, "rb") as source:
                opened = os.fstat(source.fileno())
                if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise APIError(409, "备份文件已变化，请重试")
                raw = source.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise APIError(413, "备份超过 2 MiB，已停止恢复")
            return validate_rules(strict_json(raw))
        except APIError:
            raise
        except OSError as exc:
            raise APIError(409, "无法安全读取备份，请检查目录、文件及权限") from exc
        finally:
            if backup_fd is not None:
                os.close(backup_fd)


def restore(directory: str, backup_name: str):
    """Current file must remain valid; save rechecks its version and backs it up."""
    if not isinstance(backup_name, str) or not BACKUP_NAME.fullmatch(backup_name):
        raise APIError(422, "请指定管理器生成的完整备份文件名；不能包含目录、斜杠或 ..")
    store = CommandsStore(directory)
    current = store.read()
    rules = read_backup(store, backup_name)
    return store.save(rules, current["version"])


def main(argv=None):
    parser = argparse.ArgumentParser(description="安全恢复一个指定备份；请先停止管理容器")
    parser.add_argument("backup_name", help=".commands-backups 中的备份文件名，不含路径")
    parser.add_argument("--commands-dir", default=os.environ.get("COMMANDS_DIR", "/config/skills"))
    args = parser.parse_args(argv)
    try:
        result = restore(args.commands_dir, args.backup_name)
    except APIError as exc:
        print(f"恢复失败：{exc.message}。现有文件未被恢复操作覆盖", file=sys.stderr)
        return 1
    except OSError:
        print("恢复失败：无法读取现有目录或文件，请检查权限。现有文件未被恢复操作覆盖", file=sys.stderr)
        return 1
    print("恢复完成；已保留 commands.json 原有权限和所有者")
    print(f"恢复前的词库已另存备份：{result['backup']}")
    if result.get("warning"):
        print(result["warning"], file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

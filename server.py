#!/usr/bin/env python3
"""Local-network editor for an existing xiaogpt commands.json (stdlib only)."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import ipaddress
import json
import math
import os
from pathlib import Path
import secrets
import socket
import stat
import sys
import threading
import time
from datetime import datetime, timezone
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

try:
    import fcntl  # Linux: a directory lock also coordinates separate manager processes.
except ImportError:  # Local Windows preview uses the process-wide thread lock.
    fcntl = None


MAX_BYTES = 2 * 1024 * 1024
SESSION_TTL = 8 * 60 * 60
COOKIE_NAME = "xiaoai_session"
DEFAULT_NETWORKS = "127.0.0.0/8,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,::1/128,fc00::/7,fe80::/10"
STATIC_FILES = {"/": "index.html", "/index.html": "index.html", "/style.css": "style.css", "/app.js": "app.js", "/speaker.svg": "speaker.svg"}
STATIC_TYPES = {"index.html": "text/html", "style.css": "text/css", "app.js": "application/javascript", "speaker.svg": "image/svg+xml"}


class APIError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def strict_json(raw: bytes):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("JSON 对象含重复字段")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("JSON 不能包含 NaN 或 Infinity")

    def finite_float(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("JSON 数值必须有限")
        return parsed

    try:
        result = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_object,
                            parse_constant=invalid_constant, parse_float=finite_float)
        # Python accepts escaped lone surrogates, but they cannot be valid UTF-8.
        json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
        return result
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise APIError(422, "JSON 无效，请检查格式、重复字段和字符编码") from exc


def validate_rules(rules):
    if not isinstance(rules, list):
        raise APIError(422, "词库必须是 JSON 数组")
    for index, rule in enumerate(rules):
        label = f"第 {index + 1} 条规则"
        if not isinstance(rule, dict):
            raise APIError(422, f"{label}必须是对象")
        for field in ("name", "reply"):
            if not isinstance(rule.get(field), str) or not rule[field].strip():
                raise APIError(422, f"{label}的 {field} 必须是非空文字")
        keywords = rule.get("keywords")
        if not isinstance(keywords, list) or not keywords:
            raise APIError(422, f"{label}至少需要一个关键词")
        if any(not isinstance(word, str) or not word.strip() for word in keywords):
            raise APIError(422, f"{label}的关键词必须是非空文字")
        repeat = rule.get("repeat")
        if type(repeat) is not int or repeat < 1:
            raise APIError(422, f"{label}的 repeat 必须是正整数")
    # Keep every additional field, while ensuring it remains valid finite JSON.
    try:
        json.dumps(rules, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise APIError(422, "词库含不能保存的 JSON 内容") from exc
    return rules


def match_rules(query: str, rules: list):
    for index, rule in enumerate(rules):
        for keyword in rule["keywords"]:
            if keyword in query:
                return {"matched": True, "index": index, "keyword": keyword, "rule": rule,
                        "reply": rule["reply"], "repeat": rule["repeat"]}
    return {"matched": False}


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class CommandsStore:
    """Never seeds/recreates commands.json; all requests use fixed filenames."""

    def __init__(self, directory: str):
        self.directory = os.path.abspath(directory)
        self.thread_lock = threading.RLock()

    @contextlib.contextmanager
    def locked(self, exclusive=False):
        with self.thread_lock:
            directory_fd = None
            try:
                try:
                    info = os.lstat(self.directory)
                except FileNotFoundError as exc:
                    raise APIError(404, "skills 目录不存在，请检查已有目录的挂载") from exc
                if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    raise APIError(409, "skills 必须是实际目录，不能是符号链接")
                if os.name == "posix":
                    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
                    directory_fd = os.open(self.directory, flags)
                    opened = os.fstat(directory_fd)
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise APIError(409, "skills 目录已变化，请重试")
                    if fcntl is not None:
                        fcntl.flock(directory_fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                yield directory_fd
            finally:
                if directory_fd is not None:
                    if fcntl is not None:
                        fcntl.flock(directory_fd, fcntl.LOCK_UN)
                    os.close(directory_fd)

    def _path(self, name: str, directory_fd):
        return name if directory_fd is not None else os.path.join(self.directory, name)

    def _read(self, directory_fd):
        name = self._path("commands.json", directory_fd)
        kwargs = {"dir_fd": directory_fd} if directory_fd is not None else {}
        try:
            info = os.stat(name, follow_symlinks=False, **kwargs)
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise APIError(409, "commands.json 必须是普通文件，不能是符号链接")
            fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), **kwargs)
        except FileNotFoundError as exc:
            raise APIError(404, "未找到已有 commands.json；请检查挂载。管理页不会新建或覆盖词库") from exc
        except OSError as exc:
            raise APIError(409, "无法安全读取 commands.json，请检查文件和权限") from exc
        with os.fdopen(fd, "rb") as source:
            opened = os.fstat(source.fileno())
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise APIError(409, "commands.json 已变化，请重新载入")
            raw = source.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise APIError(413, "词库超过 2 MiB，管理页停止读取以保护现有文件")
        rules = validate_rules(strict_json(raw))
        return raw, rules, opened

    def read(self):
        with self.locked() as directory_fd:
            raw, rules, _ = self._read(directory_fd)
            return {"rules": rules, "version": digest(raw)}

    def read_portrait(self):
        with self.locked() as directory_fd:
            name = self._path("niuniu.png", directory_fd)
            kwargs = {"dir_fd": directory_fd} if directory_fd is not None else {}
            try:
                info = os.stat(name, follow_symlinks=False, **kwargs)
                if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    raise APIError(404, "人物图片必须是实际 PNG 文件")
                fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), **kwargs)
                with os.fdopen(fd, "rb") as source:
                    opened = os.fstat(source.fileno())
                    if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise APIError(409, "人物图片已变化")
                    content = source.read(8 * 1024 * 1024 + 1)
            except FileNotFoundError as exc:
                raise APIError(404, "请将私人抠图放到 skills/niuniu.png") from exc
            if len(content) > 8 * 1024 * 1024 or not content.startswith(b"\x89PNG\r\n\x1a\n"):
                raise APIError(422, "人物图片必须是小于 8 MiB 的 PNG")
            return content

    def _backup_directory(self, directory_fd):
        name = self._path(".commands-backups", directory_fd)
        kwargs = {"dir_fd": directory_fd} if directory_fd is not None else {}
        try:
            os.mkdir(name, 0o700, **kwargs)
        except FileExistsError:
            pass
        info = os.stat(name, follow_symlinks=False, **kwargs)
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise APIError(409, "备份目录不安全：不能是符号链接或其他文件")
        if directory_fd is None:
            return None
        fd = os.open(name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), **kwargs)
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            os.close(fd)
            raise APIError(409, "备份目录已变化，请重试")
        return fd

    def save(self, rules, version):
        validate_rules(rules)
        if not isinstance(version, str) or len(version) != 64 or any(char not in "0123456789abcdef" for char in version):
            raise APIError(422, "缺少有效文件版本，请重新载入词库")
        try:
            encoded = (json.dumps(rules, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
        except (UnicodeEncodeError, RecursionError) as exc:
            raise APIError(422, "词库包含无法编码的字符或过深的嵌套") from exc
        if len(encoded) > MAX_BYTES:
            raise APIError(413, "保存后的词库超过 2 MiB")
        with self.locked(exclusive=True) as directory_fd:
            original, _, info = self._read(directory_fd)
            if not hmac.compare_digest(version, digest(original)):
                raise APIError(409, "词库已被其他页面或程序修改，请重新载入后再编辑")
            backup_fd = None
            temp_name = f".commands.{secrets.token_hex(12)}.tmp"
            temp_path = self._path(temp_name, directory_fd)
            kwargs = {"dir_fd": directory_fd} if directory_fd is not None else {}
            temp_created = False
            try:
                backup_fd = self._backup_directory(directory_fd)
                fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, **kwargs)
                temp_created = True
                with os.fdopen(fd, "wb") as target:
                    if os.name == "posix":
                        current = os.fstat(target.fileno())
                        if (current.st_uid, current.st_gid) != (info.st_uid, info.st_gid):
                            os.fchown(target.fileno(), info.st_uid, info.st_gid)
                        os.fchmod(target.fileno(), stat.S_IMODE(info.st_mode))
                    target.write(encoded)
                    target.flush()
                    os.fsync(target.fileno())
                if os.name != "posix":
                    os.chmod(temp_path, stat.S_IMODE(info.st_mode))
                # Detect manual changes while preparing this transaction too.
                latest, _, _ = self._read(directory_fd)
                if digest(latest) != digest(original):
                    raise APIError(409, "保存期间词库发生变化，请重新载入")
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
                backup_name = f"commands.{stamp}.{secrets.token_hex(8)}.json"
                backup_path = backup_name if backup_fd is not None else os.path.join(self.directory, ".commands-backups", backup_name)
                backup_kwargs = {"dir_fd": backup_fd} if backup_fd is not None else {}
                fd = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, **backup_kwargs)
                try:
                    with os.fdopen(fd, "wb") as backup:
                        backup.write(original)
                        backup.flush()
                        os.fsync(backup.fileno())
                except OSError:
                    # A failed partial backup must never look like a restore point.
                    try:
                        os.unlink(backup_path, **backup_kwargs)
                    except OSError:
                        pass
                    raise
                if backup_fd is not None:
                    os.fsync(backup_fd)
                latest, _, _ = self._read(directory_fd)
                if digest(latest) != digest(original):
                    raise APIError(409, "保存期间词库发生变化，已保留原始备份，请重新载入")
                # Same directory + os.replace means readers see one complete file.
                replace_kwargs = {"src_dir_fd": directory_fd, "dst_dir_fd": directory_fd} if directory_fd is not None else {}
                os.replace(temp_path, self._path("commands.json", directory_fd), **replace_kwargs)
                temp_created = False
                warning = None
                if directory_fd is not None:
                    try:
                        os.fsync(directory_fd)
                    except OSError:
                        warning = "词库已保存且备份已保留，但目录同步失败，请检查存储状态"
                result = {"rules": rules, "version": digest(encoded), "backup": f".commands-backups/{backup_name}"}
                if warning:
                    result["warning"] = warning
                return result
            except APIError:
                raise
            except OSError as exc:
                raise APIError(500, "保存失败，请检查目录写入权限和剩余空间；原文件或备份仍保留") from exc
            finally:
                if temp_created:
                    try:
                        os.unlink(temp_path, **kwargs)
                    except OSError:
                        pass
                if backup_fd is not None:
                    os.close(backup_fd)


class Application:
    def __init__(self, password: str, directory: str, networks: str):
        self.password = password
        self.store = CommandsStore(directory)
        self.static_directory = Path(__file__).resolve().parent / "static"
        try:
            self.networks = [ipaddress.ip_network(item.strip()) for item in networks.split(",") if item.strip()]
        except ValueError as exc:
            raise ValueError("ALLOWED_NETWORKS 必须是以逗号分隔的 IP 网段") from exc
        if not self.networks:
            raise ValueError("ALLOWED_NETWORKS 不能为空")
        self.sessions = {}
        self.failures = {}
        self.auth_lock = threading.Lock()

    def allowed(self, address):
        try:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
            if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
                ip = ip.ipv4_mapped
            return any(ip.version == network.version and ip in network for network in self.networks)
        except ValueError:
            return False

    def session(self, cookie_header):
        try:
            cookie = SimpleCookie()
            cookie.load(cookie_header or "")
            token = cookie[COOKIE_NAME].value if COOKIE_NAME in cookie else ""
        except CookieError:
            return None, None
        with self.auth_lock:
            session = self.sessions.get(token)
            if session and session["expires"] > time.monotonic():
                return token, session
            if session:
                self.sessions.pop(token, None)
        return None, None

    def login(self, password, address):
        now = time.monotonic()
        with self.auth_lock:
            if len(self.failures) >= 1024:
                self.failures = {key: [stamp for stamp in values if stamp > now - 60]
                                 for key, values in self.failures.items() if values and values[-1] > now - 60}
                if address not in self.failures and len(self.failures) >= 1024:
                    raise APIError(429, "尝试过于频繁，请一分钟后再试")
            attempts = [value for value in self.failures.get(address, []) if value > now - 60]
            if len(attempts) >= 8:
                raise APIError(429, "尝试过于频繁，请一分钟后再试")
            if not isinstance(password, str) or not hmac.compare_digest(password.encode("utf-8"), self.password.encode("utf-8")):
                attempts.append(now)
                self.failures[address] = attempts
                raise APIError(401, "密码不正确")
            self.failures.pop(address, None)
            self.sessions = {key: value for key, value in self.sessions.items() if value["expires"] > now}
            if len(self.sessions) >= 512:
                oldest = min(self.sessions, key=lambda key: self.sessions[key]["expires"])
                self.sessions.pop(oldest, None)
            token = secrets.token_urlsafe(32)
            session = {"csrfToken": secrets.token_urlsafe(32), "expires": now + SESSION_TTL}
            self.sessions[token] = session
            return token, session


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "XiaoAiCommands/1.0"
    sys_version = ""

    @property
    def app(self):
        return self.server.app

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, format, *args):
        # Do not log body, cookies, query parameters, or arbitrary request paths.
        status = args[1] if len(args) > 1 and str(args[1]).isdigit() else "-"
        print(f"{self.log_date_time_string()} {self.client_address[0]} {getattr(self, 'command', '?')} {status}", file=sys.stderr)

    def send_error(self, code, message=None, explain=None):
        self._json(code, {"error": "请求不受支持"})

    def _send(self, status, content: bytes, content_type="application/json; charset=utf-8", cookie=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; font-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.send_header("Connection", "close")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            self.wfile.write(content)

    def _json(self, status, data, cookie=None):
        self._send(status, json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8"), cookie=cookie)

    def _guard(self):
        if not self.app.allowed(self.client_address[0]):
            raise APIError(403, "仅允许家庭局域网访问")

    def _origin(self):
        origins = self.headers.get_all("Origin", [])
        if len(origins) > 1:
            raise APIError(403, "来源校验失败")
        origin = origins[0] if origins else None
        host_values = self.headers.get_all("Host", [])
        if len(host_values) != 1:
            raise APIError(400, "缺少有效 Host")
        if origin is not None:
            try:
                parsed = urlsplit(origin)
            except ValueError as exc:
                raise APIError(403, "来源校验失败") from exc
            if parsed.scheme not in ("http", "https") or parsed.netloc != host_values[0] or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
                raise APIError(403, "不允许跨站请求")
        elif self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
            raise APIError(403, "来源校验失败")

    def _authenticated(self, csrf=False):
        token, session = self.app.session(self.headers.get("Cookie"))
        if session is None:
            raise APIError(401, "请先登录")
        if csrf:
            provided = self.headers.get("X-CSRF-Token", "")
            if not hmac.compare_digest(provided.encode("utf-8"), session["csrfToken"].encode("utf-8")):
                raise APIError(403, "登录校验已过期，请刷新页面")
        return token, session

    def _body(self):
        if self.headers.get("Transfer-Encoding"):
            raise APIError(400, "不支持分块请求")
        content_types = self.headers.get_all("Content-Type", [])
        content_type = content_types[0].split(";", 1)[0].strip().lower() if len(content_types) == 1 else ""
        if content_type != "application/json":
            raise APIError(415, "请求必须使用 application/json")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0] or any(char not in "0123456789" for char in lengths[0]):
            raise APIError(411, "缺少有效请求长度")
        if len(lengths[0]) > 10:
            raise APIError(413, "请求超过 2 MiB")
        length = int(lengths[0])
        if length > MAX_BYTES:
            raise APIError(413, "请求超过 2 MiB")
        try:
            raw = self.rfile.read(length)
        except (socket.timeout, TimeoutError) as exc:
            raise APIError(408, "请求超时") from exc
        if len(raw) != length:
            raise APIError(400, "请求内容不完整")
        body = strict_json(raw)
        if not isinstance(body, dict):
            raise APIError(422, "请求必须是 JSON 对象")
        return body

    def _dispatch(self):
        self._guard()
        path = urlsplit(self.path).path
        if self.command == "GET":
            if path == "/niuniu.png":
                # Private family image stays in the existing NAS bind mount.
                self._send(200, self.app.store.read_portrait(), "image/png")
                return
            if path in STATIC_FILES:
                target = self.app.static_directory / STATIC_FILES[path]
                try:
                    content = target.read_bytes()
                except OSError as exc:
                    raise APIError(404, "页面文件不存在，请检查应用文件是否完整") from exc
                content_type = STATIC_TYPES[target.name]
                self._send(200, content, content_type if content_type.startswith("image/") else content_type + "; charset=utf-8")
            elif path == "/api/session":
                _, session = self.app.session(self.headers.get("Cookie"))
                self._json(200, {"authenticated": session is not None, **({"csrfToken": session["csrfToken"]} if session else {})})
            elif path == "/api/rules":
                self._authenticated()
                self._json(200, self.app.store.read())
            else:
                raise APIError(404, "页面不存在")
            return
        if self.command not in ("POST", "PUT"):
            raise APIError(405, "不支持此请求方法")
        self._origin()
        if path == "/api/login" and self.command == "POST":
            body = self._body()
            token, session = self.app.login(body.get("password"), self.client_address[0])
            self._json(200, {"authenticated": True, "csrfToken": session["csrfToken"]},
                       cookie=f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL}")
            return
        token, session = self._authenticated(csrf=True)
        body = self._body()
        if path == "/api/logout" and self.command == "POST":
            with self.app.auth_lock:
                self.app.sessions.pop(token, None)
            self._json(200, {"authenticated": False}, cookie=f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")
        elif path == "/api/rules" and self.command == "PUT":
            self._json(200, self.app.store.save(body.get("rules"), body.get("version")))
        elif path == "/api/test" and self.command == "POST":
            query = body.get("query")
            if not isinstance(query, str):
                raise APIError(422, "query 必须是文字")
            rules = validate_rules(body["rules"]) if "rules" in body else self.app.store.read()["rules"]
            self._json(200, match_rules(query, rules))
        else:
            raise APIError(404, "接口不存在")

    def _handle(self):
        try:
            self._dispatch()
        except APIError as exc:
            self._json(exc.status, {"error": exc.message})
        except (BrokenPipeError, ConnectionResetError, socket.timeout, TimeoutError):
            self.close_connection = True
        except Exception as exc:
            # Print only the exception class, never request contents or configuration.
            print(f"Request failed: {type(exc).__name__}", file=sys.stderr)
            self._json(500, {"error": "服务器处理失败，请检查文件权限或稍后重试"})

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle
    do_HEAD = _handle
    do_OPTIONS = _handle


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=os.environ.get("PORT", "9528"))
    parser.add_argument("--commands-dir", default=os.environ.get("COMMANDS_DIR", "/config/skills"))
    args = parser.parse_args()
    password = os.environ.get("APP_PASSWORD", "")
    placeholders = {"change-this-password", "change_this_password", "changeme123456", "replace-me-with-a-long-password", "your-password-here", "password123456", "123456789012"}
    if len(password) < 12 or not password.strip() or password.lower() in placeholders or password.lower().startswith(("change_me", "change-this", "change_this", "replace_me", "replace-this", "your_password")):
        parser.error("请先设置 APP_PASSWORD 为至少 12 个字符的独立密码，不能使用示例占位密码")
    if not 1 <= args.port <= 65535:
        parser.error("PORT 必须在 1 到 65535 之间")
    try:
        app = Application(password, args.commands_dir, os.environ.get("ALLOWED_NETWORKS", DEFAULT_NETWORKS))
        if ":" in args.host:
            Server.address_family = socket.AF_INET6
        with Server((args.host, args.port), Handler) as server:
            server.app = app
            print(f"小爱亲子词库管理已启动，端口 {args.port}；不会初始化或改写现有词库", flush=True)
            try:
                server.serve_forever(poll_interval=0.5)
            except KeyboardInterrupt:
                pass
    except (ValueError, OSError) as exc:
        print(f"启动失败：{type(exc).__name__}。请检查网段、端口和目录配置", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

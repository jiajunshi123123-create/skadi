"""Web API 认证、会话归属与简易限流。

WEB_API_KEY 默认必需：未配置、短于 16 位或仍是示例占位符时，业务接口拒绝访问。
WEB_ADMIN_API_KEY 仅用于管理接口（/api/scan），且不能与 WEB_API_KEY 相同。
会话键绑定到密钥指纹，客户端只能复用自己的 session_id。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Mapping, Optional

_MIN_KEY_LENGTH = 16
_PLACEHOLDER_KEYS = {
    "changeme",
    "password",
    "secret",
    "your_api_key",
    "your-api-key",
    "replace-with-a-long-random-key",
    "replace-with-a-different-long-random-key",
}
_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,39}$")
_SCOPE_ENV = {
    "user": ("WEB_RATE_LIMIT_PER_MINUTE", 30),
    "admin": ("WEB_ADMIN_RATE_LIMIT_PER_MINUTE", 5),
    # 钉钉等机器人通道按「渠道:外部用户 ID」计数，与 Web 调用方分开。
    "bot": ("BOT_RATE_LIMIT_PER_MINUTE", 30),
}


@dataclass
class AuthDecision:
    caller_id: Optional[str] = None
    status_code: Optional[int] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status_code is None


class RateLimiter:
    """固定窗口计数。进程内有效，多副本部署时各自独立。"""

    def __init__(self, limit: int, window_seconds: float = 60.0):
        if limit < 1:
            raise ValueError("limit must be positive")
        self.limit = limit
        self.window_seconds = window_seconds
        self._hits: dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def too_many(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            self._prune(key, now)
            return len(self._hits.get(key, ())) >= self.limit

    def hit(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._prune(key, now)
            self._hits[key].append(now)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            queue = self._hits[key]
            self._prune_queue(queue, now)
            if len(queue) >= self.limit:
                return False
            queue.append(now)
            return True

    def _prune(self, key: str, now: float) -> None:
        queue = self._hits.get(key)
        if not queue:
            return
        self._prune_queue(queue, now)
        if not queue:
            self._hits.pop(key, None)
        self._drop_stale_keys(now)

    def _prune_queue(self, queue: Deque[float], now: float) -> None:
        cutoff = now - self.window_seconds
        while queue and queue[0] < cutoff:
            queue.popleft()

    def _drop_stale_keys(self, now: float) -> None:
        if len(self._hits) <= 5000:
            return
        cutoff = now - self.window_seconds
        stale = [key for key, queue in self._hits.items() if not queue or queue[-1] < cutoff]
        for key in stale:
            self._hits.pop(key, None)


_limiters: dict[str, RateLimiter] = {}
_failure_limiter: Optional[RateLimiter] = None


def reset_for_tests() -> None:
    """清空限流计数。仅测试使用。"""
    global _failure_limiter
    _limiters.clear()
    _failure_limiter = None


def get_api_key() -> str:
    return os.getenv("WEB_API_KEY", "").strip()


def get_admin_key() -> str:
    return os.getenv("WEB_ADMIN_API_KEY", "").strip()


def configured_user_keys() -> list:
    """WEB_API_KEY 可以是单个密钥，或用逗号分隔的多个调用方密钥。"""
    keys = []
    seen = set()
    for part in os.getenv("WEB_API_KEY", "").split(","):
        key = part.strip()
        if not _key_is_strong(key):
            continue
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        keys.append(key)
    return keys


def api_key_configured() -> bool:
    return bool(configured_user_keys())


def admin_key_configured() -> bool:
    admin = get_admin_key()
    if not _key_is_strong(admin):
        return False
    for user in configured_user_keys():
        if _keys_match(admin, user):
            return False
    # 整段未拆分时也拒绝与用户密钥完全相同的配置。
    raw_user = get_api_key()
    if raw_user and _keys_match(admin, raw_user):
        return False
    return True


def caller_id_for_key(key: str, *, admin: bool = False) -> str:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    prefix = "adm" if admin else "usr"
    return f"{prefix}-{digest}"


def scoped_session_id(caller_id: str, client_session_id: Optional[str]) -> str:
    """把客户端 session_id 限定在已认证调用方名下。"""
    raw = (client_session_id or "").strip() or "default"
    if not _SESSION_RE.fullmatch(raw):
        raise ValueError("invalid session_id")
    if not caller_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", caller_id):
        raise ValueError("invalid caller")
    return f"{caller_id}:{raw}"


def authorize(headers: Mapping, client_host: str, *, admin: bool = False) -> AuthDecision:
    """校验 API 密钥。失败时不回显密钥。"""
    ip = (client_host or "unknown").strip() or "unknown"
    failures = _get_failure_limiter()
    if failures.too_many(ip):
        return AuthDecision(status_code=429, error="尝试过多，请稍后再试")

    if admin:
        if not admin_key_configured():
            failures.hit(ip)
            return AuthDecision(status_code=503, error="管理接口未启用，请配置独立的 WEB_ADMIN_API_KEY")
        expected = get_admin_key()
        presented = _presented_admin_key(headers)
        caller_admin = True
    else:
        if not api_key_configured():
            failures.hit(ip)
            return AuthDecision(status_code=503, error="服务未配置有效的 WEB_API_KEY，已拒绝访问")
        presented = _presented_user_key(headers)
        expected = _match_user_key(presented)
        caller_admin = False

    if admin:
        if not presented or not _keys_match(presented, expected):
            failures.hit(ip)
            return AuthDecision(status_code=401, error="未授权")
    elif not expected:
        failures.hit(ip)
        return AuthDecision(status_code=401, error="未授权")

    return AuthDecision(caller_id=caller_id_for_key(expected, admin=caller_admin))


def check_rate_limit(caller_id: str, scope: str) -> Optional[AuthDecision]:
    limiter = _limiter_for(scope)
    if limiter.allow(caller_id):
        return None
    return AuthDecision(status_code=429, error="请求过于频繁，请稍后再试")


def _limiter_for(scope: str) -> RateLimiter:
    env_name, default = _SCOPE_ENV.get(scope, ("WEB_RATE_LIMIT_PER_MINUTE", 30))
    limit = _positive_int_env(env_name, default)
    current = _limiters.get(scope)
    if current is None or current.limit != limit:
        current = RateLimiter(limit)
        _limiters[scope] = current
    return current


def _get_failure_limiter() -> RateLimiter:
    global _failure_limiter
    limit = _positive_int_env("WEB_AUTH_FAILURE_LIMIT_PER_MINUTE", 10)
    if _failure_limiter is None or _failure_limiter.limit != limit:
        _failure_limiter = RateLimiter(limit)
    return _failure_limiter


def _positive_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _key_is_strong(key: str) -> bool:
    if not key or len(key) < _MIN_KEY_LENGTH:
        return False
    return key.lower() not in _PLACEHOLDER_KEYS


def _match_user_key(presented: str) -> Optional[str]:
    """比对全部已配置密钥，避免提前返回带来的时序差异。"""
    if not presented:
        return None
    matched = None
    for key in configured_user_keys():
        if _keys_match(presented, key):
            matched = key
    return matched


def _keys_match(presented: str, expected: str) -> bool:
    if not presented or not expected:
        return False
    return hmac.compare_digest(
        hashlib.sha256(presented.encode("utf-8")).digest(),
        hashlib.sha256(expected.encode("utf-8")).digest(),
    )


def _header(headers: Mapping, name: str) -> str:
    target = name.lower()
    getter = getattr(headers, "get", None)
    if getter is not None:
        # Starlette Headers.get 忽略大小写；普通 dict 再按小写兜底。
        direct = getter(name)
        if direct:
            return str(direct)
        direct = getter(target)
        if direct:
            return str(direct)
    for key, value in headers.items():
        if str(key).lower() == target and value:
            return str(value)
    return ""


def _presented_user_key(headers: Mapping) -> str:
    authorization = _header(headers, "authorization").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return _header(headers, "x-api-key").strip()


def _presented_admin_key(headers: Mapping) -> str:
    explicit = _header(headers, "x-admin-key").strip()
    if explicit:
        return explicit
    return _presented_user_key(headers)

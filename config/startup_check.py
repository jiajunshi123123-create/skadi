"""各入口启动前检查必需配置。缺一项就退出，并一次列出全部缺失项。"""

from __future__ import annotations

from config.env_settings import read_setting

_PLACEHOLDERS = {
    "your_deepseek_api_key",
    "your_password",
    "your_database",
    "your_app_key",
    "your_app_secret",
    "your-app-key",
    "your-app-secret",
    "replace-with-a-long-random-key",
    "replace-with-a-different-long-random-key",
    "changeme",
    "password",
    "secret",
}

# 正式名 -> 仍接受的旧名。校验看的是回退之后的值。
_ALIASES = {
    "DB_HOST": ("STARROCKS_HOST",),
    "DB_USER": ("STARROCKS_USER",),
    "DB_PASSWORD": ("STARROCKS_PASSWORD",),
    "DB_NAME": ("STARROCKS_DB",),
    "PG_HOST": ("PGHOST",),
    "PG_DATABASE": ("PG_DB", "PGDATABASE"),
    "PG_USER": ("PGUSER",),
    "PG_PASSWORD": ("PGPASSWORD",),
}

_COMMON = (
    "DEEPSEEK_API_KEY",
    "DB_HOST",
    "DB_USER",
    "DB_PASSWORD",
    "DB_NAME",
    "PG_HOST",
    "PG_DATABASE",
    "PG_USER",
    "PG_PASSWORD",
)

_ENTRYPOINTS = {
    "web": _COMMON + ("WEB_API_KEY", "WEB_API_KEY_ROLES"),
    "dingtalk": _COMMON + ("DINGTALK_BOT_APP_KEY", "DINGTALK_BOT_APP_SECRET"),
    "feishu": _COMMON + ("FEISHU_APP_ID", "FEISHU_APP_SECRET"),
}


class StartupConfigError(Exception):
    def __init__(self, entrypoint: str, missing: list[str]):
        self.entrypoint = entrypoint
        self.missing = list(missing)
        super().__init__(self.format_message())

    def format_message(self) -> str:
        lines = [f"{self.entrypoint} 启动失败，以下配置缺失或仍是占位符:"]
        lines.extend(f"- {name}" for name in self.missing)
        lines.append("请在 .env 里填写正式值。旧变量名仍可读，启动时会提示改用新名字。")
        return "\n".join(lines)


def resolved_value(name: str) -> str:
    aliases = _ALIASES.get(name, ())
    return read_setting(name, *aliases, default="")


def _is_missing(name: str) -> bool:
    if name == "WEB_API_KEY":
        from utils.web_auth import api_key_configured

        return not api_key_configured()
    value = resolved_value(name)
    if not value:
        return True
    if name == "WEB_API_KEY_ROLES":
        return "=" not in value
    return value.strip().lower() in _PLACEHOLDERS


def validate_entrypoint(name: str) -> None:
    """通过则返回。缺配置时抛出 StartupConfigError，不打印变量的值。"""
    required = _ENTRYPOINTS.get(name)
    if required is None:
        raise StartupConfigError(name, [f"未知入口 {name}"])
    missing = [item for item in required if _is_missing(item)]
    if missing:
        raise StartupConfigError(name, missing)


def require_entrypoint(name: str) -> None:
    """给进程入口用：失败时以非 0 退出，消息里列出全部缺失项。"""
    try:
        validate_entrypoint(name)
    except StartupConfigError as exc:
        raise SystemExit(exc.format_message()) from exc

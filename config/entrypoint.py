"""容器和本机共用的启动入口。先校验配置，再拉起对应进程。"""

from __future__ import annotations

import os
import sys


def main(argv=None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in {"web", "dingtalk", "feishu"}:
        raise SystemExit("用法: python -m config.entrypoint web|dingtalk|feishu")
    name = args[0]
    from config.startup_check import require_entrypoint

    require_entrypoint(name)
    if name == "web":
        import uvicorn

        host = os.getenv("WEB_BIND_HOST", "127.0.0.1").strip() or "127.0.0.1"
        port = int(os.getenv("WEB_PORT", "8080"))
        uvicorn.run("web_server:app", host=host, port=port, reload=False, log_level="info")
        return
    if name == "dingtalk":
        from dingtalk_bot import main as run_dingtalk

        run_dingtalk()
        return
    from channels.feishu import main as run_feishu

    run_feishu()


if __name__ == "__main__":
    main()

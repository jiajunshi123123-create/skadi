"""给没有 HTTP 端口的机器人写一个心跳文件，供容器健康检查读取。"""

from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger("skadi.heartbeat")

DEFAULT_PATH = "/tmp/skadi-heartbeat"


def write_heartbeat(path: str) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write("ok\n")
    os.replace(temporary, path)


def start_heartbeat(path: str = None, interval: float = 20.0) -> str:
    """立刻写一次，然后在后台按间隔刷新。文件内容只有 ok，不含配置。"""
    target = path or os.getenv("HEARTBEAT_PATH", DEFAULT_PATH)
    write_heartbeat(target)

    def loop() -> None:
        while True:
            time.sleep(interval)
            try:
                write_heartbeat(target)
            except Exception:
                logger.exception("心跳文件写入失败 path=%s", target)

    threading.Thread(target=loop, name="skadi-heartbeat", daemon=True).start()
    return target

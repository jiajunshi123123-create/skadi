"""独立进程跑定时报告。机器人进程也可以调用 start_inprocess。"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime

from reports.schedules import PgScheduleStore, SHANGHAI, run_due

logger = logging.getLogger("skadi.scheduler")


def resolve_role(user_id: str):
    from permission_manager import permission_manager

    return permission_manager.get_user_role(user_id)


def run_report(spec: str, role: dict):
    import asyncio

    from agent_orchestrator import run_agent

    return asyncio.run(run_agent(spec, user_role=role, last_context=None))


def deliver(channel: str, target: str, text: str, metadata: dict) -> None:
    """只按任务上记下的渠道和私聊对象发送。"""
    logger.info("投递定时报告 channel=%s target=%s chars=%s", channel, target, len(text or ""))
    if channel == "feishu":
        _deliver_feishu(target, text, metadata)
        return
    if channel == "dingtalk":
        logger.warning("钉钉定时报告需要机器人在线回复，本次只记录 target=%s", target)
        return
    logger.info("web 定时报告已生成，用户可在下次打开时下载 target=%s", target)


def _deliver_feishu(open_id: str, text: str, metadata: dict) -> None:
    from channels.feishu import FeishuApi
    from reports.deliver import deliver_feishu
    from reports.files import materialize_report

    app_id = os.environ.get("FEISHU_APP_ID", "").strip()
    app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        logger.error("缺少飞书应用配置，定时报告没有发出")
        return
    api = FeishuApi(app_id, app_secret)
    api.send_text(open_id, text)
    artifacts = materialize_report(open_id, text, metadata)
    paths = {}
    root = os.environ.get("REPORT_STORE_DIR", os.path.join("data", "reports"))
    folder = os.path.join(root, open_id, artifacts["report_id"])
    for name in artifacts.get("files") or []:
        paths[name] = os.path.join(folder, name)
    deliver_feishu(api, open_id, {"paths": paths})


def tick(now: datetime = None) -> list:
    moment = now or datetime.now(SHANGHAI)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=SHANGHAI)
    return run_due(PgScheduleStore(), resolve_role, run_report, deliver, moment)


def start_inprocess() -> None:
    """机器人进程里每分钟看一次。REPORT_SCHEDULER=off 时不启动。"""
    if os.environ.get("REPORT_SCHEDULER", "inprocess").strip().lower() == "off":
        logger.info("进程内定时报告已关闭")
        return
    import threading

    def loop():
        while True:
            try:
                tick()
            except Exception:
                logger.exception("定时报告轮询失败")
            time.sleep(60)

    threading.Thread(target=loop, name="report-scheduler", daemon=True).start()
    logger.info("进程内定时报告已启动")


def main() -> None:
    from config.startup_check import require_entrypoint

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    require_entrypoint("scheduler")
    logger.info("定时报告进程启动")
    while True:
        try:
            tick()
        except Exception:
            logger.exception("定时报告轮询失败")
        time.sleep(60)


if __name__ == "__main__":
    main()

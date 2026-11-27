"""聊天里的定时报告指令。群聊到不了这里。"""

from __future__ import annotations

import logging
from typing import Optional

from reports.schedule_parse import parse_schedule_command

logger = logging.getLogger("skadi.schedules")


def handle_schedule_command(text: str, user, message, store=None) -> Optional[str]:
    parsed = parse_schedule_command(text)
    if parsed is None:
        return None
    if store is None:
        try:
            from reports.schedules import PgScheduleStore

            store = PgScheduleStore()
        except Exception:
            logger.exception("定时报告存储不可用")
            return "定时报告需要经验库里的 report_schedules 表。请先执行 scripts/migrate.sql。"
    action = parsed["action"]
    try:
        if action == "list":
            rows = store.list_for(user.user_id)
            if not rows:
                return "你还没有定时报告。"
            lines = ["你的定时报告："]
            for row in rows:
                lines.append(f"{row.id}. {row.cron}（上海时间）{row.report_spec} → {row.channel} 私聊")
            return "\n".join(lines)
        if action == "cancel":
            ok = store.cancel(user.user_id, parsed["schedule_id"])
            if not ok:
                return f"没有找到编号 {parsed['schedule_id']} 的定时报告。"
            return f"已取消定时报告 {parsed['schedule_id']}。"
        schedule = store.create(
            owner_user_id=user.user_id,
            channel=message.channel,
            target=message.user_ext_id,
            cron=parsed["cron"],
            report_spec=parsed["report_spec"],
        )
    except Exception:
        logger.exception("定时报告指令失败 user_id=%s", getattr(user, "user_id", ""))
        return "定时报告没有写成。请确认经验库已执行 scripts/migrate.sql。"
    return (
        f"已创建定时报告 {schedule.id}。\n"
        f"时间：{schedule.cron}（Asia/Shanghai）\n"
        f"内容：{schedule.report_spec}\n"
        f"只发到 {schedule.channel} 私聊 {schedule.target}。角色失效后会自动停用。"
    )

"""从中文指令解析定时报告。只支持每天、每周某天、每月某号。"""

from __future__ import annotations

import re
from typing import Optional

_LIST = "我的定时报告"
_CANCEL = re.compile(r"^取消定时报告\s*(\d+)\s*$")
_CREATE = re.compile(
    r"^每(?P<unit>天|周|月)"
    r"(?:(?P<weekday>一|二|三|四|五|六|日|天))?"
    r"(?:(?P<monthday>\d{1,2})号)?"
    r"(?P<daypart>早上|上午|中午|下午|晚上)?"
    r"(?P<hour>\d{1,2})(?:[:：](?P<minute>\d{2})|点(?P<minute2>\d{1,2})?分?)?"
    r"发我(?P<body>.+)$"
)
_WEEKDAY = {"一": "1", "二": "2", "三": "3", "四": "4", "五": "5", "六": "6", "日": "0", "天": "0"}


def parse_schedule_command(text: str) -> Optional[dict]:
    raw = (text or "").strip()
    if raw == _LIST:
        return {"action": "list"}
    cancel = _CANCEL.match(raw)
    if cancel:
        return {"action": "cancel", "schedule_id": int(cancel.group(1))}
    created = _CREATE.match(raw)
    if not created:
        return None
    body = created.group("body").strip()
    if not body:
        return None
    hour = int(created.group("hour"))
    minute_text = created.group("minute") or created.group("minute2") or "0"
    minute = int(minute_text)
    daypart = created.group("daypart") or ""
    if daypart in {"下午", "晚上"} and hour < 12:
        hour += 12
    if daypart == "中午" and hour < 11:
        hour = 12
    if hour > 23 or minute > 59:
        return None
    unit = created.group("unit")
    if unit == "天":
        cron = f"{minute} {hour} * * *"
    elif unit == "周":
        weekday = created.group("weekday")
        if not weekday:
            return None
        cron = f"{minute} {hour} * * {_WEEKDAY[weekday]}"
    else:
        monthday = created.group("monthday")
        if not monthday or not (1 <= int(monthday) <= 31):
            return None
        cron = f"{minute} {hour} {int(monthday)} * *"
    return {"action": "create", "cron": cron, "report_spec": body, "timezone": "Asia/Shanghai"}

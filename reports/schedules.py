"""定时报告的存储和到期执行。执行时重新读取主人的当前角色。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass
class Schedule:
    id: int
    owner_user_id: str
    channel: str
    target: str
    cron: str
    report_spec: str
    enabled: bool = True
    last_run_at: Optional[datetime] = None
    last_error: str = ""


@dataclass
class MemoryScheduleStore:
    """测试和没有数据库时的替身。"""

    rows: dict = field(default_factory=dict)
    _next: int = 1

    def create(self, owner_user_id: str, channel: str, target: str, cron: str, report_spec: str) -> Schedule:
        if channel not in {"feishu", "dingtalk", "web"}:
            raise ValueError("只支持私聊渠道")
        if not target or target == "unknown":
            raise ValueError("没有私聊对象")
        item = Schedule(
            id=self._next,
            owner_user_id=owner_user_id,
            channel=channel,
            target=target,
            cron=cron,
            report_spec=report_spec,
        )
        self.rows[item.id] = item
        self._next += 1
        return item

    def list_for(self, owner_user_id: str) -> list:
        return [row for row in self.rows.values() if row.owner_user_id == owner_user_id and row.enabled]

    def cancel(self, owner_user_id: str, schedule_id: int) -> bool:
        row = self.rows.get(int(schedule_id))
        if row is None or row.owner_user_id != owner_user_id:
            return False
        row.enabled = False
        return True

    def enabled(self) -> list:
        return [row for row in self.rows.values() if row.enabled]

    def disable(self, schedule_id: int, reason: str) -> None:
        row = self.rows.get(int(schedule_id))
        if row:
            row.enabled = False
            row.last_error = reason

    def mark_ran(self, schedule_id: int, when: datetime, error: str = "") -> None:
        row = self.rows.get(int(schedule_id))
        if row:
            row.last_run_at = when
            row.last_error = error


def cron_trigger(cron: str) -> CronTrigger:
    """五段 cron。星期按常见约定：0 是周日，1 是周一。"""
    minute, hour, day, month, dow = cron.split()
    if dow != "*":
        converted = []
        for token in dow.split(","):
            if token.isdigit():
                number = int(token)
                if number == 7:
                    number = 0
                converted.append(str((number - 1) % 7))
            else:
                converted.append(token)
        dow = ",".join(converted)
    return CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=dow,
        timezone=SHANGHAI,
    )


def is_due(schedule: Schedule, now: datetime) -> bool:
    """now 必须是上海时区。只在计划时间点之后的两分钟内触发一次。"""
    if not schedule.enabled:
        return False
    if now.tzinfo is None:
        now = now.replace(tzinfo=SHANGHAI)
    probe = now - timedelta(minutes=2)
    upcoming = cron_trigger(schedule.cron).get_next_fire_time(None, probe)
    if upcoming is None or not (probe < upcoming <= now):
        return False
    if schedule.last_run_at is not None and schedule.last_run_at >= upcoming:
        return False
    return True


def run_due(
    store,
    resolve_role: Callable[[str], Optional[dict]],
    run_report: Callable,
    deliver: Callable,
    now: datetime,
) -> list:
    """角色失效就停用，不再发到别人的会话。"""
    done = []
    for schedule in list(store.enabled()):
        if not is_due(schedule, now):
            continue
        role = resolve_role(schedule.owner_user_id)
        if not role:
            store.disable(schedule.id, "角色已失效")
            done.append({"id": schedule.id, "status": "disabled"})
            continue
        try:
            text, metadata = run_report(schedule.report_spec, role)
        except Exception as exc:
            store.mark_ran(schedule.id, now, error=str(exc))
            done.append({"id": schedule.id, "status": "error"})
            continue
        deliver(schedule.channel, schedule.target, text, metadata or {})
        store.mark_ran(schedule.id, now, error="")
        done.append({"id": schedule.id, "status": "sent", "target": schedule.target})
    return done


class PgScheduleStore:
    """report_schedules 表。连接失败时由调用方提示，不在导入时连接。"""

    def __init__(self, connect: Callable = None):
        self._connect = connect

    def _conn(self):
        if self._connect:
            return self._connect()
        from tools.pg_tool import get_connection

        return get_connection()

    def create(self, owner_user_id: str, channel: str, target: str, cron: str, report_spec: str) -> Schedule:
        if channel not in {"feishu", "dingtalk", "web"} or not target:
            raise ValueError("只支持私聊渠道")
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO report_schedules
                        (owner_user_id, channel, target, cron, report_spec, enabled)
                    VALUES (%s, %s, %s, %s, %s, TRUE)
                    RETURNING id
                    """,
                    (owner_user_id, channel, target, cron, report_spec),
                )
                schedule_id = cur.fetchone()[0]
            conn.commit()
        finally:
            conn.close()
        return Schedule(schedule_id, owner_user_id, channel, target, cron, report_spec)

    def list_for(self, owner_user_id: str) -> list:
        return self._select("owner_user_id = %s AND enabled = TRUE", (owner_user_id,))

    def cancel(self, owner_user_id: str, schedule_id: int) -> bool:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE report_schedules
                    SET enabled = FALSE, updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s AND owner_user_id = %s AND enabled = TRUE
                    """,
                    (int(schedule_id), owner_user_id),
                )
                changed = cur.rowcount
            conn.commit()
        finally:
            conn.close()
        return bool(changed)

    def enabled(self) -> list:
        return self._select("enabled = TRUE", ())

    def disable(self, schedule_id: int, reason: str) -> None:
        self._update(
            "UPDATE report_schedules SET enabled = FALSE, last_error = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            (reason[:500], int(schedule_id)),
        )

    def mark_ran(self, schedule_id: int, when: datetime, error: str = "") -> None:
        self._update(
            """
            UPDATE report_schedules
            SET last_run_at = %s, last_error = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (when, (error or "")[:500], int(schedule_id)),
        )

    def _select(self, where: str, params: tuple) -> list:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, owner_user_id, channel, target, cron, report_spec,
                           enabled, last_run_at, last_error
                    FROM report_schedules
                    WHERE {where}
                    ORDER BY id
                    """,
                    params,
                )
                rows = cur.fetchall()
        finally:
            conn.close()
        return [
            Schedule(
                id=row[0],
                owner_user_id=row[1],
                channel=row[2],
                target=row[3],
                cron=row[4],
                report_spec=row[5],
                enabled=row[6],
                last_run_at=row[7],
                last_error=row[8] or "",
            )
            for row in rows
        ]

    def _update(self, sql: str, params: tuple) -> None:
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

"""渠道无关的对话处理：身份、限流、重置、反馈、追问和确认。

各渠道只负责把消息变成 IncomingMessage、把文本发回去。
这里不再保留「确认后直接跑 SQL」的旧流程；分析失败时回复明确错误。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Awaitable, Callable, Optional

from channels.base import IncomingMessage
from core.identity import IdentityStore, PgIdentityStore, ResolvedUser

logger = logging.getLogger("skadi.conversation")

RESET_COMMANDS = {"重置", "清除记忆", "重新开始", "清空会话", "忘记", "clear"}
POSITIVE_FEEDBACK = {"👍", "有用", "好的"}
NEGATIVE_FEEDBACK = {"👎", "没用", "不准确"}
CONFIRMATION_KEYWORDS = frozenset({
    "1", "OK", "ok", "y", "Y", "yes", "Yes", "YES",
    "确认", "执行", "开始", "是", "是的", "好的", "好", "可以",
})
FOLLOW_UP_STRONG = (
    "继续下钻", "按", "分别看", "换个维度", "改成按",
    "和", "对比", "同比", "环比", "对照", "比一下",
    "为什么", "为何", "原因", "怎么回事",
    "那", "呢", "怎么样", "另外",
)
FOLLOW_UP_STARTERS = (
    "继续", "那", "按", "换", "为什么", "原因",
    "和", "对比", "同比", "环比",
)

CONTEXT_TTL_SECONDS = 3600
SUGGESTION_TTL_SECONDS = 600
M3_SQL_RE = re.compile(r"<!--M3_SQL:(.+?)-->", re.DOTALL)

ANALYZING_TEXT = "已收到，正在分析，请稍候。"
RESET_TEXT = "已重置会话记录，您可以重新开始提问。"
FEEDBACK_TEXT = "感谢您的反馈，我们会持续优化。"
SUGGESTION_EXPIRED_TEXT = "之前的建议已过期，请重新描述您的问题，我来帮您查询。"
AGENT_UNAVAILABLE_TEXT = "分析服务暂时不可用，请稍后重试。本次没有改用其他查询方式。"
UNEXPECTED_ERROR_TEXT = "分析失败，请稍后重试。本次没有改用其他查询方式。"
RATE_LIMIT_TEXT = "请求过于频繁，请稍后再试。"


def is_follow_up(prompt: str) -> bool:
    """短句里的追问词，或固定开头，视为对上一轮的延续。"""
    text = (prompt or "").strip()
    if len(text) <= 30:
        for keyword in FOLLOW_UP_STRONG:
            if keyword in text:
                return True
    for starter in FOLLOW_UP_STARTERS:
        if text.startswith(starter):
            return True
    return False


def is_confirmation(prompt: str) -> bool:
    """整句正好是确认词才算。子串不算，例如「1月日活」里的「1」。"""
    text = (prompt or "").strip()
    return bool(text) and text in CONFIRMATION_KEYWORDS


def permission_denied_text(message: IncomingMessage) -> str:
    return (
        "您暂无数据查询权限，请联系管理员开通。\n\n"
        f"渠道: {message.channel}\n"
        f"您的ID: {message.user_ext_id}\n"
        "请把此 ID 发给管理员，写入 user_identities，并关联有效角色。"
    )


def strip_sql_marker(reply: str) -> tuple[str, str]:
    """去掉回复里的 SQL 标记，返回 (给用户看的文本, SQL)。"""
    if not reply or "<!--M3_SQL:" not in reply:
        return reply or "", ""
    match = M3_SQL_RE.search(reply)
    sql = match.group(1) if match else ""
    visible = M3_SQL_RE.sub("", reply).rstrip()
    return visible, sql


class ConversationService:
    """处理一条已解析的私聊消息。"""

    def __init__(
        self,
        identity_store: Optional[IdentityStore] = None,
        agent_runner: Optional[Callable[..., Awaitable]] = None,
        rate_limit: Optional[Callable[[str, str], object]] = None,
        save_session: Optional[Callable] = None,
        get_session: Optional[Callable] = None,
        delete_session: Optional[Callable] = None,
        save_task: Optional[Callable] = None,
        on_feedback: Optional[Callable] = None,
        deliver_assets: Optional[Callable] = None,
        schedules=None,
        clock=time.time,
        context_ttl: int = CONTEXT_TTL_SECONDS,
        suggestion_ttl: int = SUGGESTION_TTL_SECONDS,
    ):
        self.identity_store = identity_store if identity_store is not None else PgIdentityStore()
        self.agent_runner = agent_runner
        self.rate_limit = rate_limit
        self._save_session = save_session
        self._get_session = get_session
        self._delete_session = delete_session
        self._save_task = save_task
        self._on_feedback = on_feedback
        self.deliver_assets = deliver_assets
        self.schedules = schedules
        self.clock = clock
        self.context_ttl = context_ttl
        self.suggestion_ttl = suggestion_ttl
        self._contexts: dict[str, dict] = {}
        self._suggestions: dict[str, dict] = {}

    async def handle(self, message: IncomingMessage, send: Callable[[str], None]) -> None:
        """按固定顺序处理：限流、身份、重置、反馈、确认、分析。"""
        if self._limited(message):
            send(RATE_LIMIT_TEXT)
            return

        user = self.identity_store.resolve(message.channel, message.user_ext_id)
        if user is None:
            logger.info(
                "拒绝未授权用户 channel=%s external_id=%s",
                message.channel,
                message.user_ext_id,
            )
            send(permission_denied_text(message))
            return

        text = message.text.strip()
        if not text:
            send("请发送文字消息。")
            return

        if text in RESET_COMMANDS:
            self._reset(user)
            send(RESET_TEXT)
            return

        if is_confirmation(text):
            status = self._suggestion_status(user.user_id)
            if status == "ready":
                text = self._take_suggestion(user.user_id) or text
            elif status == "expired":
                send(SUGGESTION_EXPIRED_TEXT)
                return
            elif text in POSITIVE_FEEDBACK or text in NEGATIVE_FEEDBACK:
                # 「好的」两边都在。没有待确认建议时，它是赞，不是确认。
                polarity = "positive" if text in POSITIVE_FEEDBACK else "negative"
                self._record_feedback(user, polarity)
                send(FEEDBACK_TEXT)
                return
        elif text in POSITIVE_FEEDBACK or text in NEGATIVE_FEEDBACK:
            polarity = "positive" if text in POSITIVE_FEEDBACK else "negative"
            self._record_feedback(user, polarity)
            send(FEEDBACK_TEXT)
            return

        schedule_reply = self._schedule_reply(text, user, message)
        if schedule_reply is not None:
            send(schedule_reply)
            return

        send(ANALYZING_TEXT)
        last_context = self._load_context(user) if is_follow_up(text) else None
        try:
            reply, metadata = await self._run_agent(text, user, last_context)
        except Exception:
            logger.exception(
                "分析调用失败 user_id=%s channel=%s", user.user_id, message.channel
            )
            send(UNEXPECTED_ERROR_TEXT)
            return

        if not reply or reply.startswith("Agent service"):
            logger.error(
                "分析入口返回不可用回复 user_id=%s reply=%s",
                user.user_id,
                (reply or "")[:120],
            )
            send(AGENT_UNAVAILABLE_TEXT)
            return

        visible, marked_sql = strip_sql_marker(reply)
        self._remember(user, message_text=text, visible=visible, sql=marked_sql, metadata=metadata or {})
        send(visible)
        self._deliver_report(user, message, visible, metadata or {})

    def _schedule_reply(self, text: str, user: ResolvedUser, message: IncomingMessage):
        try:
            from reports.schedule_commands import handle_schedule_command
        except Exception:
            logger.exception("定时报告模块不可用")
            return None
        return handle_schedule_command(text, user, message, self.schedules)

    def _deliver_report(self, user: ResolvedUser, message: IncomingMessage, visible: str, metadata: dict) -> None:
        if not metadata.get("report") or self.deliver_assets is None:
            return
        try:
            from reports.files import materialize_report

            artifacts = materialize_report(user.user_id, visible, metadata)
            self.deliver_assets(user, message, artifacts)
        except Exception:
            logger.exception("报告附件发送失败 user_id=%s", user.user_id)

    def _limited(self, message: IncomingMessage) -> bool:
        checker = self.rate_limit
        if checker is None:
            from utils.web_auth import check_rate_limit

            checker = check_rate_limit
        key = f"{message.channel}:{message.user_ext_id}"
        decision = checker(key, "bot")
        return decision is not None

    async def _run_agent(self, text: str, user: ResolvedUser, last_context: Optional[dict]):
        runner = self.agent_runner or _default_agent_runner
        return await runner(
            text,
            user_role=user.role,
            last_context=last_context,
            user_id=user.user_id,
            user_name=user.display_name or user.user_id,
        )

    def _reset(self, user: ResolvedUser) -> None:
        self._contexts.pop(user.user_id, None)
        self._suggestions.pop(user.user_id, None)
        deleter = self._delete_session or _default_delete_session
        try:
            deleter(user.user_id)
        except Exception:
            logger.exception("重置会话失败 user_id=%s", user.user_id)

    def _record_feedback(self, user: ResolvedUser, polarity: str) -> None:
        context = self._contexts.get(user.user_id) or {}
        handler = self._on_feedback or _default_on_feedback
        try:
            handler(user, polarity, context)
        except Exception:
            logger.exception("反馈写入失败 user_id=%s polarity=%s", user.user_id, polarity)

    def _remember(self, user: ResolvedUser, message_text: str, visible: str, sql: str, metadata: dict) -> None:
        query_result = metadata.get("query_result") or {}
        executed = query_result.get("sql_executed") or sql
        context = {
            "last_sql": executed,
            "last_result_summary": (visible or "")[:200],
            "last_analysis": (metadata.get("analysis") or visible or "")[:500],
            "last_user_query": message_text,
            "cache_id": (metadata.get("plan_task") or {}).get("cache_id") or query_result.get("cache_id"),
            "saved_at": self.clock(),
        }
        self._contexts[user.user_id] = context
        saver = self._save_session or _default_save_session
        try:
            saver(user.user_id, user.display_name or user.user_id, context, message_text)
        except Exception:
            logger.exception("会话保存失败 user_id=%s", user.user_id)

        suggested = metadata.get("suggested_query")
        if suggested:
            self._suggestions[user.user_id] = {
                "suggested_query": suggested,
                "saved_at": self.clock(),
            }
        elif visible and not visible.startswith("⚠️"):
            self._suggestions.pop(user.user_id, None)

        plan_task = metadata.get("plan_task") or {}
        if plan_task or query_result:
            task_writer = self._save_task or _default_save_task
            try:
                task_writer(
                    user_id=user.user_id,
                    user_name=user.display_name or user.user_id,
                    prompt=message_text,
                    plan=plan_task,
                    results=[{
                        "total_rows": query_result.get("row_count", 0),
                        "success": query_result.get("success", False),
                        "sql_executed": executed,
                    }],
                )
            except Exception:
                logger.exception("任务保存失败 user_id=%s", user.user_id)

    def _load_context(self, user: ResolvedUser) -> Optional[dict]:
        cached = self._contexts.get(user.user_id)
        if cached and self.clock() - cached.get("saved_at", 0) <= self.context_ttl:
            return cached
        if cached:
            self._contexts.pop(user.user_id, None)
        loader = self._get_session or _default_get_session
        try:
            stored = loader(user.user_id)
        except Exception:
            logger.exception("读取会话失败 user_id=%s", user.user_id)
            return None
        if not stored:
            return None
        plan, question = stored
        if isinstance(plan, dict) and plan.get("last_sql"):
            restored = dict(plan)
            restored.setdefault("last_user_query", question or "")
            restored.setdefault("saved_at", self.clock())
            self._contexts[user.user_id] = restored
            return restored
        return None

    def _suggestion_status(self, user_id: str) -> str:
        """ready：有未过期建议。expired：建议刚过期并已清掉。none：本来就没有。"""
        entry = self._suggestions.get(user_id)
        if not entry:
            return "none"
        if self.clock() - entry.get("saved_at", 0) > self.suggestion_ttl:
            self._suggestions.pop(user_id, None)
            return "expired"
        return "ready"

    def _take_suggestion(self, user_id: str) -> Optional[str]:
        entry = self._suggestions.get(user_id)
        if not entry:
            return None
        if self.clock() - entry.get("saved_at", 0) > self.suggestion_ttl:
            self._suggestions.pop(user_id, None)
            return None
        self._suggestions.pop(user_id, None)
        return entry.get("suggested_query")


async def _default_agent_runner(prompt: str, user_role=None, last_context=None, user_id="", user_name=""):
    from agent_forward import forward_to_langgraph

    return await forward_to_langgraph(
        prompt,
        user_role=user_role,
        last_context=last_context,
        user_id=user_id,
        user_name=user_name,
    )


def _default_save_session(user_id, user_name, plan, question):
    from session_store_pg import save_session

    save_session(user_id, user_name, plan, question)


def _default_get_session(user_id):
    from session_store_pg import get_session

    return get_session(user_id)


def _default_delete_session(user_id):
    from session_store_pg import delete_session

    delete_session(user_id)


def _default_save_task(**kwargs):
    from core.task_log import save_task

    return save_task(**kwargs)


def _default_on_feedback(user: ResolvedUser, polarity: str, context: dict) -> None:
    """把赞/踩记到上一轮真实问题上。赞踩如何升降缓存由后续查询复用模块处理。"""
    from learning.feedback_loop import feedback_loop

    feedback_loop.on_analysis_feedback(
        user_query=context.get("last_user_query") or "",
        analysis=context.get("last_analysis") or "",
        feedback=polarity,
    )
    cache_id = context.get("cache_id")
    if not cache_id:
        return
    try:
        from learning.query_cache import query_cache

        query_cache.record_vote(int(cache_id), polarity)
    except Exception:
        logger.exception("查询缓存的赞踩没有写入 cache_id=%s", cache_id)

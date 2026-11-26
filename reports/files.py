"""按用户存放报告文件。别人的 ID 打不开，过期文件会删。"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from typing import Optional

from reports.charts import write_charts
from reports.export import write_docx, write_markdown

_OWNER_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ID_RE = re.compile(r"^[a-f0-9]{32}$")
ALLOWED_NAMES = {"report.md", "report.docx", "series.png", "breakdown.png"}
DEFAULT_TTL_SECONDS = 7 * 24 * 3600


def report_root() -> str:
    return os.environ.get("REPORT_STORE_DIR", os.path.join("data", "reports"))


def ttl_seconds() -> int:
    raw = os.environ.get("REPORT_TTL_DAYS", "7").strip()
    try:
        days = int(raw)
    except ValueError:
        days = 7
    return max(1, days) * 24 * 3600


def save_report(owner: str, text: str, facts_list: list, root: str = None, now: float = None) -> dict:
    owner = _owner(owner)
    root = root or report_root()
    report_id = _new_id()
    folder = os.path.join(root, owner, report_id)
    os.makedirs(folder, exist_ok=False)
    write_markdown(os.path.join(folder, "report.md"), text)
    images = []
    for facts in facts_list or []:
        if not isinstance(facts, dict):
            continue
        for path in write_charts(facts, folder):
            name = os.path.basename(path)
            if name not in images:
                images.append(name)
    write_docx(
        os.path.join(folder, "report.docx"),
        text,
        [os.path.join(folder, name) for name in images],
    )
    created = time.time() if now is None else now
    meta = {
        "owner": owner,
        "report_id": report_id,
        "created_at": created,
        "files": ["report.md", "report.docx", *images],
        "dir": folder,
    }
    with open(os.path.join(folder, "meta.json"), "w", encoding="utf-8") as handle:
        json.dump(meta, handle)
    cleanup(root=root, now=created)
    return meta


def resolve_file(owner: str, report_id: str, name: str, root: str = None, now: float = None) -> Optional[str]:
    """只返回属于 owner 且未过期的文件。其余情况一律 None。"""
    root = root or report_root()
    cleanup(root=root, now=time.time() if now is None else now)
    try:
        owner = _owner(owner)
    except ValueError:
        return None
    if not _ID_RE.fullmatch(report_id or "") or name not in ALLOWED_NAMES:
        return None
    folder = os.path.join(root, owner, report_id)
    meta_path = os.path.join(folder, "meta.json")
    if not os.path.isfile(meta_path):
        return None
    with open(meta_path, "r", encoding="utf-8") as handle:
        meta = json.load(handle)
    if meta.get("owner") != owner or name not in (meta.get("files") or []):
        return None
    path = os.path.join(folder, name)
    if not os.path.isfile(path):
        return None
    return path


def cleanup(root: str = None, now: float = None, ttl: int = None) -> int:
    root = root or report_root()
    now = time.time() if now is None else now
    ttl = ttl_seconds() if ttl is None else ttl
    removed = 0
    if not os.path.isdir(root):
        return 0
    for owner in os.listdir(root):
        owner_dir = os.path.join(root, owner)
        if not os.path.isdir(owner_dir):
            continue
        for report_id in os.listdir(owner_dir):
            folder = os.path.join(owner_dir, report_id)
            meta_path = os.path.join(folder, "meta.json")
            created = None
            if os.path.isfile(meta_path):
                try:
                    with open(meta_path, "r", encoding="utf-8") as handle:
                        created = float(json.load(handle).get("created_at") or 0)
                except (OSError, ValueError, json.JSONDecodeError):
                    created = 0
            elif os.path.isdir(folder):
                created = os.path.getmtime(folder)
            if created is not None and now - created > ttl:
                shutil.rmtree(folder, ignore_errors=True)
                removed += 1
    return removed


def materialize_report(owner: str, text: str, metadata: dict, root: str = None) -> dict:
    facts_list = []
    for part in (metadata or {}).get("report_parts") or []:
        facts = ((part or {}).get("result") or {}).get("computed_facts")
        if isinstance(facts, dict) and facts.get("row_count"):
            facts_list.append(facts)
    if not facts_list:
        facts = ((metadata or {}).get("query_result") or {}).get("computed_facts")
        if isinstance(facts, dict):
            facts_list.append(facts)
    return save_report(owner, text, facts_list, root=root)


def download_hint(report_id: str, files: list) -> str:
    lines = ["", "报告文件只保留给当前用户，过期后删除："]
    for name in files:
        if name in ALLOWED_NAMES:
            lines.append(f"- /api/reports/{report_id}?name={name}")
    return "\n".join(lines)


def _owner(owner: str) -> str:
    value = (owner or "").strip()
    if not _OWNER_RE.fullmatch(value):
        raise ValueError("非法的用户标识")
    return value


def _new_id() -> str:
    return os.urandom(16).hex()

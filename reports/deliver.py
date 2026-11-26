"""把报告图表和 docx 发到私聊。失败时保留已经发出的文字。"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("skadi.deliver")


def deliver_feishu(api, open_id: str, artifacts: dict) -> None:
    """上传主图和 docx。调用方必须保证 open_id 是报告主人的私聊。"""
    folder_files = artifacts.get("paths") or {}
    image = folder_files.get("series.png") or folder_files.get("breakdown.png")
    if image and os.path.isfile(image):
        try:
            image_key = api.upload_image(image)
            api.send_image(open_id, image_key)
        except Exception:
            logger.exception("飞书图片发送失败 open_id=%s", open_id)
    docx = folder_files.get("report.docx")
    if docx and os.path.isfile(docx):
        try:
            file_key = api.upload_file(docx, os.path.basename(docx))
            api.send_file(open_id, file_key)
        except Exception:
            logger.exception("飞书文件发送失败 open_id=%s", open_id)


def deliver_dingtalk(reply_markdown, reply_image, title: str, text: str, artifacts: dict) -> None:
    """钉钉支持 markdown 时发 markdown；图片接口存在时再发本地图片。"""
    image = (artifacts.get("paths") or {}).get("series.png")
    body = text if len(text) < 4000 else text[:4000] + "\n\n（正文已截断，完整文件见服务端下载）"
    if reply_markdown is not None:
        try:
            reply_markdown(title or "分析报告", body)
        except Exception:
            logger.exception("钉钉 markdown 发送失败")
    if reply_image is not None and image and os.path.isfile(image):
        try:
            reply_image(image)
        except Exception:
            logger.exception("钉钉图片发送失败")

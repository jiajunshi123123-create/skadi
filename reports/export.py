"""把报告写成 Markdown 和带图的 docx。"""

from __future__ import annotations

import os
from typing import Iterable


def write_markdown(path: str, text: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text if text.endswith("\n") else text + "\n")
    return path


def write_docx(path: str, text: str, images: Iterable[str] = ()) -> str:
    from docx import Document
    from docx.shared import Inches

    os.makedirs(os.path.dirname(path), exist_ok=True)
    document = Document()
    for line in (text or "").splitlines():
        if line.startswith("## "):
            document.add_heading(line[3:].strip(), level=1)
        elif line.startswith("# "):
            document.add_heading(line[2:].strip(), level=0)
        elif line.startswith("- "):
            document.add_paragraph(line[2:].strip(), style="List Bullet")
        elif line.startswith("### "):
            document.add_heading(line[4:].strip(), level=2)
        else:
            document.add_paragraph(line)
    for image in images:
        if image and os.path.isfile(image):
            document.add_picture(image, width=Inches(6.2))
    document.save(path)
    return path

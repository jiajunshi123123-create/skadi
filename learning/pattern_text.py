"""中文问题不能按空格切词。这里抽出可用于 ILIKE 的词。"""

from __future__ import annotations

import re


def keywords_for_match(query: str) -> list[str]:
    text = (query or "").replace("？", " ").replace("?", " ")
    words = []
    for chunk in re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9_]+", text):
        if re.fullmatch(r"[A-Za-z0-9_]+", chunk):
            if len(chunk) > 1:
                words.append(chunk.lower())
            continue
        words.append(chunk)
        if len(chunk) > 2:
            words.extend(chunk[i:i + 2] for i in range(len(chunk) - 1))
    unique = []
    for word in words:
        if word not in unique:
            unique.append(word)
    return unique[:12]

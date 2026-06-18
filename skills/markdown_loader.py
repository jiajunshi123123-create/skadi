"""Markdown 技能文件解析器

借鉴 Claude Code 的 skill/loader.py:
- Markdown + YAML frontmatter 格式
- 自动发现 skills/ 目录下的技能文件
- 解析 frontmatter 元数据（名称、关键词、描述、版本）
- 提取 prompt_snippet 用于注入 Analysis Agent

文件格式:
    ---
    name: 趋势分析
    keywords: [趋势, 走势, 变化, 增长, 下降]
    version: "1.0"
    category: statistical
    ---

    # 趋势分析方法论

    分析步骤: ...
"""

import os
import re
import logging
import sys
from typing import Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# skills 目录
SKILLS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'skills')


@dataclass
class SkillMetadata:
    """技能元数据（从 frontmatter 解析）"""
    name: str                    # 技能名称
    keywords: list = field(default_factory=list)  # 匹配关键词
    version: str = '1.0'         # 版本
    category: str = 'general'    # 分类: statistical/testing/ml
    description: str = ''        # 技能描述
    author: str = ''             # 作者
    enabled: bool = True         # 是否启用
    priority: int = 0            # 优先级（越高越优先匹配）
    prompt_snippet: str = ''     # 提取的分析方法指导

    def to_dict(self) -> dict:
        return {
            'name': self.name,
            'keywords': self.keywords,
            'version': self.version,
            'category': self.category,
            'description': self.description,
            'priority': self.priority,
            'enabled': self.enabled,
        }


class MarkdownSkillLoader:
    """Markdown 技能文件加载器

    特性:
    - 从 skills/ 目录发现所有 .md 技能文件
    - 解析 YAML frontmatter 元数据
    - 提取 prompt_snippet (非元数据内容)
    - 支持关键词匹配（用于自动技能激活）
    - 兼容现有的 Python 技能类（skills/*.py）

    文件格式要求:
        ---
        name: 技能名称
        keywords: [关键词1, 关键词2]
        category: statistical
        version: "1.0"
        ---

        # 分析方法论内容...
    """

    FRONTMATTER_PATTERN = re.compile(
        r'^---\s*\n(.*?)\n---\s*\n',
        re.DOTALL
    )

    def __init__(self, skills_dir: str = SKILLS_DIR):
        self.skills_dir = skills_dir
        self._cache: dict[str, SkillMetadata] = {}

    def discover(self) -> list[str]:
        """发现 skills/ 目录下的所有 Markdown 技能文件。

        Returns:
            .md 文件路径列表
        """
        md_files = []
        if not os.path.isdir(self.skills_dir):
            logger.warning(f"[SkillLoader] skills目录不存在: {self.skills_dir}")
            return md_files

        for fname in os.listdir(self.skills_dir):
            if fname.endswith('.md') and not fname.startswith('_'):
                md_files.append(os.path.join(self.skills_dir, fname))

        logger.info(f"[SkillLoader] 发现 {len(md_files)} 个Markdown技能文件")
        return md_files

    def load(self, filepath: str) -> Optional[SkillMetadata]:
        """加载单个 Markdown 技能文件。

        Args:
            filepath: .md 文件路径

        Returns:
            SkillMetadata 或 None（解析失败时）
        """
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            logger.error(f"[SkillLoader] 读取技能文件失败 {filepath}: {e}")
            return None

        # 解析 frontmatter
        match = self.FRONTMATTER_PATTERN.match(content)
        if not match:
            logger.warning(f"[SkillLoader] {filepath} 缺少frontmatter元数据")
            return None

        frontmatter_text = match.group(1)
        body_text = content[match.end():]

        # 解析 YAML frontmatter（简化版，不依赖pyyaml）
        metadata = self._parse_frontmatter(frontmatter_text)

        # 设置技能名称（从文件名推断）
        if not metadata.get('name'):
            fname = os.path.basename(filepath)
            metadata['name'] = fname.replace('.md', '').replace('_', ' ')

        # 提取 prompt_snippet（去掉 frontmatter 的剩余内容）
        metadata['prompt_snippet'] = body_text.strip()

        skill = SkillMetadata(**metadata)
        self._cache[skill.name] = skill

        logger.info(
            f"[SkillLoader] 加载技能: {skill.name} "
            f"({skill.category}, keywords={skill.keywords[:3]}...)"
        )
        return skill

    def load_all(self) -> dict[str, SkillMetadata]:
        """加载所有发现的技能文件。

        Returns:
            {name: SkillMetadata} 字典
        """
        skills = {}
        for filepath in self.discover():
            skill = self.load(filepath)
            if skill:
                skills[skill.name] = skill
        return skills

    def get(self, name: str) -> Optional[SkillMetadata]:
        """按名称获取技能。

        Returns:
            SkillMetadata 或 None
        """
        if name in self._cache:
            return self._cache[name]
        # 尝试从文件加载
        for filepath in self.discover():
            skill = self.load(filepath)
            if skill and skill.name == name:
                return skill
        return None

    def match_query(
        self, query: str, top_k: int = 3
    ) -> list[SkillMetadata]:
        """根据用户查询匹配最合适的技能。

        按关键词命中数和优先级排序。

        Args:
            query: 用户查询文本
            top_k: 返回前K个技能

        Returns:
            匹配的技能列表（按相关性降序）
        """
        scored = []

        for skill in self._cache.values():
            if not skill.enabled:
                continue

            # 计算关键词命中数
            hits = sum(1 for kw in skill.keywords if kw in query)
            if hits > 0:
                # 得分 = 命中数 * 10 + 优先级
                score = hits * 10 + skill.priority
                scored.append((score, skill))

        # 按得分降序
        scored.sort(key=lambda x: x[0], reverse=True)
        return [s for _, s in scored[:top_k]]

    def list_all(self) -> list[SkillMetadata]:
        """列出所有已加载的技能。"""
        return list(self._cache.values())

    def list_by_category(self, category: str) -> list[SkillMetadata]:
        """按分类列出技能。"""
        return [s for s in self._cache.values() if s.category == category]

    def _parse_frontmatter(self, text: str) -> dict:
        """解析 YAML frontmatter（简化版，不依赖pyyaml）。

        支持以下字段：
        - name: 字符串
        - keywords: YAML列表 ['a', 'b', 'c'] 或 [a, b, c]
        - version: 字符串
        - category: 字符串
        - description: 字符串
        - author: 字符串
        - enabled: 布尔
        - priority: 整数

        Args:
            text: YAML frontmatter 文本

        Returns:
            解析后的字典
        """
        result = {}

        for line in text.strip().split('\n'):
            line = line.strip()
            if not line or line.startswith('#'):
                continue

            if ':' in line:
                key, _, value = line.partition(':')
                key = key.strip().lower()
                value = value.strip()

                # 处理不同值类型
                if value.startswith('[') and value.endswith(']'):
                    # 列表: [a, b, c] 或 ['a', 'b', 'c']
                    items = value[1:-1].split(',')
                    result[key] = [
                        item.strip().strip("'").strip('"')
                        for item in items if item.strip()
                    ]
                elif value.lower() in ('true', 'yes'):
                    result[key] = True
                elif value.lower() in ('false', 'no'):
                    result[key] = False
                elif value.isdigit():
                    result[key] = int(value)
                else:
                    result[key] = value.strip("'").strip('"')

        return result

    def reload(self) -> int:
        """重新加载所有技能文件。

        Returns:
            加载的技能数量
        """
        self._cache.clear()
        skills = self.load_all()
        return len(skills)


# 全局实例
_markdown_skill_loader: Optional[MarkdownSkillLoader] = None


def get_markdown_skill_loader() -> MarkdownSkillLoader:
    """获取全局 MarkdownSkillLoader 实例（延迟初始化）。"""
    global _markdown_skill_loader
    if _markdown_skill_loader is None:
        _markdown_skill_loader = MarkdownSkillLoader()
        _markdown_skill_loader.load_all()
    return _markdown_skill_loader

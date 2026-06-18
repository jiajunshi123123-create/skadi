"""分析技能系统 - 可插拔的数据分析方法模块

设计理念：
- 每个技能是一个独立的分析视角/方法论
- 通过关键词匹配自动激活相关技能
- 技能通过 prompt snippet 注入 Analysis Agent 的上下文
- 可选提供 Python 层面的数据计算能力

使用方式：
    from skills import SkillRegistry
    registry = SkillRegistry()
    matched = registry.match_query("昨天日活趋势怎么样")
    # → [TrendSkill, AnomalySkill]
"""

import os
import importlib
import logging
from typing import List, Optional

from .base import AnalysisSkill

logger = logging.getLogger(__name__)


class SkillRegistry:
    """技能注册中心 - 自动发现并管理所有分析技能"""

    def __init__(self):
        self._skills: List[AnalysisSkill] = []
        self._discover_skills()

    def _discover_skills(self):
        """自动发现 skills/ 目录下所有技能模块"""
        skills_dir = os.path.dirname(os.path.abspath(__file__))
        for filename in os.listdir(skills_dir):
            if filename.endswith('.py') and not filename.startswith('_') and filename != 'base.py':
                module_name = filename[:-3]
                try:
                    module = importlib.import_module(f'skills.{module_name}')
                    # 查找模块中继承 AnalysisSkill 的类
                    for attr_name in dir(module):
                        attr = getattr(module, attr_name)
                        if (isinstance(attr, type) and
                            issubclass(attr, AnalysisSkill) and
                                attr is not AnalysisSkill):
                            skill_instance = attr()
                            self._skills.append(skill_instance)
                            logger.info(
                                f"[SkillRegistry] 发现技能: {skill_instance.name} "
                                f"({skill_instance.category})"
                            )
                except Exception as e:
                    logger.warning(f"[SkillRegistry] 加载技能模块 {module_name} 失败: {e}")

        logger.info(f"[SkillRegistry] 共加载 {len(self._skills)} 个分析技能")

    def match_query(self, query: str, top_k: int = 3) -> List[AnalysisSkill]:
        """
        根据用户查询匹配最相关的技能

        Args:
            query: 用户自然语言查询
            top_k: 返回最多几个技能

        Returns:
            按匹配度降序排列的技能列表
        """
        scored = []
        for skill in self._skills:
            score = skill.match_score(query)
            if score > 0:
                scored.append((score, skill))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [skill for _, skill in scored[:top_k]]

    def get_all(self) -> List[AnalysisSkill]:
        """获取所有已注册的技能"""
        return list(self._skills)

    def get_by_name(self, name: str) -> Optional[AnalysisSkill]:
        """按名称查找技能"""
        for skill in self._skills:
            if skill.name == name:
                return skill
        return None

    def get_by_category(self, category: str) -> List[AnalysisSkill]:
        """按分类获取技能"""
        return [s for s in self._skills if s.category == category]

    def list_skills_info(self) -> List[dict]:
        """列出所有技能的基本信息（供前端展示）"""
        return [
            {
                'name': s.name,
                'description': s.description,
                'category': s.category,
                'keywords': s.keywords[:5],
            }
            for s in self._skills
        ]


# 全局单例
skill_registry = SkillRegistry()

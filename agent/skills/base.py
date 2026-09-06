"""Skill 抽象基类：所有技能的统一契约。

规则：Skill.run() 绝不向上抛裸异常——任何失败都封装进 SkillResult。
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any

from ..logger import log
from ..models import SkillResult


class Skill(ABC):
    """技能基类。子类只需实现 run()，外层自动兜底异常并计时。"""

    name: str = "base_skill"
    description: str = ""

    @abstractmethod
    async def run(self, **kwargs) -> SkillResult:
        """统一执行入口。"""

    # ---- 通用兜底包装：供 Task 层调用 ----
    async def execute(self, **kwargs) -> SkillResult:
        """带异常兜底与计时的执行包装。"""
        started = time.monotonic()
        try:
            result = await self.run(**kwargs)
        except Exception as exc:  # noqa: BLE001 —— Skill 边界兜底
            log.exception("[%s] 技能执行异常", self.name)
            result = SkillResult(success=False, error=f"{type(exc).__name__}: {exc}")
        result.meta.setdefault("skill", self.name)
        result.meta.setdefault("took_ms", int((time.monotonic() - started) * 1000))
        return result

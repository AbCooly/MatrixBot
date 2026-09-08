"""Task 抽象基类与上下文。

Task 负责把多个 Skill 串成完整业务流程，并产出可落盘的 TaskReport。
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..config import Settings
from ..logger import log
from ..models import TaskReport
from ..skills.comment_manager import CommentManager
from ..skills.content_generator import ContentGenerator
from ..skills.media_creator import MediaCreator
from ..skills.platform_uploader import PlatformUploader
from ..skills.search_trends import SearchTrends


@dataclass
class TaskContext:
    """注入给 Task 的运行时上下文：配置 + 共享的 Skill 实例。"""

    settings: Settings
    search: SearchTrends
    content: ContentGenerator
    media: MediaCreator
    uploader: PlatformUploader
    comments: CommentManager
    task_config: dict = field(default_factory=dict)  # tasks.yaml 中本任务的配置
    progress_cb: Callable[[dict], None] | None = None  # WebUI 实时进度回调


class Task(ABC):
    """任务基类。"""

    name: str = "base_task"

    @abstractmethod
    async def execute(self, ctx: TaskContext) -> TaskReport:
        """执行任务并返回报告。"""

    # ---- 节奏工具：任务内部随机间隔（打散“连发/固定节奏”这类机器指纹） ----
    @staticmethod
    def _parse_gap(value, default_min: float, default_max: float) -> tuple[float, float]:
        """把任务配置里的间隔值解析成 [min, max] 秒。

        支持：None → 默认区间；int/float → [0, N]；
        "min-max" 字符串 → [min, max]；dict{min,max} / 二元组/list → 对应区间。
        """
        if value is None:
            return default_min, default_max
        if isinstance(value, bool):
            return default_min, default_max
        if isinstance(value, (int, float)):
            return 0.0, max(0.0, float(value))
        if isinstance(value, str):
            text = value.strip().replace("~", "-").replace("，", ",")
            if "-" in text:
                try:
                    lo, hi = (float(x.strip()) for x in text.split("-", 1))
                    return lo, hi
                except ValueError:
                    return default_min, default_max
            try:
                n = float(text)
            except ValueError:
                return default_min, default_max
            return 0.0, max(0.0, n)
        if isinstance(value, dict):
            lo = float(value.get("min", default_min))
            hi = float(value.get("max", default_max))
            return lo, hi
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            return float(value[0]), float(value[1])
        return default_min, default_max

    async def _random_gap(self, cfg: dict, key: str, default_min: float = 60.0,
                          default_max: float = 600.0, why: str = "") -> None:
        """在任务内两个动作之间插入随机间隔并休眠。

        cfg[key] 支持 int/"min-max"/{min,max}/[a,b]；未配置用 default 区间。
        用于：平台间发布错峰、同平台多次动作之间抖动，避免固定间隔。
        """
        lo, hi = self._parse_gap(cfg.get(key), default_min, default_max)
        if hi <= 0:
            return
        seconds = random.uniform(lo, hi)
        why = f"（{why}）" if why else ""
        log.info("[%s] 节奏抖动：等待 %.1f~%.1f → 本次 %.1f 秒 %s",
                 self.name, lo, hi, seconds, why)
        await asyncio.sleep(seconds)

    # ---- 报告工具 ----
    def _new_report(self) -> TaskReport:
        return TaskReport(
            task_name=self.name,
            started_at=datetime.now().isoformat(timespec="seconds"),
            finished_at="",
            success=True,
        )

    def _finish(self, report: TaskReport) -> TaskReport:
        report.finished_at = datetime.now().isoformat(timespec="seconds")
        report.success = report.error == "" and not any(s["status"] == "failed" for s in report.steps)
        return report

    def _step(self, report: TaskReport, step: str, detail: str = "", status: str = "ok") -> None:
        """记录一步执行结果。"""
        log.info("[%s] 步骤 %s: %s (%s)", self.name, step, detail, status)
        entry = {"step": step, "status": status, "detail": str(detail)[:500], "took_ms": 0}
        report.steps.append(entry)
        # 实时进度回调（WebUI 通过 TaskContext.progress_cb 订阅）
        ctx = getattr(self, "_ctx", None)
        if ctx and ctx.progress_cb:
            try:
                ctx.progress_cb(entry)
            except Exception:  # noqa: BLE001
                pass

    def _mark_step_failed(self, report: TaskReport, step: str, detail: str = "") -> None:
        """把某一步标记为 failed（用于业务失败而非异常的步骤）。"""
        for s in report.steps:
            if s["step"] == step:
                s["status"] = "failed"
                s["detail"] = str(detail)[:500]
                break
        if not report.error:
            report.error = f"{step}: {detail}"

    @staticmethod
    def save_report(report: TaskReport, logs_dir: Path) -> Path:
        """把报告落盘 logs/reports/<task>_<date>.json。"""
        report_dir = logs_dir / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        date = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path = report_dir / f"{report.task_name}_{date}.json"
        path.write_text(
            json.dumps(
                {
                    "task_name": report.task_name,
                    "started_at": report.started_at,
                    "finished_at": report.finished_at,
                    "success": report.success,
                    "steps": report.steps,
                    "error": report.error,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return path

    async def safe_step(self, report: TaskReport, step: str, coro, detail: str = "") -> Any:
        """执行一步并捕获异常（单步失败不中断整个任务，但记录 failed）。"""
        started = time.monotonic()
        try:
            result = await coro
            self._step(report, step, detail or "ok")
            return result
        except Exception as exc:  # noqa: BLE001 —— Task 边界兜底
            self._step(report, step, f"{type(exc).__name__}: {exc}", status="failed")
            if not report.error:
                report.error = f"{step}: {exc}"
            return None

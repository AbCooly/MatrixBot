"""Cron 调度器：APScheduler 封装（常驻进程内定时触发 Task）。

- 配置驱动：config/tasks.yaml 定义任务与 5 段标准 cron 表达式（分 时 日 月 周）
- 常驻：main 进程持有事件循环，进程不退出即持续生效（配合 nohup/systemd 使用）
- 手动触发：run_once() 供 CLI 与测试使用，不依赖调度器运行
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from ..config import Settings
from ..logger import log

if TYPE_CHECKING:
    from ..tasks.base import Task, TaskContext


def parse_cron(expr: str) -> dict:
    """把 5 段标准 cron 表达式转成 APScheduler CronTrigger 参数。

    "分 时 日 月 周" → {minute, hour, day, month, day_of_week}（秒固定 0）。
    APScheduler 的每个字段都接受 "*/5"、"1-5"、"1,3" 等写法，直接透传。
    """
    fields = expr.strip().split()
    if len(fields) != 5:
        raise ValueError(f"cron 表达式必须为 5 段（分 时 日 月 周），收到: {expr!r}")
    minute, hour, day, month, dow = fields
    return {
        "minute": minute,
        "hour": hour,
        "day": day,
        "month": month,
        "day_of_week": dow,
    }


class Scheduler:
    """任务调度器：加载 tasks.yaml，注册 Task 并常驻运行。"""

    def __init__(self, settings: Settings, yaml_path: str | Path | None = None):
        self._settings = settings
        self._yaml_path = Path(yaml_path) if yaml_path else (
            Path(__file__).resolve().parent.parent.parent / "config" / "tasks.yaml"
        )
        self._scheduler = None  # APScheduler AsyncIOScheduler
        self._tasks: dict[str, Task] = {}
        self._config: dict = {}

    # ---------------- 构建 ----------------
    def _load_config(self) -> dict:
        """读取 tasks.yaml（不存在则返回空配置）。"""
        if not self._yaml_path.exists():
            log.warning("未找到调度配置 %s，调度器将不注册任何任务", self._yaml_path)
            return {}
        import yaml as _yaml

        with open(self._yaml_path, encoding="utf-8") as fh:
            return _yaml.safe_load(fh) or {}

    def build_context(self, task_config: dict | None = None, progress_cb=None) -> "TaskContext":
        """构建 TaskContext：实例化所有 Skill（共享同一个浏览器池）。"""
        from ..skills.comment_manager import CommentManager
        from ..skills.content_generator import ContentGenerator
        from ..skills.media_creator import MediaCreator
        from ..skills.platform_uploader import PlatformUploader
        from ..skills.search_trends import SearchTrends
        from ..tasks.base import TaskContext

        uploader = PlatformUploader(self._settings)
        return TaskContext(
            settings=self._settings,
            search=SearchTrends(),
            content=ContentGenerator(self._settings),
            media=MediaCreator(self._settings),
            uploader=uploader,
            comments=CommentManager(self._settings, uploader.pool),
            task_config=task_config or {},
            progress_cb=progress_cb,
        )

    # ---------------- 注册与运行 ----------------
    def register(self, name: str, cron_expr: str, task: "Task") -> None:
        """注册一个任务（cron 为 5 段表达式）。"""
        self._tasks[name] = task
        log.info("注册任务 [%s] cron=%s", name, cron_expr)

    def start(self) -> None:
        """常驻启动：按 tasks.yaml 注册任务并进入阻塞调度。"""
        asyncio.run(self._run_scheduler())

    async def _run_scheduler(self) -> None:
        """异步调度主循环（AsyncIOScheduler 需要运行中的事件循环）。

        每 30s 检测一次 tasks.yaml 是否变化，变化则热重载任务计划——
        WebUI / AI 工坊新建或修改定时任务后，无需重启调度器即可自动生效。
        """
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        self._config = self._load_config()
        scheduler = AsyncIOScheduler()
        self._schedule_from_config(scheduler)
        self._last_yaml_mtime = self._yaml_mtime()
        self._scheduler = scheduler
        scheduler.start()
        log.info("调度器已启动（常驻，每 30s 自动检测 tasks.yaml 变更）。Ctrl+C 退出。")
        try:
            while True:
                await asyncio.sleep(30)
                self._maybe_reload(scheduler)
        except (KeyboardInterrupt, SystemExit):
            log.info("收到退出信号，关闭调度器")

    def _schedule_from_config(self, scheduler) -> None:
        """按当前 self._config 重建任务计划（先清空再注册，支持热重载）。"""
        try:
            scheduler.remove_all_jobs()
        except Exception:  # noqa: BLE001
            pass
        tasks_cfg = self._config.get("tasks") or {}
        if not tasks_cfg:
            log.warning("tasks.yaml 中没有任何任务配置")
        scheduled = 0
        for name, cfg in tasks_cfg.items():
            if not cfg.get("enabled", True):
                continue
            cron_expr = cfg.get("cron")
            if not cron_expr:
                log.warning("任务 [%s] 缺少 cron 配置，跳过", name)
                continue
            # 内置任务优先；未注册的任务按 cfg.type 实例化（WebUI 自定义任务）
            task = self._tasks.get(name)
            if task is None:
                task = _build_task_from_type(cfg.get("type"))
            if task is None:
                log.warning("任务 [%s] 未注册到代码且无有效 type，跳过", name)
                continue
            try:
                trigger_args = parse_cron(cron_expr)
            except ValueError as exc:
                log.error("任务 [%s] cron 配置错误: %s", name, exc)
                continue
            task_config = {k: v for k, v in cfg.items() if k not in ("cron", "enabled", "type")}
            scheduler.add_job(
                self._run_task, trigger="cron", args=[name, task, task_config], **trigger_args,
                id=name, replace_existing=True, misfire_grace_time=300,
            )
            log.info("已排程 [%s] cron=%s type=%s", name, cron_expr, cfg.get("type", "内置"))
            scheduled += 1
        log.info("任务计划已刷新：共 %d 个定时任务", scheduled)

    def _yaml_mtime(self) -> float:
        try:
            return self._yaml_path.stat().st_mtime
        except Exception:  # noqa: BLE001
            return 0.0

    def _maybe_reload(self, scheduler) -> None:
        """tasks.yaml 有变更则热重载（WebUI/AI 工坊落盘即生效）。"""
        try:
            if self._yaml_mtime() == self._last_yaml_mtime:
                return
        except Exception:  # noqa: BLE001
            return
        log.info("检测到 tasks.yaml 变化，热重载任务计划…")
        self._config = self._load_config()
        self._schedule_from_config(scheduler)
        self._last_yaml_mtime = self._yaml_mtime()

    async def _run_task(self, name: str, task: "Task", task_config: dict) -> None:
        """调度回调：异步执行任务（异常不阻断调度）。"""
        log.info("========== 调度触发任务 [%s] ==========", name)
        ctx = self.build_context(task_config)
        self._last_ctx = ctx
        try:
            report = await task.execute(ctx)
            log.info("========== 任务 [%s] 结束 success=%s ==========", name, report.success)
        except Exception:  # noqa: BLE001
            log.exception("任务 [%s] 执行异常（已捕获，调度继续）", name)
        finally:
            try:
                await ctx.uploader.close()
            except Exception:  # noqa: BLE001
                pass

    async def run_once(self, name: str, progress_cb=None) -> None:
        """手动单次执行某任务（CLI --once 与测试使用）。"""
        if name not in self._tasks:
            raise KeyError(f"未知任务: {name}（可选: {list(self._tasks)}）")
        self._config = self._load_config()
        task_cfg = ((self._config.get("tasks") or {}).get(name) or {})
        task_config = {k: v for k, v in task_cfg.items() if k not in ("cron", "enabled")}
        task = self._tasks[name]
        ctx = self.build_context(task_config, progress_cb=progress_cb)
        self._last_ctx = ctx
        report = await task.execute(ctx)
        if hasattr(ctx, "uploader"):
            await ctx.uploader.close()
        log.info("任务 [%s] 完成 success=%s", name, report.success)


def _build_task_from_type(task_type) -> "Task | None":
    """按 type 字段实例化任务（WebUI 自定义任务复用内置任务逻辑）。"""
    if task_type == "daily_post":
        from ..tasks.daily_post import DailyPostTask

        return DailyPostTask()
    if task_type == "maintain_traffic":
        from ..tasks.maintain_traffic import MaintainTrafficTask

        return MaintainTrafficTask()
    return None

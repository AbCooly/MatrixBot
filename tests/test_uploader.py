"""Platform_Uploader 与 Scheduler 的纯逻辑测试（不启动浏览器）。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent.config import Settings
from agent.models import PostPayload
from agent.scheduler.cron import parse_cron
from agent.skills.platform_uploader import PlatformUploader

TEST_STATE = Path("/tmp/agent-test-state")


def _settings() -> Settings:
    return Settings(state_dir=TEST_STATE)


class TestParseCron:
    def test_standard_5_fields(self):
        """标准 5 段 cron → APScheduler 参数（秒固定 0）。"""
        args = parse_cron("0 9 * * *")
        assert args == {"minute": "0", "hour": "9", "day": "*", "month": "*", "day_of_week": "*"}

    def test_interval(self):
        args = parse_cron("*/30 8-23 * * *")
        assert args["minute"] == "*/30"
        assert args["hour"] == "8-23"

    def test_invalid_length(self):
        with pytest.raises(ValueError):
            parse_cron("0 9 * *")      # 只有 4 段
        with pytest.raises(ValueError):
            parse_cron("0 9 * * * *")  # 6 段


class TestRegistry:
    def test_register_and_get(self):
        """注册表应能按平台名取到适配器。"""
        uploader = PlatformUploader(_settings())
        try:
            assert uploader.registry.get("douyin") is not None
            assert uploader.registry.get("xiaohongshu") is not None
            assert uploader.registry.get("wechat_channels") is not None
            assert {"douyin", "xiaohongshu", "wechat_channels", "zhihu", "toutiao"} <= set(uploader.registry.platforms())
        finally:
            asyncio.run(uploader.close())

    def test_unknown_platform_raises(self):
        uploader = PlatformUploader(_settings())
        with pytest.raises(ValueError):
            uploader._adapter("facebook")

    def test_run_unknown_action(self):
        uploader = PlatformUploader(_settings())
        result = asyncio.run(uploader.run(action="不存在的动作"))
        assert result.success is False

    def test_publish_requires_media(self):
        """空载荷（无图无视频）应返回 failed 而非异常。"""
        async def _t():
            uploader = PlatformUploader(_settings())
            try:
                payload = PostPayload(platform="douyin", title="t", content="c")
                result = await uploader.publish(payload)
                assert result.success is False
                assert result.status == "failed"
            finally:
                await uploader.close()

        asyncio.run(_t())


class TestSchedulerRunOnce:
    async def test_run_once_executes_registered_task(self, settings, tmp_path, monkeypatch):
        """run_once 应执行已注册任务。"""
        from agent.scheduler.cron import Scheduler
        from agent.tasks.base import Task, TaskContext
        from agent.models import TaskReport

        executed = []

        class FakeTask(Task):
            name = "fake"

            async def execute(self, ctx: TaskContext) -> TaskReport:
                executed.append(True)
                report = self._new_report()
                return self._finish(report)

        scheduler = Scheduler(settings, yaml_path=tmp_path / "empty.yaml")
        scheduler.register("fake", "0 9 * * *", FakeTask())
        # 不构建真实浏览器上下文（此处仅验证调度逻辑）
        monkeypatch.setattr(scheduler, "build_context", lambda cfg, progress_cb=None: object())
        await scheduler.run_once("fake")
        assert executed

    async def test_run_once_unknown_task(self, settings, tmp_path):
        from agent.scheduler.cron import Scheduler

        scheduler = Scheduler(settings, yaml_path=tmp_path / "empty.yaml")
        with pytest.raises(KeyError):
            await scheduler.run_once("不存在")


class TestCustomTaskType:
    def test_build_task_from_type(self):
        """WebUI 自定义任务按 type 实例化内置任务。"""
        from agent.scheduler.cron import _build_task_from_type

        from agent.tasks.daily_post import DailyPostTask
        from agent.tasks.maintain_traffic import MaintainTrafficTask

        assert isinstance(_build_task_from_type("daily_post"), DailyPostTask)
        assert isinstance(_build_task_from_type("maintain_traffic"), MaintainTrafficTask)
        assert _build_task_from_type("未知类型") is None

    async def test_run_scheduler_registers_custom_task(self, tmp_path, monkeypatch):
        """_run_scheduler 应把带 type 的自定义任务排程（不抛异常）。"""
        import asyncio

        from agent.scheduler.cron import Scheduler

        cfg = tmp_path / "tasks.yaml"
        cfg.write_text(
            "tasks:\n"
            "  custom:\n"
            "    enabled: true\n"
            "    cron: '0 10 * * 6'\n"
            "    type: daily_post\n"
            "    platforms: ['douyin']\n",
            encoding="utf-8",
        )
        scheduler = Scheduler(__import__("agent.config").config.Settings(state_dir=tmp_path / "state"),
                              yaml_path=cfg)
        # 不真正启动：直接验证排程注册（捕获 add_job 调用）
        registered = []
        import apscheduler.schedulers.asyncio as mod

        class FakeScheduler:
            def __init__(self):
                self.jobs = []

            def add_job(self, fn, **kw):
                self.jobs.append(kw)

            def start(self):
                pass

        monkeypatch.setattr(mod, "AsyncIOScheduler", FakeScheduler)

        class FakeEvent:
            def __init__(self):
                pass

            async def wait(self):
                return True  # 立即放行，避免常驻挂起

        monkeypatch.setattr("agent.scheduler.cron.asyncio.Event", FakeEvent)
        await scheduler._run_scheduler()
        assert any(j.get("id") == "custom" for j in scheduler._scheduler.jobs)

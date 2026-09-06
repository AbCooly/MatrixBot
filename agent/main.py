"""CLI 入口。

用法：
  python -m agent.main                常驻运行（加载 config/tasks.yaml 启动调度）
  python -m agent.main --once daily_post       立即执行一次每日发帖
  python -m agent.main --once maintain_traffic 立即执行一次流量维护
  python -m agent.main --verify                检查所有平台登录状态

登录不在 CLI：请在 WebUI 打开对应账号的「远程驾驶舱」人工完成（扫码/滑块/短信验证码），
再用 --verify 确认登录态。

常驻方式（进程保持存活）：
  nohup python -m agent.main >> logs/stdout.log 2>&1 &
  # 或 systemd / supervisor 托管，重启自动恢复登录态（Cookie 在 state/browsers/）
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from .config import load_settings
from .logger import log
from .scheduler.cron import Scheduler
from .tasks.daily_post import DailyPostTask
from .tasks.maintain_traffic import MaintainTrafficTask


def build_scheduler() -> Scheduler:
    """构建调度器并注册全部任务（新增任务在此注册）。"""
    settings = load_settings()
    scheduler = Scheduler(settings)
    scheduler.register("daily_post", "0 9 * * *", DailyPostTask())
    scheduler.register("maintain_traffic", "*/30 8-23 * * *", MaintainTrafficTask())
    return scheduler


async def _verify(platform: str | None) -> int:
    """检查登录状态。"""
    settings = load_settings()
    from .skills.platform_uploader import PlatformUploader

    uploader = PlatformUploader(settings)
    try:
        targets = [platform] if platform else ["douyin", "xiaohongshu", "wechat_channels"]
        for p in targets:
            try:
                ok = await uploader.check_login(p)
                log.info("%s 登录状态: %s", p, "✅ 已登录" if ok else "❌ 未登录")
            except Exception as exc:  # noqa: BLE001
                log.warning("%s 检查失败: %s", p, exc)
        return 0
    finally:
        await uploader.close()


async def _run_once(task_name: str) -> int:
    """手动单次执行任务。"""
    scheduler = build_scheduler()
    try:
        await scheduler.run_once(task_name)
        return 0
    except KeyError as exc:
        log.error("%s", exc)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="DeepSeek 社交媒体全自动运营智能体")
    parser.add_argument("--once", metavar="TASK", help="单次执行任务: daily_post | maintain_traffic")
    parser.add_argument("--verify", nargs="?", const="", metavar="PLATFORM", help="检查登录状态")
    args = parser.parse_args()

    if args.once:
        return asyncio.run(_run_once(args.once))
    if args.verify is not None:
        return asyncio.run(_verify(args.verify or None))

    # 默认：常驻调度
    scheduler = build_scheduler()
    scheduler.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""WebUI 人工接管（仅容器化 Browser-GUI 模式）。

自动化登录常被滑块/扫码/风控卡住，本模块只负责把「人工」送进浏览器：

  prepare_managed(settings, platform, profile):
    1. 向 browser-gui 守护进程 ensure + pin 该「平台×账号」浏览器实例
       （pin 保留期内不会被闲置回收/淘汰，防止登录做到一半窗口被切走）；
    2. 把浏览器导航到该平台登录/首页（窗口出现在 Xvfb 桌面，noVNC 可见）。
  然后前端打开 /remote-desktop（noVNC 全屏页），用户在真实桌面里扫码/拖滑块/
  输验证码。完成后关页面回 WebUI，后台登录态队列自动刷新。

v2 简化：已移除 Local/CDP 模式的截图驾驶舱（RemoteSession 截图流 + 鼠标键盘事件
转发），只保留容器化 noVNC 接管这一条路径。
"""
from __future__ import annotations

import asyncio

from .logger import log

# 平台「人工接管」导航地址 —— 一律用平台首页/主页（而不是硬编码登录页）：
#   已登录账号 → 直接进入自己的主页/后台，保持登录态不受打扰；
#   未登录账号 → 平台会自动跳转到扫码/手机号登录页，同样能完成登录。
_LOGIN_URL = {
    "douyin": "https://creator.douyin.com/creator-micro/home",
    "xiaohongshu": "https://creator.xiaohongshu.com/",
    "wechat_channels": "https://channels.weixin.qq.com/platform",
    "zhihu": "https://www.zhihu.com",
    "toutiao": "https://mp.toutiao.com/",
}


def prepare_managed(settings, platform: str, profile: str | None = None) -> dict:
    """容器化「人工接管」准备：拉起账号浏览器窗口到 noVNC 桌面并打开登录/首页。

    与旧的截图驾驶舱不同：不建截图流会话，真实操作交给 noVNC 远程桌面。
    """
    from .skills.uploader.browser import BrowserManagerClient, BrowserPool

    if platform not in _LOGIN_URL:
        return {"ok": False, "error": f"未知平台: {platform}"}
    if not settings.browser_mgr_url:
        return {"ok": False, "error": "未配置 BROWSER_MGR_URL（仅支持容器化部署，请用 docker compose 启动）"}
    account = f"{platform}__{profile}" if profile else platform

    async def _run() -> dict:
        # 1) pin + 确保实例运行（守护进程负责启动/复用/上限淘汰）
        client = BrowserManagerClient(settings.browser_mgr_url)
        try:
            info = await client.ensure_browser(account, pin=True)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"浏览器守护启动失败: {exc}"}
        # 2) 打开平台首页/主页（未登录自动跳登录页，已登录不打断）
        pool = BrowserPool(settings)
        try:
            _, page = await pool.get_page(platform, profile)
            url = _LOGIN_URL.get(platform)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_timeout(2500)  # 等重定向/页面渲染稳定后再给人工
            except Exception:  # noqa: BLE001 —— 部分加载也留给人工处理
                pass
            return {
                "ok": True,
                "account": account,
                "url": page.url if not page.is_closed() else url,
                "cdp_url": info.get("cdp_url", ""),
                "message": "浏览器窗口已打开（远程桌面可见），请打开远程桌面完成登录",
            }
        finally:
            # close_all 只断开 CDP 连接，不关闭浏览器/不清登录态
            await pool.close_all()

    try:
        return asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        log.warning("人工接管准备失败 [%s]: %s", account, exc)
        return {"ok": False, "error": f"准备失败: {exc}"}

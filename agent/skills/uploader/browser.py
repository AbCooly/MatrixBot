"""浏览器访问层（仅容器化 Browser-GUI 模式）。

架构（v2 简化）：
  本项目只支持「容器化部署」一条路径：浏览器全部跑在 docker/browser-gui 守护进程里，
  每「平台×账号」= 一个独立有头 Chromium（Xvfb 桌面窗口 + 独立 CDP 端口），
  noVNC(:6080) 可人工接管（扫码/滑块/验证码），登录态落盘共享卷 state/browsers/。
  agent 通过 HTTP + CDP 连到这些实例，不再本机起浏览器、不再连外部 Chrome。

  守护进程负责：严格串行（MAX_INSTANCES=1）调度、闲置回收、人工接管 pin 保留。
  agent 的 BrowserPool 每 45s 向守护续租一次，防止任务中途实例被回收。

使用：
  pool = BrowserPool(settings)
  context, page = await pool.get_page("douyin", "主号")   # 独立账号窗口
"""
from __future__ import annotations

import asyncio
import os
import threading
from typing import TYPE_CHECKING

from ...config import Settings
from ...logger import log

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Page

# 本进程持有浏览器期间定期向守护续租，防止被闲置回收/淘汰。
HB_INTERVAL_S = 45.0

# 页面级“自动化痕迹”遮蔽（纵深防御）：
# 本架构的 Chromium 由守护进程手动拉起（非 playwright.launch），navigator.webdriver
# 本来就是 false；此脚本主要防“会话中途被注入/被二次导航”这类场景，并顺手抹掉
# 自动化构造器残留标记，降低被页面脚本直接读取判定的概率。
_STEALTH_JS = """() => {
  try {
    const defGet = (obj, prop, val) => {
      try { Object.defineProperty(obj, prop, { get: () => val, configurable: true }); }
      catch (e) {}
    };
    // 1) webdriver 标记：真实 Chrome 里它是 false，杜绝任何渠道被置 true
    try { if (window.navigator.webdriver !== false) defGet(window.navigator, 'webdriver', false); }
    catch (e) { defGet(window.navigator, 'webdriver', false); }
    // 2) 桌面鼠标会话：没有触摸屏的机器 maxTouchPoints 为 0（避免环境不一致）
    try { if (window.navigator.maxTouchPoints > 0) defGet(window.navigator, 'maxTouchPoints', 0); }
    catch (e) {}
    // 3) 常见自动化注入残留（Playwright/旧版库构造器命名痕迹）
    for (const k of ['cdc_adoQpoasnfa76pfcZLmcfl_Array',
                     'cdc_adoQpoasnfa76pfcZLmcfl_Promise',
                     'cdc_adoQpoasnfa76pfcZLmcfl_Symbol']) {
      try { if (k in window) delete window[k]; } catch (e) {}
    }
  } catch (e) {}
}"""


def context_key(platform: str, profile: str | None = None) -> str:
    """上下文缓存 key：platform[/profile]。"""
    return f"{platform}/{profile}" if profile else platform


def account_dir_name(platform: str, profile: str | None = None) -> str:
    """浏览器数据目录名（守护进程侧账号名：platform__profile / platform）。"""
    return f"{platform}__{profile}" if profile else platform


# 平台 → 站点域名特征（跨运行复用已开标签页，避免每次任务堆新 tab）
PLATFORM_DOMAINS = {
    "douyin": "creator.douyin.com",
    "xiaohongshu": "creator.xiaohongshu.com",
    "wechat_channels": "channels.weixin.qq.com",
    "zhihu": "zhihu.com",
    "toutiao": "mp.toutiao.com",
}


def _platform_match(platform: str, url: str) -> bool:
    domain = PLATFORM_DOMAINS.get(platform)
    return bool(domain) and domain in url


class BrowserPool:
    """管理到 browser-gui 守护进程的浏览器连接。"""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._contexts: dict[str, BrowserContext] = {}
        self._lock = asyncio.Lock()
        self._pages: dict[str, Page] = {}
        self._mgr: BrowserManagerClient | None = None
        self._mgr_browsers: dict[str, "Browser"] = {}
        self._hb_accounts: set[str] = set()   # 已 ensure 的账号（保活对象）
        self._hb_thread: threading.Thread | None = None
        self._hb_stop = threading.Event()
        self._stealth_done: set[int] = set()  # 已注入遮蔽脚本的 context（按 id）

    @property
    def managed_enabled(self) -> bool:
        """容器化部署：浏览器跑在 browser-gui 守护容器里。"""
        return bool(self._settings.browser_mgr_url)

    @property
    def mode(self) -> str:
        return "managed"

    async def _ensure_mgr_browser(self, key: str, platform: str,
                                  profile: str | None = None) -> "Browser":
        """向守护进程要/复用该账号的独立 Chromium 实例（守护负责启动/复用/上限调度）。"""
        browser = self._mgr_browsers.get(key)
        if browser is not None and browser.is_connected():
            return browser
        if self._mgr is None:
            if not self._settings.browser_mgr_url:
                raise RuntimeError(
                    "未配置 BROWSER_MGR_URL（仅支持容器化 Browser-GUI 部署）。"
                    "请用 docker compose 启动：docker compose up -d --build"
                )
            self._mgr = BrowserManagerClient(self._settings.browser_mgr_url)
        info = await self._mgr.ensure_browser(account_dir_name(platform, profile))
        from playwright.async_api import async_playwright

        self._pw = getattr(self, "_pw", None)
        if self._pw is None:
            self._pw = await async_playwright().start()
        try:
            browser = await self._pw.chromium.connect_over_cdp(info["cdp_url"])
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"连接账号浏览器失败（{info.get('cdp_url')}）。请确认 browser-gui 守护进程正常，"
                f"可打开 WebUI 远程桌面查看对应窗口。原始错误: {exc}"
            ) from exc
        self._mgr_browsers[key] = browser
        self._hb_accounts.add(account_dir_name(platform, profile))
        self._start_heartbeat()
        log.info("已连接账号浏览器 [%s] → %s", key, info["cdp_url"])
        return browser

    def _start_heartbeat(self) -> None:
        """保活：只要本 pool 还持有该浏览器，就周期性向守护续租。"""
        if not self.managed_enabled or self._hb_thread is not None:
            return
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop, name="browser-heartbeat", daemon=True
        )
        self._hb_thread.start()

    def _heartbeat_loop(self) -> None:
        while not self._hb_stop.wait(HB_INTERVAL_S):
            accounts = list(self._hb_accounts)
            if not accounts or self._mgr is None:
                continue
            for account in accounts:
                try:
                    asyncio.run(self._mgr.heartbeat_browser(account))
                except Exception:  # noqa: BLE001 —— 保活失败不致命，下轮重试
                    pass

    async def _arm_stealth(self, context: BrowserContext) -> None:
        """给 context 注册“新文档自动遮蔽”脚本（每 context 只注册一次）。

        已打开的旧文档不会自动生效，需配合 _apply_stealth_now 即时覆盖一次。
        """
        key = id(context)
        if key in self._stealth_done:
            return
        try:
            await context.add_init_script(_STEALTH_JS)
            self._stealth_done.add(key)
        except Exception:  # noqa: BLE001 —— 注入失败不致命，页面级仍会兜底
            log.info("webdriver 遮蔽注入失败（context 级，页面级已兜底）")

    async def _apply_stealth_now(self, page: Page) -> None:
        """对已加载/正在加载的页面即时执行一次遮蔽（幂等）。"""
        try:
            await page.evaluate(_STEALTH_JS)
        except Exception:  # noqa: BLE001 —— about:blank / 加载中都会安全失败
            pass

    async def _claim_platform_page(self, context: BrowserContext, key: str, platform: str) -> Page:
        """在 context 内认领/新建该平台的标签页（避免每次任务堆积新 tab）。"""
        await self._arm_stealth(context)
        page = self._pages.get(key)
        if page is not None and not page.is_closed():
            await self._apply_stealth_now(page)
            return page
        claimed = {id(p) for p in self._pages.values()}
        page = next(
            (p for p in context.pages
             if not p.is_closed() and id(p) not in claimed
             and _platform_match(platform, p.url)),
            None,
        )
        if page is None:
            page = await context.new_page()
        page.set_default_timeout(self._settings.browser_timeout_ms)
        await self._apply_stealth_now(page)
        self._pages[key] = page
        return page

    async def new_page(self, platform: str, profile: str | None = None) -> tuple[BrowserContext, Page]:
        """发布专用：开全新标签页（避免复用 tab 的状态脏/冲突）。"""
        async with self._lock:
            key = context_key(platform, profile)
            browser = await self._ensure_mgr_browser(key, platform, profile)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            await self._arm_stealth(context)
            page = await context.new_page()
            page.set_default_timeout(self._settings.browser_timeout_ms)
            await self._apply_stealth_now(page)
            return context, page

    async def get_page(self, platform: str, profile: str | None = None) -> tuple[BrowserContext, Page]:
        """获取平台页面，返回 (context, page)：每「平台×账号」独立浏览器实例。"""
        async with self._lock:
            key = context_key(platform, profile)
            browser = await self._ensure_mgr_browser(key, platform, profile)
            context = self._contexts.get(key)
            if context is None:
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
                await self._arm_stealth(context)
                self._contexts[key] = context
            return context, await self._claim_platform_page(context, key, platform)

    async def close_all(self) -> None:
        """退出时断开与守护的连接（不关闭浏览器实例/不清登录态，窗口与登录态保留）。"""
        self._hb_stop.set()
        self._hb_accounts.clear()
        if self._hb_thread is not None:
            self._hb_thread.join(timeout=2)
            self._hb_thread = None
        pw = getattr(self, "_pw", None)
        if pw is not None:
            try:
                await pw.stop()
            except Exception:  # noqa: BLE001
                pass
            self._pw = None
            self._mgr_browsers.clear()
            self._contexts.clear()
            self._pages.clear()


class BrowserManagerClient:
    """browser-gui 守护进程的 HTTP 客户端。

    ensure_browser(account) → 守护保证该账号的独立有头 Chromium 在运行并返回 CDP 信息。
    首次访问启动实例（窗口出现在 Xvfb 桌面，noVNC 可见），之后复用；
    容器重启后按 user-data-dir/端口映射自动恢复，登录态不丢。
    """

    def __init__(self, base_url: str, timeout: float = 15.0):
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        # 守护进程访问令牌（cloud 部署防内网提权；未配置则不发，兼容本地调试）
        self._token = os.environ.get("GUARDIAN_TOKEN", "")

    def _headers(self) -> dict:
        return {"X-Guardian-Token": self._token} if self._token else {}

    async def ensure_browser(self, account: str, heartbeat: bool = False,
                             pin: bool | None = None) -> dict:
        """确保浏览器实例运行，返回 {"account","port","cdp_url", ...}。

        heartbeat=True：仅续租（刷新守护侧空闲计时，避免被闲置回收），不启动实例。
        pin=True/False：设置/解除「人工接管」保留——WebUI 远程桌面打开某账号窗口时
        置 pin，该实例在 PIN_TTL 内不会被淘汰或闲置回收。
        """
        import httpx

        body: dict = {"account": account}
        if heartbeat:
            body["heartbeat"] = True
        if pin is not None:
            body["pin"] = bool(pin)
        async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers()) as client:
            resp = await client.post(f"{self._base}/browser", json=body)
        if resp.status_code >= 400:
            raise RuntimeError(f"浏览器守护 ensure 失败（HTTP {resp.status_code}）: {resp.text}")
        return resp.json()

    async def heartbeat_browser(self, account: str) -> bool:
        """续租某账号浏览器（守护侧刷新空闲计时，防止闲置回收）。"""
        try:
            info = await self.ensure_browser(account, heartbeat=True)
            return bool(info.get("running"))
        except Exception:  # noqa: BLE001
            return False

    async def list_browsers(self) -> list[dict]:
        """列出守护进程管理的全部浏览器实例。"""
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers()) as client:
            resp = await client.get(f"{self._base}/browsers")
        if resp.status_code >= 400:
            return []
        return resp.json().get("browsers", [])

    async def _delete(self, url: str, account: str) -> dict:
        """DELETE 请求（兼容旧版 httpx：client.delete 可能不支持 json 参数）。"""
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers()) as client:
            resp = await client.request("DELETE", url, json={"account": account})
            resp.raise_for_status()
            return resp.json()

    async def stop_browser(self, account: str) -> None:
        """关闭某账号浏览器实例（登录态目录保留，下次 ensure 会以同端口重启）。"""
        await self._delete(f"{self._base}/browser", account)

    async def delete_account(self, account: str) -> dict:
        """彻底删除账号：停实例 + 清持久化端口映射 + 删除登录态目录。

        返回 {"account","stopped","removed_map","dir_exists"}。用于 WebUI
        「删除账号」，删除后守护 list_browsers 与目录扫描都不再出现该账号。
        """
        return await self._delete(f"{self._base}/profile", account)

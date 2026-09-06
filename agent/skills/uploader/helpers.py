"""Playwright 韧性选择器工具（移植自 social_media_pubulish_MCP 的 playwright-helpers，改 Python）。

设计思想：平台前端频繁改版，单一选择器极易失效。
所有交互都走"多选择器候选 + 可见性/可用性校验 + 兜底 evaluate"。
"""
from __future__ import annotations

import asyncio
import random
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Locator, Page


def fit_text(value: str | None, limit: int) -> str:
    """把标题等文本收敛到平台限制（中英文均按 1 字符计）：

    - 折叠换行/连续空白（平台输入框通常不接受换行标题）；
    - 超出 limit 直接截断（按字符截，不会拆坏多字节字符）。
    """
    if not value:
        return value or ""
    value = " ".join(str(value).split())
    if len(value) <= limit:
        return value
    return value[:limit]


async def type_human(page: Page, value: str) -> None:
    """逐段键入并模拟真人节奏：随机打字间隔 + 随机停顿。

    相比 locator.fill() 的“瞬时填完”，平台的脚本运营检测更容易放行
    带停顿的键盘输入（小红书尤其明显）。
    """
    value = str(value or "")
    if not value:
        return
    parts = re.split(r"(?<=[。！？!?，,.;；\n])", value) or [value]
    for part in parts:
        if not part:
            continue
        i = 0
        n = len(part)
        while i < n:
            size = random.randint(10, 28)
            chunk = part[i:i + size]
            await page.keyboard.type(chunk, delay=random.randint(30, 95))
            i += size
            # 随机“思考停顿”（约一半概率），避免机械匀速
            if random.random() < 0.45:
                await page.wait_for_timeout(random.randint(120, 650))
        if random.random() < 0.3:
            await page.wait_for_timeout(random.randint(200, 500))


async def first_visible_locator(page: Page, selectors: list[str]) -> Locator | None:
    """按顺序返回第一个可见的定位器。"""
    for selector in selectors:
        locators = page.locator(selector)
        count = await locators.count()
        for i in range(count):
            loc = locators.nth(i)
            try:
                if await loc.is_visible():
                    return loc
            except Exception:  # noqa: BLE001 —— 元素可能已脱离 DOM
                continue
    return None


async def click_first_usable(page: Page, selectors: list[str]) -> bool:
    """点击第一个可见且可用的元素。"""
    loc = await first_visible_locator(page, selectors)
    if loc is None:
        return False
    try:
        await loc.click()
        return True
    except Exception:  # noqa: BLE001
        return False


async def fill_first_visible(page: Page, selectors: list[str], value: str) -> bool:
    """填充第一个可见输入框（拟人键盘输入，失败兜底一次性 fill）。"""
    loc = await first_visible_locator(page, selectors)
    if loc is None:
        return False
    try:
        await loc.click()
        await type_human(page, value)
        return True
    except Exception:  # noqa: BLE001
        try:
            await loc.fill(value)
            return True
        except Exception:  # noqa: BLE001
            return False


async def has_any_visible(page: Page, selectors: list[str]) -> bool:
    """任一选择器可见即返回 True。"""
    return (await first_visible_locator(page, selectors)) is not None


async def wait_for_any_visible(page: Page, selectors: list[str], timeout: float = 30.0) -> bool:
    """轮询等待任一选择器可见。"""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if await has_any_visible(page, selectors):
            return True
        await page.wait_for_timeout(500)
    return False


async def safe_goto(page: Page, url: str) -> None:
    """带网络空闲等待的跳转（失败不抛，交由调用方判断）。"""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_load_state("networkidle", timeout=8_000)
    except Exception:  # noqa: BLE001
        pass


async def page_text(page: Page) -> str:
    """当前页面可见文本（空白归一化），供正则判断页面状态。"""
    try:
        text = await page.evaluate("() => document.body ? document.body.textContent || '' : ''")
        return " ".join(str(text).split())
    except Exception:  # noqa: BLE001
        return ""

async def dismiss_popups(page, max_rounds: int = 4) -> bool:
    """关闭页面上的推广/激励/引导类弹窗（头条"首发激励"等）。

    策略（每轮）：检测弹窗容器 → ESC → × 图标/关闭按钮 → 按钮文案（知道了/暂不/跳过/取消）。
    返回是否已无弹窗。
    """
    dialog_selectors = [
        "[role='dialog']", "[class*='modal']", "[class*='Modal']",
        "[class*='dialog']", "[class*='Dialog']",
        "[class*='popup']", "[class*='popover']",
    ]

    async def _has_dialog() -> bool:
        return await has_any_visible(page, dialog_selectors)

    for _ in range(max_rounds):
        if not await _has_dialog():
            return True
        # 1) ESC
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(600)
        except Exception:  # noqa: BLE001
            pass
        if not await _has_dialog():
            return True
        # 2) 关闭按钮（按文案优先级）
        clicked = await click_first_usable(page, [
            "button:has-text('暂不')", "button:has-text('知道了')", "button:has-text('跳过')",
            "button:has-text('取消')", "button:has-text('关闭')", "button:has-text('不参加')",
            "text=/暂不|知道了|跳过|取消/",
        ])
        if clicked:
            await page.wait_for_timeout(800)
            continue
        # 3) 弹窗内 × 图标
        try:
            closed = await page.evaluate(
                """() => {
                    const dialogs = Array.from(document.querySelectorAll(
                        '[role="dialog"],[class*="modal"],[class*="Modal"],[class*="dialog"],[class*="popup"]'));
                    for (const d of dialogs) {
                        if (d.getBoundingClientRect().width < 100) continue;
                        const nodes = Array.from(d.querySelectorAll('*'));
                        const close = nodes.find(n => {
                            const cls = (n.className||'').toString().toLowerCase();
                            return (cls.includes('close') || cls.includes('icon-close')) &&
                                   (n.getBoundingClientRect().width >= 8);
                        });
                        if (close) { close.click(); return true; }
                    }
                    return false;
                }"""
            )
            if closed:
                await page.wait_for_timeout(800)
                continue
        except Exception:  # noqa: BLE001
            pass
        # 本轮未关掉，可能不是弹窗（如常驻面板），停止避免误操作
        return False
    return not await _has_dialog()

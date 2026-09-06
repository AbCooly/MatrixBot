"""知乎适配器：文章发布 + 登录态检测。

调研结论：知乎无公开个人发布 API，自动化走"人工登录 + Cookie 持久化 + 浏览器自动化"
（参考 blog-auto-publishing-tools / zhihu-cli 的思路，Apache/GPL 许可下借鉴流程设计）。

- 登录：人工在「远程驾驶舱」打开 https://www.zhihu.com/signin 完成扫码/验证
- 发布：zhuanlan.zhihu.com/write 发布文章（标题 + 富文本正文 + 话题）

注意：知乎风控较强，选择器可能随改版失效，联调时按日志调整。
"""
from __future__ import annotations

import asyncio
import re

from ...logger import log
from ...models import PostPayload, PublishResult
from .base import PlatformAdapter
from .helpers import click_first_usable, fill_first_visible, has_any_visible, page_text, safe_goto

SIGNIN_URL = "https://www.zhihu.com/signin"
HOME_URL = "https://www.zhihu.com/"
WRITE_URL = "https://zhuanlan.zhihu.com/write"

# 登录成功特征：知乎首页右上角有"创作中心"或个人头像入口
_LOGGED_IN_SIGNALS = [
    "text=/创作中心/", "[class*='Avatar']", "img[class*='avatar']",
    "button:has-text('写文章')",
]
_TITLE_SELECTORS = ["textarea[placeholder*='标题']", "input[placeholder*='标题']", "div[data-controller*='title']"]
_CONTENT_EDITOR = ["div[contenteditable='true']", "[data-controller*='editor'] [contenteditable='true']"]
_PUBLISH_BTN = ["button:has-text('发布')", "button:has-text('下一步')", "button:has-text('继续发布')"]
# 发布弹窗里的最终确认按钮
_CONFIRM_BTN = ["button:has-text('发布')", "button:has-text('确认发布')"]


class ZhihuAdapter(PlatformAdapter):
    """知乎适配器（文章发布）。"""

    platform = "zhihu"
    capabilities = {"image": False, "video": False, "article": True}

    # ---------------- 登录态检测 ----------------
    async def check_login(self, profile: str | None = None) -> bool:
        # 1) Cookie 特征：知乎登录后 www.zhihu.com 域会更新 SESSIONID/z_c0 会话键
        context, page = (await self._pool.get_page("zhihu", profile))
        try:
            cookies = await context.cookies()
            names = {c["name"] for c in cookies}
            if "z_c0" in names:  # 知乎登录会话键（未登录时为匿名占位）
                for c in cookies:
                    if c["name"] == "z_c0" and c.get("value") and c["value"] != "undefined":
                        log.info("知乎登录状态: 已登录（cookie: z_c0）")
                        return True
        except Exception:  # noqa: BLE001
            pass
        # 2) 页面判定兜底
        await safe_goto(page, HOME_URL)
        if "signin" in page.url or "login" in page.url:
            log.info("知乎登录状态: 未登录")
            return False
        ok = await has_any_visible(page, _LOGGED_IN_SIGNALS)
        log.info("知乎登录状态: %s", "已登录" if ok else "未登录")
        return ok

    # ---------------- 发布 ----------------
    async def publish_image(self, payload: PostPayload, profile: str | None = None) -> PublishResult:
        """知乎暂不支持图文直发（可发布"想法"带图，暂未实现），返回不支持。"""
        return PublishResult(self.platform, False, "failed", "知乎暂不支持图文发布（仅文章）")

    async def publish_article(self, payload: PostPayload, profile: str | None = None) -> PublishResult:
        """知乎发布文章（标题 + 正文 + 话题）。

        实测 2026-09：标题是 textarea（placeholder"请输入标题（最多 100 个字）"），
        正文是 Draft.js 富文本编辑器（div.public-DraftEditor-content，contenteditable），
        对 Draft 编辑器用真实键盘输入最稳（fill 时好时坏）；
        "发布"按钮在内容为空时 disabled，填入正文后解禁，点击后出发布弹窗需再确认。
        """
        page = (await self._pool.get_page("zhihu", profile))[1]
        if not await self.check_login(profile=profile):
            return PublishResult(self.platform, False, "login_required", "知乎未登录")

        await safe_goto(page, WRITE_URL)
        title_loc = page.locator("textarea[placeholder*='标题']").first
        editor_loc = page.locator(
            "div.public-DraftEditor-content[contenteditable='true'], div[contenteditable='true']"
        ).first
        try:
            await title_loc.wait_for(state="visible", timeout=20000)
            await editor_loc.wait_for(state="visible", timeout=20000)
        except Exception as exc:  # noqa: BLE001
            return PublishResult(self.platform, False, "failed", f"知乎写作页编辑器未出现: {exc}")

        # 标题（textarea，直接 fill）
        try:
            await title_loc.fill(payload.title[:100])
        except Exception as exc:  # noqa: BLE001
            return PublishResult(self.platform, False, "failed", f"知乎标题填写失败: {exc}")

        # 正文：点击 Draft 编辑器 → 全选清空（防草稿恢复）→ 键盘输入
        try:
            await editor_loc.click()
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Delete")
            await page.wait_for_timeout(300)
            body = payload.content or ""
            if payload.tags:
                body = f"{body}\n\n{' '.join(t for t in payload.tags[:5])}"
            # Draft.js 逐段输入（空行分段，过长截断）
            for paragraph in body.split("\n")[:120]:
                if paragraph:
                    await page.keyboard.type(paragraph[:500], delay=8)
                await page.keyboard.press("Enter")
        except Exception as exc:  # noqa: BLE001
            return PublishResult(self.platform, False, "failed", f"知乎正文填写失败: {exc}")

        await page.wait_for_timeout(1500)

        # 点"发布"（按钮解禁后；dry-run 会在这一步拦截）
        published = await self._click_publish(page)
        if published:
            log.info("知乎文章已发布")
            return PublishResult(self.platform, True, "published", "知乎文章已发布（请到创作中心确认）")
        return PublishResult(self.platform, False, "failed", "知乎点击发布失败（按钮未解禁或未找到）")

    async def _click_publish(self, page) -> bool:
        """点底部"发布"：第一次打开发布设置面板，第二次确认发布。

        实测 2026-09：底部按钮文本精确为"发布"（class 含 Button primary），
        另有"发布设置/添加话题"等按钮文本也含"发布"二字，不能用 has-text 模糊匹配。
        用 JS 精确匹配 textContent==='发布' 的可见按钮，取最靠下的一个，
        真实鼠标点按（dry-run 会在第一次点击时拦截）。
        """
        POINT_JS = """() => {
            const norm = s => (s||'').replace(/[\\s\\u200b]/g,'');
            const btns = Array.from(document.querySelectorAll('button')).filter(b => {
                if (norm(b.textContent) !== '发布') return false;
                if (b.disabled) return false;
                const r = b.getBoundingClientRect();
                if (r.width < 40 || r.y < 300) return false;
                const s = getComputedStyle(b);
                return s.display !== 'none' && s.visibility !== 'hidden';
            }).sort((a,b) => b.getBoundingClientRect().y - a.getBoundingClientRect().y);
            const t = btns[0];
            if (!t) return null;
            const r = t.getBoundingClientRect();
            return {x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2)};
        }"""
        deadline = asyncio.get_event_loop().time() + 30
        point = None
        while asyncio.get_event_loop().time() < deadline:
            try:
                point = await page.evaluate(POINT_JS)
            except Exception:  # noqa: BLE001
                point = None
            if point:
                break
            await page.wait_for_timeout(1000)
        if not point:
            log.warning("知乎发布按钮 30 秒内未解禁")
            return False

        # 第一次点击：展开"发布设置"面板
        log.info("知乎：点'发布'打开设置面板 @(%d,%d)", point["x"], point["y"])
        await page.mouse.move(point["x"], point["y"], steps=5)
        await page.wait_for_timeout(200)
        await page.mouse.click(point["x"], point["y"])
        await page.wait_for_timeout(2500)

        # 第二次点击：确认发布（位置不变；若弹出专栏选择等弹窗，补点确认）
        log.info("知乎：再次点'发布'确认发布 @(%d,%d)", point["x"], point["y"])
        await page.mouse.click(point["x"], point["y"])
        await page.wait_for_timeout(3000)
        await click_first_usable(page, _CONFIRM_BTN)
        await page.wait_for_timeout(3000)
        return True

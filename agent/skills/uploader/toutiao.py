"""今日头条（头条号）适配器：图文/文章发布 + 登录态检测。

调研结论：头条号无公开个人发布 API，自动化走"人工登录 + Cookie 持久化 + 浏览器自动化"
（参考 toutiao-ops / toutiao-auto-publisher / blog-auto-publishing-tools 的思路）。

- 登录：人工在「远程驾驶舱」打开 https://mp.toutiao.com/auth/page/login 完成扫码/验证
- 发布：头条号创作中心发布图文/文章

注意：头条平台风控较严，选择器可能随改版失效，联调时按日志调整。
"""
from __future__ import annotations

import asyncio

from ...logger import log
from ...models import PostPayload, PublishResult
from .base import PlatformAdapter
from .helpers import (
    click_first_usable,
    fill_first_visible,
    has_any_visible,
    page_text,
    safe_goto,
)

LOGIN_URL = "https://mp.toutiao.com/auth/page/login"
HOME_URL = "https://mp.toutiao.com/"
# 图文（微头条：正文 + 图片，无标题）；长文章走 profile_v4/graphic/publish（文章编辑器）
PUBLISH_URL = "https://mp.toutiao.com/profile_v4/weitoutiao/publish"
ARTICLE_PUBLISH_URL = "https://mp.toutiao.com/profile_v4/graphic/publish"

# 微头条"发文助手"抽屉会全页遮罩拦截操作且无关闭按钮，直接 JS 移除
_KILL_ASSISTANT_DRAWER = """() => {
    let n = 0;
    document.querySelectorAll('.byte-drawer-mask').forEach(e => { e.remove(); n++; });
    document.querySelectorAll('.publish-assistant-old-drawer, [class*="publish-assistant"]').forEach(e => { e.remove(); n++; });
    return n;
}"""

_LOGGED_IN_SIGNALS = ["text=/创作|发布|内容管理|数据分析/", "[class*='avatar']", "[class*='user-info']"]
_TITLE_SELECTORS = ["input[placeholder*='标题']", "textarea[placeholder*='标题']", "[placeholder*='标题']"]
_CONTENT_SELECTORS = [
    "div[contenteditable='true']", "textarea", "[data-testid*='editor'] [contenteditable='true']",
]
_PUBLISH_BTN = ["button:has-text('发布')", "button:has-text('发表')", "button:has-text('提交')"]
_FINAL_BTN = ["button:has-text('确认发布')", "button:has-text('发布')", "button:has-text('确定')"]


class ToutiaoAdapter(PlatformAdapter):
    """今日头条（头条号）适配器。"""

    platform = "toutiao"
    capabilities = {"image": True, "video": False, "article": True}

    # ---------------- 登录态检测 ----------------
    async def check_login(self, profile: str | None = None) -> bool:
        context, page = (await self._pool.get_page("toutiao", profile))
        # 1) 页面级判定（最可靠）：访问主页，被重定向到登录页 = 未登录
        #    （cookie 存在但可能失效——真实环境实测：sessionid 在但被踢回登录）
        try:
            await safe_goto(page, HOME_URL)
            if "login" in page.url or "auth" in page.url:
                log.info("头条登录状态: 未登录（cookie 已失效，被重定向）")
                return False
            ok = await has_any_visible(page, _LOGGED_IN_SIGNALS)
            log.info("头条登录状态: %s", "已登录" if ok else "未登录")
            return ok
        except Exception:  # noqa: BLE001
            pass
        # 2) Cookie 特征兜底
        try:
            cookies = await context.cookies()
            names = {c["name"] for c in cookies}
            if "sessionid" in names:
                log.info("头条登录状态: 已登录（cookie: sessionid，页面验证不可用）")
                return True
        except Exception:  # noqa: BLE001
            pass
        # 2) 页面判定兜底
        await safe_goto(page, HOME_URL)
        if "login" in page.url or "auth" in page.url:
            log.info("头条登录状态: 未登录")
            return False
        ok = await has_any_visible(page, _LOGGED_IN_SIGNALS)
        log.info("头条登录状态: %s", "已登录" if ok else "未登录")
        return ok

    # ---------------- 发布 ----------------
    @staticmethod
    async def _kill_assistant_drawer(page) -> None:
        """关掉"发文助手"抽屉（全页遮罩、无关闭按钮，Esc 后 JS 移除）。"""
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(500)
        except Exception:  # noqa: BLE001
            pass
        try:
            n = await page.evaluate(_KILL_ASSISTANT_DRAWER)
            if n:
                log.info("头条：移除发文助手遮罩/抽屉 %d 个节点", n)
        except Exception:  # noqa: BLE001
            pass

    async def publish_image(self, payload: PostPayload, profile: str | None = None) -> PublishResult:
        """微头条发布：正文 + 图片（实测 2026-09 链路）。

        - 页面：profile_v4/weitoutiao/publish，ProseMirror 正文（无标题）
        - 图片：点工具栏"图片"按钮后页面挂载 image/* 的 file input，直接 set_input_files
        - "发文助手"抽屉会弹全页遮罩且无关闭按钮 → JS 移除
        - 发布：右下角 primary 按钮文本即"发布"（微头条直发，无预览步骤）
        """
        from .helpers import dismiss_popups

        page = (await self._pool.get_page("toutiao", profile))[1]
        if not await self.check_login(profile=profile):
            return PublishResult(self.platform, False, "login_required", "头条未登录")

        await safe_goto(page, PUBLISH_URL)
        await page.wait_for_timeout(4000)
        await self._kill_assistant_drawer(page)
        await dismiss_popups(page)

        # 1) 填正文（微头条无标题；把标题作为首行）
        body = (payload.content or "").strip()
        if payload.title:
            body = f"{payload.title}\n{body}" if body else payload.title
        if payload.tags:
            body = f"{body}\n{' '.join('#' + t for t in payload.tags)}"
        editor = page.locator("div.ProseMirror[contenteditable='true']").first
        try:
            await editor.wait_for(state="visible", timeout=20000)
            await editor.click()
            # 微头条会自动恢复上次草稿，先全选清空再输入，避免内容叠加
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Delete")
            await page.wait_for_timeout(300)
            await page.keyboard.type(body[:1500], delay=10)
        except Exception as exc:  # noqa: BLE001
            return PublishResult(self.platform, False, "failed", f"微头条正文填写失败: {exc}")

        # 2) 上传图片：点工具栏"图片"挂载 file input → set_input_files
        uploaded = False
        if payload.images:
            try:
                await click_first_usable(page, [
                    "button.syl-toolbar-button:has-text('图片')",
                    "span.icon-wrapper:has-text('图片')",
                ])
                await page.wait_for_timeout(2000)
                img_input = page.locator("input[type='file'][accept*='image']").first
                if await img_input.count():
                    await img_input.set_input_files(payload.images[:9])
                    log.info("微头条：已选择图片 %d 张", min(len(payload.images), 9))
                    # 图片先进弹窗预览，必须点弹窗"确定"才插入编辑器；
                    # 确定按钮在图片上传完成前 disabled（实测 2026-09，dry-run 守卫会掩盖这一步）
                    confirm = page.locator("button.byte-btn-primary:has-text('确定')").last
                    for _ in range(40):
                        if await confirm.count() and await confirm.is_visible():
                            disabled_attr = await confirm.get_attribute("disabled")
                            cls = await confirm.get_attribute("class") or ""
                            if not disabled_attr and "disabled" not in cls:
                                await confirm.click()
                                uploaded = True
                                log.info("微头条：已确认插入图片")
                                break
                        await page.wait_for_timeout(1500)
            except Exception as exc:  # noqa: BLE001
                log.warning("微头条图片上传失败: %s", exc)
        if not uploaded:
            return PublishResult(self.platform, False, "failed", "微头条图片上传失败（弹窗确定按钮未出现/未点到）")

        # 3) 等图片真正进入正文编辑器（ProseMirror 内出现图片）
        for _ in range(40):
            try:
                img_count = await page.evaluate(
                    "() => { const ed = document.querySelector('.ProseMirror,[contenteditable=\"true\"]');"
                    "return ed ? Array.from(ed.querySelectorAll('img'))"
                    ".filter(i => i.getBoundingClientRect().width > 80).length : 0; }"
                )
                if img_count >= 1:
                    log.info("微头条：图片已进入编辑器（%d 张）", img_count)
                    break
            except Exception:  # noqa: BLE001
                pass
            await page.wait_for_timeout(1500)
        await page.wait_for_timeout(2000)
        await self._kill_assistant_drawer(page)

        # 4) 点"发布"（primary 按钮；定时发布是 default 样式，不命中 primary）
        clicked = await click_first_usable(page, [
            "button.byte-btn-primary:has-text('发布'):not(:has-text('定时'))",
            "button.byte-btn-primary >> text=/^发布$/",
        ])
        if not clicked:
            return PublishResult(self.platform, False, "failed", "微头条未找到发布按钮")
        await page.wait_for_timeout(5000)
        await dismiss_popups(page)
        # 成功判定：跳转内容管理 / 成功提示
        text = await page_text(page)
        ok = "发布成功" in text or "content/manage" in page.url or "/profile_v4/" in page.url and "publish" not in page.url
        if ok:
            log.info("微头条发布成功（页面已跳转/成功提示）")
        return PublishResult(self.platform, True, "published" if ok else "published_unverified",
                             "微头条已发布（请在创作中心确认）" if ok else "微头条已点击发布（未检测到成功信号，请确认）")

    async def publish_article(self, payload: PostPayload, profile: str | None = None) -> PublishResult:
        """头条长文章：profile_v4/graphic/publish 文章编辑器（标题 + 正文）。"""
        from .helpers import dismiss_popups

        page = (await self._pool.get_page("toutiao", profile))[1]
        if not await self.check_login(profile=profile):
            return PublishResult(self.platform, False, "login_required", "头条未登录")

        await safe_goto(page, ARTICLE_PUBLISH_URL)
        await page.wait_for_timeout(4000)
        await dismiss_popups(page)

        title_ok = await fill_first_visible(page, _TITLE_SELECTORS, payload.title)
        body = payload.content or ""
        if payload.tags:
            body = f"{body}\n\n{' '.join('#' + t for t in payload.tags)}"
        content_ok = await fill_first_visible(page, _CONTENT_SELECTORS, body)
        if not (title_ok and content_ok):
            return PublishResult(self.platform, False, "failed",
                                 f"头条文章标题/正文填写失败 title={title_ok} content={content_ok}")
        await page.wait_for_timeout(1000)
        await dismiss_popups(page)
        # 文章发布：预览并发布 → 确认发布
        await click_first_usable(page, ["button:has-text('预览并发布')", "button:has-text('发布')"])
        await page.wait_for_timeout(2500)
        await dismiss_popups(page)
        await click_first_usable(page, [
            "button:has-text('确认发布')", "button:has-text('确认')", "button:has-text('发布')",
        ])
        await page.wait_for_timeout(3000)
        text = await page_text(page)
        ok = "发布成功" in text or "article" in page.url or "content/manage" in page.url
        return PublishResult(self.platform, True, "published" if ok else "published_unverified",
                             "头条文章已发布（请在创作中心确认）")

    async def publish_video(self, payload: PostPayload, profile: str | None = None) -> PublishResult:
        return PublishResult(self.platform, False, "failed", "头条暂不支持视频发布（可后续扩展）")

"""小红书适配器：登录检测 + 图文发布（默认存草稿，安全优先）。

流程来源：移植 social_media_pubulish_MCP 的 src/platforms/xiaohongshu.ts（已本地分析）：
  - 创作者平台 creator.xiaohongshu.com
  - 上传后填标题/正文/话题，通过 xhs-publish-btn._onSave() 存草稿（Angular 内部方法）
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ...logger import log
from ...models import PostPayload, PublishResult
from .base import PlatformAdapter
from .helpers import fit_text, has_any_visible, safe_goto, type_human

if TYPE_CHECKING:
    from playwright.async_api import Page

HOME_URL = "https://creator.xiaohongshu.com/"
PUBLISH_IMAGE_URL = "https://creator.xiaohongshu.com/publish/publish?from=menu&target=image"

_LOGIN_SIGNALS = [
    "text=/登录|扫码|验证码/",
    "input[placeholder*='手机号']",
    "input[placeholder*='验证码']",
]
_LOGGED_IN_SIGNALS = ["text=/发布|创作|笔记|数据/", "[class*='avatar']", "[class*='user']"]


class XiaohongshuAdapter(PlatformAdapter):
    """小红书创作者平台适配器（图文草稿）。

    登录由人工在「远程驾驶舱」完成：creator.xiaohongshu.com 只提供短信验证码
    登录（无扫码入口），需在驾驶舱里填手机号并收取验证码。
    """

    platform = "xiaohongshu"
    capabilities = {"image": True, "video": False}

    # ---------------- 登录态检测 ----------------
    async def check_login(self, profile: str | None = None) -> bool:
        page = (await self._pool.get_page("xiaohongshu", profile))[1]
        await safe_goto(page, HOME_URL)
        has_login = await has_any_visible(page, _LOGIN_SIGNALS)
        has_logged = await has_any_visible(page, _LOGGED_IN_SIGNALS)
        ok = has_logged and not has_login
        log.info("小红书登录状态: %s", "已登录" if ok else "未登录")
        return ok

    # ---------------- 图文发布 ----------------
    async def publish_image(self, payload: PostPayload, profile: str | None = None) -> PublishResult:
        page = (await self._pool.get_page("xiaohongshu", profile))[1]
        if not await self.check_login():
            return PublishResult(self.platform, False, "login_required", "小红书未登录")
        if not payload.images:
            return PublishResult(self.platform, False, "failed", "小红书图文发布缺少图片")

        await safe_goto(page, PUBLISH_IMAGE_URL)
        await page.wait_for_timeout(4000)
        from .helpers import click_first_usable as _cfu

        # 站点会记住上次发布类型（实测 target=image 也可能被重定向到"上传视频"页），
        # 发现视频上传区时显式切到"上传图文"标签
        await self._ensure_image_tab(page)

        # 上传图片：先用直接 set_input_files（真实环境第一版验证能传图、草稿成功）；
        # 若页面无可用 file input 再退回"点上传图片按钮 + file chooser"。

        uploaded = False
        file_inputs = page.locator("input[type='file']")
        if await file_inputs.count() > 0:
            try:
                await file_inputs.first.set_input_files(payload.images)
                log.info("小红书上传：直接 set 文件 %d 张", len(payload.images))
                uploaded = True
            except Exception as exc:  # noqa: BLE001
                log.warning("小红书直接 set 失败: %s", exc)
        if not uploaded:
            for attempt in range(2):
                try:
                    async with page.expect_file_chooser(timeout=15000) as fc_info:
                        clicked = await _cfu(page, [
                            "button:has-text('上传图片')", "text=/上传图片/",
                            "[class*='image-upload-buttons']", "button:has-text('上传图文')",
                        ])
                        log.info("小红书上传：点击上传入口（可点=%s）", clicked)
                    chooser = await fc_info.value
                    await chooser.set_files(payload.images)
                    log.info("小红书上传：chooser 已选择 %d 张", len(payload.images))
                    uploaded = True
                    break
                except Exception as exc:  # noqa: BLE001
                    log.warning("小红书点上传失败（第 %d 次）: %s", attempt + 1, exc)
                    await page.wait_for_timeout(2000)
        if not uploaded:
            return PublishResult(self.platform, False, "failed", "小红书图片上传失败")
        # 等图片上传/压缩完成：编辑器（标题输入框）出现即代表进入编辑态
        # （标题 input.d-text 比预览图更可靠；最多等 45 秒）
        title_input = page.locator("input.d-text[placeholder*='标题'], input.d-text").first
        content_editor = page.locator(".tiptap.ProseMirror, [contenteditable='true'].ProseMirror").first
        editor_ready = False
        for _ in range(45):
            try:
                if await title_input.is_visible() and await content_editor.is_visible():
                    editor_ready = True
                    break
            except Exception:  # noqa: BLE001
                pass
            await page.wait_for_timeout(1000)
        if not editor_ready:
            return PublishResult(self.platform, False, "failed", "小红书图片上传后编辑器未出现")
        await page.wait_for_timeout(800)

        # 填标题与正文（标题 input.d-text，正文 tiptap ProseMirror contenteditable）
        # 小红书标题硬上限 20 字——超出会直接发布失败，先截断再填
        title_ok = await self._fill_if_visible(page, title_input, fit_text(payload.title, 20))
        body = payload.content
        if payload.tags:
            tags_block = "\n".join(f"#{t}" for t in payload.tags)
            body = f"{body}\n\n{tags_block}" if body else tags_block
        content_ok = await self._fill_if_visible(page, content_editor, body)

        if not (title_ok and content_ok):
            return PublishResult(self.platform, False, "failed",
                                 f"小红书标题/正文填写失败 title={title_ok} content={content_ok}")

        await page.keyboard.press("Escape")
        await page.wait_for_timeout(1500)

        # 真发布：点页面固定底部的"发布笔记"按钮
        # （用户需求：不要只存草稿；如后续要草稿模式可加 payload.draft=True）
        published = await self._click_publish(page)
        return PublishResult(
            self.platform, published, "published" if published else "failed",
            "小红书图文已发布" if published else "小红书点击发布失败",
        )

    # ---------------- 内部实现 ----------------
    async def _ensure_image_tab(self, page: Page) -> bool:
        """确保当前在"上传图文"标签页。

        实测：导航 target=image 后站点可能停在"上传视频"（记住上次类型）。
        用顶部 div.creator-tab.active 的文本判定当前标签（最可靠），
        不是图文就 JS 点击"上传图文"标签。
        """
        from .helpers import click_first_usable

        ACTIVE_TAB_JS = """() => {
            const tabs = Array.from(document.querySelectorAll('div.creator-tab'));
            const active = tabs.find(t => (t.className||'').includes('active'));
            return active ? (active.textContent||'').replace(/\\s+/g,'') : '';
        }"""
        CLICK_TAB_JS = """() => {
            const tabs = Array.from(document.querySelectorAll('div.creator-tab'));
            const t = tabs.find(e => (e.textContent||'').replace(/\\s+/g,'') === '上传图文');
            if (t) { t.click(); return true; }
            return false;
        }"""
        deadline = asyncio.get_event_loop().time() + 25
        switched = False
        while asyncio.get_event_loop().time() < deadline:
            try:
                active = await page.evaluate(ACTIVE_TAB_JS)
            except Exception:  # noqa: BLE001
                active = ""
            if "图文" in active:
                log.info("小红书：已在'上传图文'页（本次切换=%s）", switched)
                return True
            if "视频" in active:
                try:
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(300)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    clicked = await page.evaluate(CLICK_TAB_JS)
                except Exception:  # noqa: BLE001
                    clicked = False
                if not clicked:
                    # 兜底：Playwright 真实点击
                    clicked = await click_first_usable(page, ["div.creator-tab:has-text('上传图文')"])
                log.info("小红书：当前为'上传视频'，点击'上传图文'标签（点击=%s）", clicked)
                switched = True
                await page.wait_for_timeout(3000)
            else:
                await page.wait_for_timeout(1000)  # 标签栏还没加载
        log.warning("小红书：25 秒内未能切换到'上传图文'页（选择器可能失效）")
        return False

    @staticmethod
    async def _fill_if_visible(page: Page, locator, value: str) -> bool:
        """元素可见才输入（拟人键入 + 随机停顿，降低脚本检测特征）。"""
        try:
            if not await locator.is_visible():
                return False
            await locator.click()
            await type_human(page, value)
            return True
        except Exception:  # noqa: BLE001
            try:
                await locator.fill(value)
                return True
            except Exception:  # noqa: BLE001
                return False

    async def _save_draft(self, page: Page) -> bool:
        """调用 Angular 内部 _onSave 存草稿。"""
        try:
            return await page.locator("xhs-publish-btn").evaluate(
                """(node) => {
                    if (typeof node._onSave !== 'function') return false;
                    const r = node._onSave();
                    if (r && typeof r.then === 'function') return r.then(() => true).catch(() => false);
                    return true;
                }"""
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("小红书存草稿失败: %s", exc)
            return False

    async def _click_publish(self, page: Page) -> bool:
        """点击页脚红色"发布"按钮。

        实测（2026-09 DOM）：发布按钮在 ``xhs-publish-btn`` 自定义元素的
        **closed shadow root** 中——JS/选择器无法访问内部节点
        （``elementFromPoint`` 只返回宿主，textContent/innerHTML 全空），
        但真实 CDP 鼠标事件可以点穿 shadow。
        方案：读宿主矩形，红色"发布"按钮中心在宿主义 fx≈0.604, fy≈0.489
        （白色"暂存离开"在 fx≈0.39，切勿点到）；submit-disabled=true 时等待。
        """
        import asyncio as _aio

        deadline = _aio.get_event_loop().time() + 30
        point = None
        while _aio.get_event_loop().time() < deadline:
            try:
                point = await page.evaluate(
                    """() => {
                        const host = document.querySelector('xhs-publish-btn');
                        if (!host) return null;
                        if (host.getAttribute('submit-disabled') === 'true' ||
                            host.getAttribute('submit-loading') === 'true') return null;
                        const r = host.getBoundingClientRect();
                        if (r.width < 100 || r.height < 20) return null;
                        return {x: Math.round(r.x + r.width * 0.604),
                                y: Math.round(r.y + r.height * 0.489)};
                    }"""
                )
            except Exception:  # noqa: BLE001
                point = None
            if point:
                break
            await page.wait_for_timeout(1000)
        if not point:
            log.warning("小红书发布：未找到可用的 xhs-publish-btn 发布按钮")
            return False
        log.info("小红书发布：真实点击 shadow 内发布按钮 @(%d,%d)", point["x"], point["y"])
        await page.mouse.move(point["x"], point["y"], steps=5)
        await page.wait_for_timeout(300)
        await page.mouse.click(point["x"], point["y"])
        await page.wait_for_timeout(6000)
        return True



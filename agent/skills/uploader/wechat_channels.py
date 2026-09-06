"""微信视频号适配器：登录态检测 + 视频发布。

流程来源：移植 matrix 的 tencent_uploader/main.py（Apache-2.0）：
  - 登录：由人工在「远程驾驶舱」完成（channels.weixin.qq.com 登录页扫码/确认），
    本模块只负责 check_login（Cookie 特征优先）检测登录态。
  - 发布：platform/post/create → div.upload-content 选文件 → 键盘输入标题+话题 →
        轮询"发表"按钮解除 disabled 判定上传完成 → 可选封面/短标题 → 点"发表"

注意：视频号仅支持视频发布；建议本机安装 Chrome 并以 channel=chrome 运行规避 H264 问题。
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import TYPE_CHECKING

from ...logger import log
from ...models import PostPayload, PublishResult
from .base import PlatformAdapter
from .helpers import page_text, safe_goto

if TYPE_CHECKING:
    from playwright.async_api import Page

PLATFORM_URL = "https://channels.weixin.qq.com/platform"
LOGIN_URL = "https://channels.weixin.qq.com/platform/login-for-iframe?dark_mode=true&host_type=1"
POST_CREATE_URL = f"{PLATFORM_URL}/post/create"
POST_LIST_URL = f"{PLATFORM_URL}/post/list"


class WechatChannelsAdapter(PlatformAdapter):
    """微信视频号适配器（视频发布）。"""

    platform = "wechat_channels"
    capabilities = {"image": False, "video": True}

    # ---------------- 登录态检测 ----------------
    async def check_login(self, profile: str | None = None) -> bool:
        """登录态检测：Cookie 特征优先（不依赖页面结构），页面判定兜底。

        Cookie 特征（实测）：未登录时 channels.weixin.qq.com 域仅 2 个匿名 cookie
        （sessionid/wxuin）；微信登录确认后会写入 uekey 等会话 cookie（数量显著增加）。
        旧实现用页面文本含"扫码"判断，但登录成功后的页面模板里也可能含该字样，
        导致永远误判为未登录——这是扫码成功却一直"未登录"的根因。
        """
        context, page = (await self._pool.get_page("wechat_channels", profile))
        # 1) Cookie 特征（首选，不导航页面）
        try:
            cookies = await context.cookies()
            names = {c["name"] for c in cookies}
            if "uekey" in names:
                log.info("视频号登录状态: 已登录（cookie: uekey）")
                return True
            wx_cookies = [c for c in cookies if "weixin" in c["domain"]]
            if len(wx_cookies) >= 6:
                log.info("视频号登录状态: 已登录（weixin 域 cookie %d 个）", len(wx_cookies))
                return True
        except Exception as exc:  # noqa: BLE001
            log.debug("cookie 检测异常: %s", exc)
        # 2) 页面判定兜底：跳转发布页，看是否被重定向到登录页
        await safe_goto(page, POST_CREATE_URL)
        if "login" in page.url:
            log.info("视频号登录状态: 未登录（被重定向到登录页）")
            return False
        has_publish_ui = await self._locator_visible(page, "div.form-btns button:has-text('发表')") or \
            await self._locator_visible(page, "div.upload-content")
        if has_publish_ui:
            log.info("视频号登录状态: 已登录（发布页特征）")
            return True
        log.info("视频号登录状态: 未登录")
        return False

    # ---------------- 视频发布 ----------------
    async def publish_video(self, payload: PostPayload, profile: str | None = None) -> PublishResult:
        page = (await self._pool.get_page("wechat_channels", profile))[1]
        if not await self.check_login():
            return PublishResult(self.platform, False, "login_required", "视频号未登录")
        if not payload.video:
            return PublishResult(self.platform, False, "failed", "视频号发布缺少视频文件")

        await safe_goto(page, POST_CREATE_URL)
        # 1) 上传视频文件
        upload_div = page.locator("div.upload-content")
        try:
            async with page.expect_file_chooser(timeout=15000) as fc_info:
                await upload_div.click()
            file_chooser = await fc_info.value
            await file_chooser.set_files(payload.video)
        except Exception as exc:  # noqa: BLE001
            return PublishResult(self.platform, False, "failed", f"视频号上传文件失败: {exc}")

        # 2) 填标题与话题（键盘输入）
        await self._fill_title_tags(page, payload.title, payload.tags)

        # 3) 等待上传完成（"发表"按钮解除禁用）；期间打进度日志、失败弹窗立即返回、
        #    超时附页面现场快照便于定位（而非一句干巴巴的“处理超时”）
        ok, detail = await self._wait_upload_done(page, timeout=300)
        if not ok:
            return PublishResult(self.platform, False, "failed",
                                 f"视频号视频处理超时或失败：{detail or '请回远程桌面查看当前页面状态'}")

        # 4) 可选：更换封面
        if payload.images:
            await self._set_cover(page, payload.images[0])

        # 5) 可选：定时发布
        if payload.scheduled_at:
            await self._set_schedule(page, payload.scheduled_at)

        # 6) 短标题（视频号限制 16 字）
        await self._fill_short_title(page, payload.title)

        # 7) 发布（_click_publish 会等跳转到作品列表页，跳转即发表成功）
        if await self._click_publish(page):
            log.info("视频号视频已发表（跳转作品列表）")
            return PublishResult(self.platform, True, "published", "视频号视频已发布")
        return PublishResult(self.platform, False, "failed",
                             "视频号点击发表后未跳转作品列表（请检查发表按钮状态/是否进入审核）")

    # ---------------- 内部实现 ----------------
    async def _fill_title_tags(self, page: Page, title: str, tags: list[str]) -> None:
        """点击标题输入区，键盘输入标题与 #话题。"""
        editor = page.locator("div.input-editor")
        try:
            await editor.click()
            await page.keyboard.type(title, delay=20)
            if tags:
                await page.keyboard.press("Enter")
                for tag in tags:
                    await page.keyboard.type(f"#{tag}", delay=20)
                    await page.keyboard.press("Space")
        except Exception as exc:  # noqa: BLE001
            log.warning("视频号标题输入异常: %s", exc)

    async def _wait_upload_done(self, page: Page, timeout: float = 300) -> tuple[bool, str]:
        """轮询"发表"按钮直到不再 disabled（表示上传/转码完成）。

        返回 (是否完成, 失败现场说明)：
        - 每 ~20s 输出一次进度 + 抓一段页面可见文本，超时后把“最后现场”带回给上层，
          不再让用户只看到一句干巴巴的“处理超时”；
        - 页面弹出失败/重试类提示时立即失败返回，不等满超时。
        """
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        next_log = loop.time() + 20
        last_snap = ""
        while loop.time() < deadline:
            # 失败/错误弹窗快速失败
            dlg_txt = await self._failed_dialog(page)
            if dlg_txt is not None:
                return False, f"页面提示：{dlg_txt}"
            btn_class = await self._safe_attr(page, 'div.form-btns button:has-text("发表")', "class")
            if btn_class is not None and "weui-desktop-btn_disabled" not in btn_class:
                await page.wait_for_timeout(2000)  # 等封面生成
                return True, ""
            now = loop.time()
            if now >= next_log:
                log.info("视频号上传/转码处理中…已等待 %.0fs", now - (deadline - timeout))
                try:
                    snap = (await page.locator("body").inner_text(timeout=3000)).strip()
                    last_snap = " ".join(snap.split())[:400]
                    log.info("视频号当前页面片段: %s", last_snap)
                except Exception:  # noqa: BLE001
                    pass
                next_log = now + 20
            await page.wait_for_timeout(2000)
        return False, (f"300s 后仍未见「发表」按钮解除禁用。页面最后状态："
                       f"{last_snap or '（无法抓取页面文本，可能弹层遮挡）'}")

    @staticmethod
    async def _failed_dialog(page: Page) -> str | None:
        """检测页面上的失败提示弹窗；无提示返回 None，有提示返回提示文本。"""
        try:
            dlg = page.locator(".weui-desktop-dialog, .el-message--error, .finder-error").first
            if await dlg.count() and await dlg.is_visible():
                txt = (await dlg.inner_text()).strip()
                if txt and any(k in txt for k in ("失败", "错误", "重试", "不能", "不支持", "请重新")):
                    log.warning("视频号上传失败提示: %s", txt[:120])
                    return txt[:120]
        except Exception:  # noqa: BLE001
            pass
        return None

    async def _set_cover(self, page: Page, cover_path: str) -> None:
        """更换封面：删除旧封面 → 上传新封面 → 确定/确认。"""
        try:
            btn = page.locator('div.finder-tag-wrap.btn:has-text("更换封面")')
            btn_class = await btn.get_attribute("class")
            if btn_class and "disabled" in btn_class:
                return
            await btn.click()
            wrap = page.locator("div.single-cover-uploader-wrap > div.wrap")
            await wrap.hover()
            if await page.locator(".del-wrap > .svg-icon").count():
                await page.locator(".del-wrap > .svg-icon").click()
            async with page.expect_file_chooser(timeout=15000) as fc_info:
                await wrap.click()
            await (await fc_info.value).set_files(cover_path)
            await page.wait_for_timeout(2000)
            await page.get_by_role("button", name="确定").click()
            await page.wait_for_timeout(1000)
            await page.get_by_role("button", name="确认").click()
        except Exception as exc:  # noqa: BLE001
            log.warning("视频号换封面失败（忽略）: %s", exc)

    async def _set_schedule(self, page: Page, scheduled_at: str) -> None:
        """设置定时发布（时间选择器）。失败仅记录，不中断。"""
        try:
            dt = datetime.fromisoformat(scheduled_at)
            label = page.locator("label").filter(has_text="定时").nth(1)
            await label.click()
            await page.click('input[placeholder="请选择发表时间"]')
            month_label = f"{dt.month:02d}月"
            current = await page.inner_text('span.weui-desktop-picker__panel__label:has-text("月")')
            if current.strip() != month_label:
                await page.click("button.weui-desktop-btn__icon__right")
            cells = await page.query_selector_all("table.weui-desktop-picker__table a")
            for cell in cells:
                cls = await cell.get_attribute("class") or ""
                if "weui-desktop-picker__disabled" in cls:
                    continue
                if (await cell.inner_text()).strip() == str(dt.day):
                    await cell.click()
                    break
            await page.click('input[placeholder="请选择时间"]')
            await page.keyboard.press("Control+A")
            await page.keyboard.type(str(dt.hour))
            await page.locator("div.input-editor").click()  # 令时间生效
        except Exception as exc:  # noqa: BLE001
            log.warning("视频号定时设置失败（忽略，将立即发布）: %s", exc)

    async def _fill_short_title(self, page: Page, title: str) -> None:
        """短标题（≤16 字）。"""
        try:
            el = (
                page.get_by_text("短标题", exact=True)
                .locator("..")
                .locator("xpath=following-sibling::div")
                .locator('span input[type="text"]')
            )
            if await el.count():
                short = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff《》“”:+?%°]", "", title)[:16]
                await el.fill(short)
        except Exception as exc:  # noqa: BLE001
            log.warning("视频号短标题填写失败（忽略）: %s", exc)

    async def _click_publish(self, page: Page) -> bool:
        """点击"发表"并等待跳转到作品列表页。"""
        try:
            btn = page.locator('div.form-btns button:has-text("发表")')
            if await btn.count():
                await btn.click()
            await page.wait_for_url(f"**{POST_LIST_URL}**", timeout=60000)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("视频号点击发表未跳转（可能已发布或失败）: %s", exc)
            return POST_LIST_URL in page.url

    # ---------------- 工具 ----------------
    @staticmethod
    async def _locator_visible(page: Page, selector: str) -> bool:
        try:
            loc = page.locator(selector).first
            return bool(await loc.count()) and await loc.is_visible()
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    async def _safe_attr(page: Page, selector: str, attr: str) -> str | None:
        try:
            loc = page.locator(selector).first
            if not await loc.count():
                return None
            return await loc.get_attribute(attr)
        except Exception:  # noqa: BLE001
            return None

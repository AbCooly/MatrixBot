"""抖音适配器：图文/视频发布（仅自己可见安全模式）+ 登录态检测。

流程来源：
  - 发布选择器/验证码弹窗：移植 social_media_pubulish_MCP 的 src/platforms/douyin.ts（已本地分析）

登录由人工在「远程驾驶舱」完成，本模块不再提供自动扫码/短信登录。

注意：选择器随平台改版可能失效，联调时用 HEADLESS=false 观察并按报错日志修正。
"""
from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

from ...logger import log
from ...models import PostPayload, PublishResult
from .base import PlatformAdapter
from .helpers import (
    click_first_usable,
    fill_first_visible,
    fit_text,
    has_any_visible,
    page_text,
    safe_goto,
    wait_for_any_visible,
)

if TYPE_CHECKING:
    from playwright.async_api import Page

# ---------------- URL ----------------
MANAGE_URL = "https://creator.douyin.com/creator-micro/content/manage"
IMAGE_POST_URL = (
    "https://creator.douyin.com/creator-micro/content/post/image?default-tab=3&enter_from=publish_page"
    "&media_type=image&type=new"
)
VIDEO_POST_URL = "https://creator.douyin.com/creator-micro/content/post/video?enter_from=publish_page"

# ---------------- 选择器 ----------------
_LOGIN_SIGNALS = [
    "text=/登录|扫码|验证码|手机号/",
    "input[placeholder*='手机号']",
    "input[placeholder*='验证码']",
]
_LOGGED_IN_SIGNALS = ["text=/高清发布|内容管理|作品管理|合集管理|互动管理|数据中心|变现中心/"]
_IMAGE_MODE = [
    "text=/图文/", "button:has-text('图文')", "[role='tab']:has-text('图文')",
    "text=/图片/", "button:has-text('图片')",
]
_TITLE_SELECTORS = [
    "input[placeholder*='标题']", "textarea[placeholder*='标题']",
    "[contenteditable='true'][placeholder*='标题']", "[contenteditable='true'][aria-label*='标题']",
]
_CONTENT_SELECTORS = [
    "textarea[placeholder*='简介']", "textarea[placeholder*='描述']", "textarea[placeholder*='正文']",
    "[contenteditable='true'][placeholder*='简介']", "[contenteditable='true'][placeholder*='描述']",
    # 抖音新版：正文是无 placeholder 的 contenteditable（真实环境实测）
    "div[contenteditable='true']", "[contenteditable='true']",
]
_METADATA_READY = [
    *_TITLE_SELECTORS, *_CONTENT_SELECTORS, "text=/作品描述|作品标题|填写作品标题|填写作品简介/"
]
_IMAGE_READY = ["text=/已添加\\d+张图片/", "text=/继续添加/", "text=/编辑图片/", "text=/预览图文/"]
_VIDEO_READY = ["text=/设置封面/", "text=/预览视频/", "text=/添加合集/", "text=/自主声明/"]
_UPLOAD_START = ["text=/点击上传|上传图文|上传视频|直接将视频文件拖入此区域/"]
_DISMISS = ["button:has-text('完成')", "button:has-text('我知道了')", "button:has-text('知道了')", "button:has-text('关闭')"]
_PRIVATE_VISIBILITY = ["label:has-text('仅自己可见')", "button:has-text('仅自己可见')", "text=/仅自己可见/"]
_PUBLISH_BTN = ["button:has-text('发布')", "button:has-text('立即发布')", "text=/发布$/"]
_VERIFY_MODAL = ["text=/接收短信验证码/", "text=/请输入验证码/", "button:has-text('获取验证码')", "button:has-text('验证')"]

_PROCESSING_PATTERN = re.compile(r"上传中|处理中|校验中|正在上传|正在处理|转码中|封面上传中|封面处理中")
_VIDEO_IN_PROGRESS = re.compile(r"文件解析中|上传过程中|上传中|处理中|转码中|\b\d{1,3}%\b")
_CODE_REQUESTED = re.compile(r"重新发送|秒后重发|秒后重新获取|\d+\s*秒")


class DouyinAdapter(PlatformAdapter):
    """抖音创作者平台适配器。"""

    platform = "douyin"
    capabilities = {"image": True, "video": True}

    # ---------------- 登录态检测 ----------------
    async def check_login(self, profile: str | None = None) -> bool:
        page = (await self._pool.get_page("douyin", profile))[1]
        await safe_goto(page, MANAGE_URL)
        text = await page_text(page)
        on_login_page = bool(re.search(r"passport|login|sso", page.url, re.I))
        has_login_ui = await has_any_visible(page, _LOGIN_SIGNALS) or bool(
            re.search(r"扫码登录|验证码登录|密码登录|登录/注册|请输入手机号", text)
        )
        has_logged_ui = await has_any_visible(page, _LOGGED_IN_SIGNALS)
        ok = (not on_login_page) and (not has_login_ui) and has_logged_ui
        log.info("抖音登录状态: %s", "已登录" if ok else "未登录")
        return ok

    # ---------------- 图文发布 ----------------
    async def publish_image(self, payload: PostPayload, profile: str | None = None) -> PublishResult:
        # 复用平台 tab（CDP 不反复开新 tab，用户可手动导航；发布前统一从创作者中心进入）
        page = (await self._pool.get_page("douyin", profile))[1]
        if not await self.check_login():
            return PublishResult(self.platform, False, "login_required", "抖音未登录")

        # 从创作者中心首页点"发布图文"进入（比直接跳发布 URL 更接近真实流程）
        await safe_goto(page, "https://creator.douyin.com/creator-micro/home")
        await page.wait_for_timeout(3000)
        from .helpers import click_first_usable as _cfu

        entered = await _cfu(page, ["text=/发布图文/"])
        if not entered:
            # 兜底：点侧栏"作品发布"展开后再选图文
            await _cfu(page, ["text=/作品发布/"])
            await page.wait_for_timeout(1500)
            await _cfu(page, ["text=/发布图文/", "text=/图文/"])
        await page.wait_for_timeout(4000)
        if "post/image" not in page.url and "post" not in page.url:
            log.warning("点击发布图文后未进入发布页（URL=%s），回退直接导航", page.url[:70])
            await safe_goto(page, IMAGE_POST_URL)
            await page.wait_for_timeout(3000)

        await wait_for_any_visible(page, _UPLOAD_START, timeout=20.0)

        uploaded = await self._set_files(page, payload.images, "image")
        # 上传失败（chooser 未触发/页面脏）→ 重载重试一次
        if not uploaded:
            log.warning("抖音上传失败，重载重试一次…")
            await safe_goto(page, page.url)
            await page.wait_for_timeout(4000)
            await wait_for_any_visible(page, _UPLOAD_START, timeout=20.0)
            uploaded = await self._set_files(page, payload.images, "image")
        upload_ready = await self._wait_image_ready(page, len(payload.images))
        await page.wait_for_timeout(1500)
        await self._dismiss_overlays(page)
        await has_any_visible(page, _METADATA_READY)  # 等待编辑区出现（不强制）
        meta = await self._fill_metadata(page, payload)

        if not uploaded or not upload_ready or not meta:
            return PublishResult(self.platform, False, "failed",
                                 f"抖音图文发布失败 uploaded={uploaded} uploadReady={upload_ready}")

        if payload.visibility != "public":
            await self._set_private(page)
        published = await self._click_publish(page)
        verification = await self._handle_verification(page) if published else False
        if verification:
            return PublishResult(self.platform, False, "verification_required",
                                 "抖音发布触发身份验证，请在远程驾驶舱完成验证后重试")
        if not published:
            return PublishResult(self.platform, False, "failed", "抖音图文点击发布失败")
        return PublishResult(self.platform, True, "published",
                             "抖音图文已发布" + ("（仅自己可见）" if payload.visibility != "public" else ""))

    # ---------------- 视频发布 ----------------
    async def publish_video(self, payload: PostPayload, profile: str | None = None) -> PublishResult:
        page = (await self._pool.get_page("douyin", profile))[1]
        if not await self.check_login():
            return PublishResult(self.platform, False, "login_required", "抖音未登录")
        if not payload.video:
            return PublishResult(self.platform, False, "failed", "缺少视频文件路径")

        await safe_goto(page, VIDEO_POST_URL)
        await page.wait_for_timeout(3000)
        await wait_for_any_visible(page, _UPLOAD_START, timeout=15.0)
        uploaded = await self._set_files(page, [payload.video], "video")
        upload_ready = await self._wait_video_ready(page, timeout=240)
        await page.wait_for_timeout(2000)
        await self._dismiss_overlays(page)
        meta = await self._fill_metadata(page, payload)

        if not uploaded or not upload_ready or not meta:
            return PublishResult(self.platform, False, "failed",
                                 f"抖音视频发布失败 uploaded={uploaded} uploadReady={upload_ready}")

        if payload.visibility != "public":
            await self._set_private(page)
        published = await self._click_publish(page)
        verification = await self._handle_verification(page) if published else False
        if verification:
            return PublishResult(self.platform, False, "verification_required",
                                 "抖音发布触发身份验证，请在远程驾驶舱完成验证后重试")
        if not published:
            return PublishResult(self.platform, False, "failed", "抖音视频点击发布失败")
        return PublishResult(self.platform, True, "published",
                             "抖音视频已发布" + ("（仅自己可见）" if payload.visibility != "public" else ""))

    # ---------------- 内部实现 ----------------
    async def _clear_uploaded(self, page: Page) -> None:
        """抖音发布页会恢复上次未发布的草稿——若上传区有残留先清空。"""
        try:
            from .helpers import has_any_visible as _hav

            clear_btn = page.locator("button:has-text('清空并重新上传')").first
            if await clear_btn.count() and await clear_btn.is_visible():
                await clear_btn.click()
                await page.wait_for_timeout(1500)
                # 处理可能的确认弹窗（确定/清空/确认）
                # 确认弹窗文案实测："重新上传将清空已上传图片，是否重新上传？" → 点精确"重新上传"
                # （不能用子串匹配——会误点右上角"清空并重新上传"再触发一次清空）
                await click_first_usable(page, [
                    "text=/^重新上传$/", "button:has-text('重新上传'):not(:has-text('清空'))",
                ])
                await page.wait_for_timeout(2000)
                log.info("抖音上传：已清空草稿残留")
        except Exception:  # noqa: BLE001
            pass

    async def _set_files(self, page: Page, files: list[str], mode: str) -> bool:
        """上传图片/视频文件。

        抖音新版发布页（真实环境实测 2026-09）没有静态 file input——
        先尝试静态 input，找不到则点击"点击上传"入口用 expect_file_chooser 捕获文件选择。
        """
        inputs = page.locator("input[type='file']")
        count = await inputs.count()
        if count > 0:
            descriptors = []
            for i in range(count):
                accept = await inputs.nth(i).get_attribute("accept") or ""
                descriptors.append(accept.lower())
            idx = 0
            if mode == "video":
                idx = next((i for i, a in enumerate(descriptors) if "video/" in a or ".mp4" in a), 0)
            else:
                img_idx = next((i for i, a in enumerate(descriptors)
                                if "image/" in a or ".png" in a or ".jpg" in a), -1)
                idx = img_idx if img_idx >= 0 else (1 if len(descriptors) > 1 else 0)
            await inputs.nth(idx).set_input_files(files)
            return True
        # 新版：点击上传入口 → 捕获文件选择
        # 入口随状态变化：无图="点击上传/选择一张图片作为封面"，有残留="继续添加"
        targets = [
            "text=/继续添加/",           # 草稿恢复有图：追加图片（真实环境主要入口）
            "button:has-text('继续添加')",
            "text=/点击上传/",           # 干净上传区
            "text=/选择一张图片作为封面/",
            "text=/上传图片/",
            "[class*='upload'] [class*='btn']",
        ]
        try:
            from .helpers import wait_for_any_visible as _wav

            await _wav(page, ["text=/继续添加/", "text=/点击上传/",
                              "text=/选择一张图片作为封面/"], timeout=20.0)
            async with page.expect_file_chooser(timeout=30000) as fc_info:
                clicked = await click_first_usable(page, targets)
                log.info("抖音上传：点击上传入口（可点=%s）", clicked)
            chooser = await fc_info.value
            await chooser.set_files(files)
            log.info("抖音上传：已选择文件 %s", files[0].rsplit("/", 1)[-1])
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("抖音点击上传失败（mode=%s）: %s", mode, exc)
            return False

    async def _wait_image_ready(self, page: Page, expected: int, timeout: float = 180) -> bool:
        """等待图文上传完成（页面出现"已添加N张图片"且无处理中文案）。"""
        deadline = asyncio.get_event_loop().time() + timeout
        expected_text = f"已添加{expected}张图片"
        while asyncio.get_event_loop().time() < deadline:
            text = await page_text(page)
            ready = (expected_text in text or await has_any_visible(page, _IMAGE_READY)) and not _PROCESSING_PATTERN.search(text)
            if ready:
                return True
            await page.wait_for_timeout(1000)
        return False

    async def _wait_video_ready(self, page: Page, timeout: float = 240) -> bool:
        """等待视频处理完成。

        真实环境信号（2026-09 实测）：上传中"发布"按钮 disabled、有"上传中/xx%"文案；
        处理完成 → "发布/立即发布"按钮可点 + 出现视频预览/设置封面。
        """
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            text = await page_text(page)
            # 上传中：以"上传中/处理中/转码中"关键词为准（百分比会误报——页面常残留历史进度）
            still = bool(re.search(r"上传中|处理中|转码中|文件解析中|正在上传", text[:12000]))
            # 信号1：页面出现"设置封面/预览视频"等完成文案
            ready_by_ui = await has_any_visible(page, _VIDEO_READY)
            # 信号2（兜底）：底部"发布"按钮可见且未 disabled（上传完成的强信号）
            publish_enabled = False
            try:
                publish_enabled = await page.evaluate(
                    """() => {
                        const nodes = Array.from(document.querySelectorAll('button'));
                        const btn = nodes.filter(n => {
                            const t = (n.textContent||'').replace(/\\s+/g,' ').trim();
                            if (t !== '发布' && t !== '立即发布') return false;
                            const r = n.getBoundingClientRect();
                            return r.width > 40 && getComputedStyle(n).display !== 'none';
                        }).sort((a,b) => b.getBoundingClientRect().bottom - a.getBoundingClientRect().bottom)[0];
                        if (!btn) return false;
                        return !btn.disabled && !(btn.className||'').includes('disabled');
                    }"""
                )
            except Exception:  # noqa: BLE001
                publish_enabled = False
            if (ready_by_ui or publish_enabled) and not still:
                return True
            await page.wait_for_timeout(1500)
        return False

    async def _dismiss_overlays(self, page: Page) -> None:
        """点掉"我知道了/完成"等引导浮层。"""
        for _ in range(3):
            if not await click_first_usable(page, _DISMISS):
                return
            await page.wait_for_timeout(500)

    async def _fill_metadata(self, page: Page, payload: PostPayload) -> bool:
        """填标题、正文（含话题）。抖音标题上限 30 字，先截断再填。"""
        title_ok = await fill_first_visible(page, _TITLE_SELECTORS, fit_text(payload.title, 30))
        caption = payload.content
        if payload.tags:
            tags_line = " ".join(f"#{t}" for t in payload.tags)
            caption = f"{caption}\n\n{tags_line}" if caption else tags_line
        content_ok = await fill_first_visible(page, _CONTENT_SELECTORS, caption)
        if not (title_ok and content_ok):
            log.warning("抖音元数据填写不完整 title=%s content=%s", title_ok, content_ok)
        return title_ok and content_ok

    async def _set_private(self, page: Page) -> bool:
        """勾选"仅自己可见"（安全模式）。"""
        await page.keyboard.press("Escape")
        await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(1000)
        if await click_first_usable(page, _PRIVATE_VISIBILITY):
            await page.wait_for_timeout(500)
            return True
        # 兜底：按文本找节点点击
        return await page.evaluate(
            """() => {
                const nodes = Array.from(document.querySelectorAll('label, button, div, span'));
                const t = nodes.find(n => (n.textContent||'').replace(/\\s+/g,' ').includes('仅自己可见'));
                if (!t) return false;
                t.click(); return true;
            }"""
        )

    async def _click_publish(self, page: Page) -> bool:
        """点击页面最底部的"发布"按钮。

        发布按钮位于页面最底部（可能在内部滚动容器）——先 scrollIntoView 彻底滚动，
        再真实鼠标点击按钮中心；点后轮询验证发布结果（跳转/成功提示/处理弹窗）。
        """
        clicked = await self._click_bottom_publish(page)
        if not clicked:
            clicked = await click_first_usable(page, _PUBLISH_BTN)
        if not clicked:
            log.warning("抖音发布按钮点击失败（未找到可用按钮）")
            return False
        # 点发布后验证：跳转/成功提示 = 成功；弹窗则自动处理
        return await self._verify_published(page)

    async def _click_bottom_publish(self, page: Page) -> bool:
        """scrollIntoView 滚动到最底部"发布"按钮并真实鼠标点击。"""
        try:
            point = await page.evaluate(
                """() => {
                    const nodes = Array.from(document.querySelectorAll(
                        'button, [role="button"], div[role="button"]'));
                    const targets = nodes.filter(n => {
                        const t = (n.textContent||'').replace(/\\s+/g,' ').trim();
                        if (t !== '发布' && t !== '立即发布') return false;
                        const s = getComputedStyle(n), r = n.getBoundingClientRect();
                        if (s.visibility === 'hidden' || s.display === 'none' ||
                            r.width < 40 || r.height < 20) return false;
                        if (n.disabled || (n.className||'').includes('disabled')) return false;
                        return true;
                    }).sort((a,b) =>
                        b.getBoundingClientRect().bottom - a.getBoundingClientRect().bottom);
                    const target = targets[0];
                    if (!target) return null;
                    target.scrollIntoView({block:'center', behavior:'instant'});
                    return new Promise(res => setTimeout(() => {
                        const r = target.getBoundingClientRect();
                        if (r.width === 0) { res(null); return; }
                        res({ x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2),
                              text: (target.textContent||'').trim() });
                    }, 800));
                }"""
            )
            if not point:
                return False
            log.info("抖音发布：真实点击按钮 %s @(%d,%d)", point["text"], point["x"], point["y"])
            await page.mouse.move(point["x"], point["y"], steps=5)
            await page.wait_for_timeout(300)
            await page.mouse.click(point["x"], point["y"])
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("抖音发布按钮真实点击失败: %s", exc)
            return False

    async def _verify_published(self, page: Page) -> bool:
        """点发布后轮询验证是否成功（处理确认弹窗，最长 90 秒）。"""
        deadline = asyncio.get_event_loop().time() + 90
        while asyncio.get_event_loop().time() < deadline:
            # 处理确认弹窗（"确认发布/知道了/完成"等）
            try:
                handled = await click_first_usable(page, [
                    "button:has-text('确认发布')", "button:has-text('确认')",
                    "text=/确认发布/", "button:has-text('知道了')",
                    "button:has-text('完成')", "button:has-text('确定')",
                ])
                if handled:
                    await page.wait_for_timeout(1000)
            except Exception:  # noqa: BLE001
                pass
            text = await page_text(page)
            if "content/manage" in page.url or "creator-micro/home" in page.url:
                log.info("抖音发布：页面已跳转（发布成功）")
                return True
            if any(k in text for k in ("发布成功", "作品已发布", "发布完成")):
                log.info("抖音发布：出现成功提示")
                return True
            await page.wait_for_timeout(1500)
        log.warning("抖音发布：90 秒内未确认成功（可能停在发布页或仍需人工确认）")
        return False

    async def _handle_verification(self, page: Page) -> bool:
        """点击发布后，若弹短信验证码则点"获取验证码"并返回 True。"""
        if not await has_any_visible(page, _VERIFY_MODAL):
            return False
        await click_first_usable(page, ["text=/获取验证码/"])
        await page.wait_for_timeout(800)
        text = await page_text(page)
        return bool(_CODE_REQUESTED.search(text))

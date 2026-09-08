"""小红书适配器：登录检测 + 图文发布（默认存草稿，安全优先）。

流程来源：移植 social_media_pubulish_MCP 的 src/platforms/xiaohongshu.ts（已本地分析）：
  - 创作者平台 creator.xiaohongshu.com
  - 上传后填标题/正文/话题，通过 xhs-publish-btn._onSave() 存草稿（Angular 内部方法）
"""
from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING

from ...logger import log
from ...models import PostPayload, PublishResult
from . import humanizer as hz
from .base import PlatformAdapter
from .helpers import (
    click_first_usable_human,
    dismiss_popups,
    fit_text,
    has_any_visible,
    safe_goto,
    type_human,
)

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

        # ---------- 拟人链路：不“直奔发布页” ----------
        # 会话刚打开就到 /publish 直达页是典型脚本特征；
        # 先制造“人类到场感”：首页游走/滚动/悬停 → UI 入口点进发布页
        # （若上一会话已停在发布页，例如人工草稿未完，则直接续写）。
        if "/publish" not in (page.url or "").lower():
            await self._warmup_home(page)
            entered = await self._open_publish_ui(page)
            if not entered:
                log.info("小红书：UI 入口 12s 内未达发布页，回退直达 URL（保底）")
                await safe_goto(page, PUBLISH_IMAGE_URL)
        await asyncio.sleep(hz.think_ms(0.8, 2.2) / 1000.0)

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
                        # 拟人点击“上传图片/上传图文”入口打开系统文件选择框
                        clicked = await click_first_usable_human(page, [
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

        # 发布前“检查一遍再点”：滚动检查 → 光标回到按钮附近 → 思考停顿
        try:
            await page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(hz.think_ms(0.6, 1.8) / 1000.0)
        try:
            await hz.scroll_human(page, random.randint(-120, -60))
        except Exception:  # noqa: BLE001
            pass

        # 真发布：点页面固定底部的"发布笔记"按钮
        # （用户需求：不要只存草稿；如后续要草稿模式可加 payload.draft=True）
        published = await self._click_publish(page)
        return PublishResult(
            self.platform, published, "published" if published else "failed",
            "小红书图文已发布" if published else "小红书点击发布失败",
        )

    # ---------------- 拟人预热 / 发布页导航 ----------------
    async def _warmup_home(self, page: Page) -> None:
        """发布前在首页制造“人类到场感”：滚动浏览 + 光标游走悬停 + 思考停顿。

        总耗时约 4~12s（随机），仅做视觉与光标动作，不点击任何内容。
        """
        try:
            await hz.idle_wander(page, seconds=hz.think_ms(0.8, 2.0) / 1000.0)
        except Exception:  # noqa: BLE001
            pass
        for _ in range(random.randint(1, 2)):
            try:
                await hz.scroll_human(page, random.randint(160, 480))
                await asyncio.sleep(hz.think_ms(0.8, 2.4) / 1000.0)
            except Exception:  # noqa: BLE001
                pass
        try:
            await hz.wander(page, seconds=random.uniform(2.5, 6.0))
        except Exception:  # noqa: BLE001
            pass
        try:
            await dismiss_popups(page, max_rounds=2)
        except Exception:  # noqa: BLE001
            pass

    # 在首页找「发布笔记」入口（文本命中 + 命中测试保证可点），返回其中心坐标
    _PUBLISH_ENTRY_JS = """(txt) => {
      try {
        const pick = (el) => {
          const r = el.getBoundingClientRect();
          if (r.width < 8 || r.height < 8) return null;
          if (r.bottom < 0 || r.right < 0 || r.top > (window.innerHeight || 900)) return null;
          const cx = r.x + r.width / 2, cy = r.y + r.height / 2;
          const top = document.elementFromPoint(cx, cy);
          if (top && (el === top || el.contains(top))) return {x: cx, y: cy};
          return null;
        };
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        while (walker.nextNode()) {
          const node = walker.currentNode;
          const t = (node.textContent || '').replace(/\\s+/g, '');
          if (!t || !t.includes(txt)) continue;
          let el = node.parentElement;
          for (let d = 0; el && el !== document.body && d < 6; el = el.parentElement, d++) {
            const p = pick(el);
            if (p) return p;
          }
        }
      } catch (e) {}
      return null;
    }"""

    async def _await_publish_target(self, page: Page, timeout: float = 12.0) -> bool:
        """轮询判断页面已进入发布/上传编辑器状态。"""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                url = page.url or ""
                if "publish" in url:
                    return True
                probes = [
                    page.locator("xhs-publish-btn").count(),
                    page.locator(".creator-tab").count(),
                    page.locator("input[type='file']").count(),
                ]
                if any(await asyncio.gather(*probes)):
                    return True
            except Exception:  # noqa: BLE001
                pass
            await page.wait_for_timeout(500)
        return False

    async def _open_publish_ui(self, page: Page) -> bool:
        """从首页 UI 入口点进发布页（拟人点击，不直达 URL）。

        返回 False 表示 12s 内未确认进入发布页（由调用方回退直达 URL）。
        """
        try:
            point = await page.evaluate(self._PUBLISH_ENTRY_JS, "发布笔记")
            if point:
                await hz.click_point(page, float(point["x"]), float(point["y"]),
                                     label="首页·发布笔记入口")
                ok = await self._await_publish_target(page, timeout=12.0)
                log.info("小红书：UI 入口进入发布页=%s", ok)
                return ok
        except Exception as exc:  # noqa: BLE001
            log.warning("小红书：查找/点击发布笔记入口异常: %s", exc)
        return False

    # ---------------- 内部实现 ----------------
    async def _ensure_image_tab(self, page: Page) -> bool:
        """确保当前在"上传图文"标签页。

        实测：导航 target=image 后站点可能停在"上传视频"（记住上次类型）。
        用顶部 div.creator-tab.active 的文本判定当前标签（最可靠），
        不是图文就 JS 点击"上传图文"标签。
        """
        from .helpers import click_first_usable_human

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
                    # 兜底：拟人真实点击“上传图文”标签
                    clicked = await click_first_usable_human(
                        page, ["div.creator-tab:has-text('上传图文')"]
                    )
                log.info("小红书：当前为'上传视频'，点击'上传图文'标签（点击=%s）", clicked)
                switched = True
                await page.wait_for_timeout(3000)
            else:
                await page.wait_for_timeout(1000)  # 标签栏还没加载
        log.warning("小红书：25 秒内未能切换到'上传图文'页（选择器可能失效）")
        return False

    async def _fill_if_visible(self, page: Page, locator, value: str) -> bool:
        """元素可见才输入：拟人点击进入 + 拟人键入（脉冲串+思考停顿）。

        点击先走 humanizer（弯轨移动/悬停/持键），失败再退回 locator.click()，
        保证输入框拿到真实焦点且不暴露“直线瞬移点击”特征。
        """
        try:
            if not await locator.is_visible():
                return False
            if not await hz.click_locator_human(page, locator, label="输入框"):
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
        await hz.click_point(page, float(point["x"]), float(point["y"]),
                             label="xhs-publish 发布按钮(shadow)")
        # 提交后等待服务端处理结果（随机化等待，不固定 6s）
        await asyncio.sleep(hz.think_ms(3.0, 7.0) / 1000.0)
        return True



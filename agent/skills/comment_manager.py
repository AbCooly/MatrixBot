"""Comment_Manager 技能：评论互动（读取最新评论 → DeepSeek 生成回复 → 自动回复）。

实现策略：
  - 评论获取：Playwright 打开作品页滚动加载，按平台候选选择器抓取评论节点文本
    （不重造 MediaCrawler 的接口签名算法；其许可证为非商用，仅借鉴数据模型）
  - 自动回复：命中评论 → LLM 生成拟人回复 → 点击"回复"输入提交
  - 幂等：已回复的评论 ID 持久化到 state/replied.json，绝不重复回复

注意：平台 DOM 结构改版会导致选择器失效，请按日志提示调整 _SELECTORS。
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from ..config import Settings, llm_ready
from ..llm_client import DeepSeekClient
from ..logger import log
from ..models import Comment, ReplyResult, SkillResult
from .base import Skill
from .uploader.browser import BrowserPool

# 每平台：评论节点候选选择器（第一个命中的为准）
_SELECTORS: dict[str, list[str]] = {
    "xiaohongshu": ["[class*='comment-item']", "[class*='commentItem']", ".note-comment .item"],
    "douyin": [".comment-item", "[class*='commentItem']", "[class*='comment-list'] [class*='item']"],
}

# 触发"回复"按钮的候选文本/选择器
_REPLY_TRIGGERS = ["text=/回复/", "[class*='reply']", "[class*='Reply']"]
# 回复输入框候选
_REPLY_INPUTS = ["textarea", "div[contenteditable='true']", "input[type='text']"]
# 提交回复的候选
_REPLY_SUBMIT = ["button:has-text('发送')", "button:has-text('回复')", "[class*='submit']"]

# 回复内容黑名单（命中则不回复，避免违规）
_BLACKLIST = re.compile(r"加微信|加vx|q群|代购|刷单|兼职|赚钱|赌博|色情|私我")


class CommentManager(Skill):
    """评论获取 + 自动回复。"""

    name = "comment_manager"
    description = "读取作品最新评论并用 AI 自动回复"

    def __init__(self, settings: Settings, pool: BrowserPool):
        self._settings = settings
        self._pool = pool
        self._llm = DeepSeekClient(settings)
        self._replied_path: Path = settings.state_dir / "replied.json"
        self._lock = asyncio.Lock()
        self._load_replied()

    # ---------------- 状态持久化 ----------------
    def _load_replied(self) -> None:
        """加载已回复记录 {platform: {note_url: {comment_id: reply_text}}}。"""
        if self._replied_path.exists():
            try:
                self._replied: dict = json.loads(self._replied_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 —— 损坏则重置
                log.warning("replied.json 解析失败，重置为空")
                self._replied = {}
        else:
            self._replied = {}

    async def _save_replied(self) -> None:
        async with self._lock:
            self._replied_path.parent.mkdir(parents=True, exist_ok=True)
            self._replied_path.write_text(
                json.dumps(self._replied, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    def _mark_replied(self, comment: Comment, reply_text: str) -> None:
        self._replied.setdefault(comment.platform, {}).setdefault(comment.note_url, {})[
            comment.comment_id
        ] = reply_text

    # ---------------- 统一入口 ----------------
    async def run(self, action: str = "auto_reply", **kwargs) -> SkillResult:
        try:
            if action == "fetch":
                comments = await self.fetch_comments(
                    kwargs["platform"], kwargs["note_url"], kwargs.get("limit", 10)
                )
                return SkillResult(success=True, data=comments, meta={"count": len(comments)})
            if action == "auto_reply":
                results = await self.auto_reply(
                    platform=kwargs.get("platform", ""),
                    note_url=kwargs.get("note_url", ""),
                    max_replies=int(kwargs.get("max_replies") or 10),
                    topic=str(kwargs.get("topic") or ""),
                    persona=str(kwargs.get("persona") or ""),
                    tone=str(kwargs.get("tone") or ""),
                    materials=kwargs.get("materials") or None,
                    taboos=kwargs.get("taboos") or None,
                )
                return SkillResult(success=True, data=results, meta={"count": len(results)})
            return SkillResult(success=False, error=f"未知 action: {action}")
        except Exception as exc:  # noqa: BLE001
            log.exception("[comment_manager] 执行异常")
            return SkillResult(success=False, error=f"{type(exc).__name__}: {exc}")

    # ---------------- 评论获取 ----------------
    async def fetch_comments(self, platform: str, note_url: str, limit: int = 10) -> list[Comment]:
        """打开作品页，滚动加载并抓取评论。

        任何页面异常（风控页/加载失败/JS 错误）都返回空列表而非抛异常，
        由上层决定是否继续（不会让整个流量维护任务崩溃）。
        """
        if platform not in _SELECTORS:
            raise ValueError(f"暂不支持平台: {platform}")
        page = (await self._pool.get_page(platform))[1]
        try:
            await page.goto(note_url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] 评论页打开失败: %s", platform, exc)
            return []

        # 滚动加载评论（最多 8 轮）
        for _ in range(8):
            try:
                items = await self._collect_nodes(page, platform)
            except Exception as exc:  # noqa: BLE001
                log.warning("[%s] 评论采集失败（页面可能被风控拦截）: %s", platform, exc)
                return []
            if len(items) >= limit:
                break
            try:
                await page.evaluate("() => window.scrollBy(0, window.innerHeight)")
            except Exception:  # noqa: BLE001
                pass
            await page.wait_for_timeout(1200)

        try:
            items = await self._collect_nodes(page, platform)
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] 评论采集失败: %s", platform, exc)
            return []
        comments = [
            Comment(
                platform=platform,
                comment_id=item.get("id") or f"idx-{i}",
                user_name=item.get("user") or "匿名用户",
                content=item.get("content") or "",
                like_count=int(item.get("likes") or 0),
                note_url=note_url,
            )
            for i, item in enumerate(items[:limit])
            if (item.get("content") or "").strip()
        ]
        log.info("[%s] 抓取到 %d 条评论", platform, len(comments))
        return comments

    async def _collect_nodes(self, page, platform: str) -> list[dict]:
        """在浏览器内按候选选择器抓取评论节点（返回 dict 列表）。"""
        selectors = _SELECTORS[platform]
        return await page.evaluate(
            """(selectors) => {
                const pick = (el, pats) => {
                    for (const p of pats) {
                        const m = el.querySelector(p);
                        if (m && m.textContent) return m.textContent.replace(/\\s+/g,' ').trim();
                    }
                    return '';
                };
                const out = [];
                for (const sel of selectors) {
                    const nodes = Array.from(document.querySelectorAll(sel));
                    if (!nodes.length) continue;
                    for (const node of nodes) {
                        const text = (node.textContent || '').replace(/\\s+/g,' ').trim();
                        if (!text) continue;
                        // 从文本里粗分 用户名/内容：首行视为用户名，其余视为内容
                        const lines = text.split(/\\n/).map(s => s.trim()).filter(Boolean);
                        const first = lines[0] || '';
                        const rest = lines.slice(1).join(' ') || first;
                        out.push({
                            id: node.getAttribute('data-id') || node.getAttribute('id') || '',
                            user: first,
                            content: rest,
                            likes: 0
                        });
                    }
                    if (out.length) break;  // 命中第一组选择器即可
                }
                return out;
            }""",
            selectors,
        )

    # ---------------- 回复生成 ----------------
    async def generate_reply(
        self,
        comment: Comment,
        topic: str = "",
        persona: str = "",
        tone: str = "",
        materials: list | None = None,
        taboos: list | None = None,
    ) -> str:
        """按账号定制人设生成拟人回复；无 Key 时用模板兜底。

        persona/tone/materials/taboos 来自该任务的定制内容（AI 工坊定制包 /
        任务调度的「定制」配置），让每条回复都贴合账号人设而不是千篇一律。
        """
        if llm_ready(self._settings):
            lines = [
                "你是该账号的主理人，正在后台逐条回复粉丝评论。",
                "要求：中文、口语化、有温度、不机械；不要用'感谢支持''谢谢宝子'这类套话开头；"
                "长度控制在 10-40 字；不得包含任何联系方式/导流/硬广，不得承诺效果。",
            ]
            if persona or tone:
                if persona:
                    lines.append(f"账号人设：{persona}")
                if tone:
                    lines.append(f"账号统一口吻：{tone}")
            if taboos:
                taboos = [str(t).strip() for t in taboos if str(t).strip()]
                if taboos:
                    lines.append(f"内容红线（绝不能触碰）：{'；'.join(taboos)}")
            mat_lines = []
            for m in materials or []:
                if isinstance(m, dict):
                    name = str(m.get("name") or "").strip()
                    detail = str(m.get("detail") or "").strip()
                    if name or detail:
                        mat_lines.append(f"- {name}{'：' + detail if detail else ''}")
                elif isinstance(m, str) and m.strip():
                    mat_lines.append(f"- {m.strip()}")
            if mat_lines:
                lines.append(
                    "你的自有素材（仅当评论确实相关时自然带出一点，不要硬塞）：\n" + "\n".join(mat_lines)
                )
            lines.append("只输出回复内容本身，不要引号、不要任何解释。")
            system = "\n".join(lines)
            user = (
                f"作品主题：{topic or '未指定'}\n粉丝评论：{comment.content}\n（评论者：{comment.user_name}）"
            )
            try:
                reply = self._llm.chat(system, user, temperature=0.8, max_tokens=200)
                if reply:
                    return reply[:80]
            except Exception as exc:  # noqa: BLE001
                log.warning("回复生成失败，使用模板: %s", exc)
        # 兜底模板
        return f"谢谢你的分享呀，有同感！欢迎常来聊聊～"

    # ---------------- 自动回复 ----------------
    async def auto_reply(
        self,
        platform: str,
        note_url: str,
        max_replies: int = 10,
        topic: str = "",
        persona: str = "",
        tone: str = "",
        materials: list | None = None,
        taboos: list | None = None,
    ) -> list[ReplyResult]:
        """获取评论 → 过滤去重 → 按账号人设生成回复 → 自动提交。"""
        comments = await self.fetch_comments(platform, note_url, limit=max_replies + 5)
        results: list[ReplyResult] = []
        replied_map = self._replied.get(platform, {}).get(note_url, {})

        done = 0
        for comment in comments:
            if done >= max_replies:
                break
            # 跳过已回复 / 黑名单内容 / 空内容
            if comment.comment_id in replied_map or _BLACKLIST.search(comment.content):
                continue
            reply_text = await self.generate_reply(
                comment,
                topic=topic or note_url,
                persona=persona,
                tone=tone,
                materials=materials,
                taboos=taboos,
            )
            result = await self._do_reply(platform, note_url, comment, reply_text)
            if result.success:
                self._mark_replied(comment, reply_text)
                done += 1
            results.append(result)
            await page_breath()
        await self._save_replied()
        log.info("[%s] 自动回复完成：成功 %d/%d", platform, done, len(results))
        return results

    async def _do_reply(
        self, platform: str, note_url: str, comment: Comment, text: str
    ) -> ReplyResult:
        """在页面上定位评论并提交回复。"""
        page = (await self._pool.get_page(platform))[1]
        try:
            await page.goto(note_url, wait_until="domcontentloaded", timeout=30_000)
            # 滚动到评论区
            await page.evaluate("() => window.scrollBy(0, window.innerHeight)")
            await page.wait_for_timeout(1000)

            selectors = _SELECTORS[platform]
            items = page.locator(selectors[0])
            count = await items.count()
            if count == 0:
                return ReplyResult(comment, error="未找到评论节点")
            # 按 id 匹配（尽力而为）；失败则用第一条
            target = None
            if comment.comment_id and not comment.comment_id.startswith("idx-"):
                for i in range(count):
                    cid = await items.nth(i).get_attribute("data-id") or ""
                    if cid == comment.comment_id:
                        target = items.nth(i)
                        break
            target = target or items.first

            # 点击回复触发器
            from .uploader.helpers import click_first_usable

            clicked = await self._click_reply_trigger(target)
            if not clicked:
                return ReplyResult(comment, error="未找到回复按钮")

            await page.wait_for_timeout(800)
            filled = await self._fill_reply_input(page, text)
            if not filled:
                return ReplyResult(comment, error="未找到回复输入框")

            await page.wait_for_timeout(500)
            submitted = await click_first_usable(page, _REPLY_SUBMIT)
            if not submitted:
                # 兜底：回车提交（部分平台回复框支持）
                await page.keyboard.press("Enter")
                submitted = True
            await page.wait_for_timeout(1500)
            return ReplyResult(comment, reply_text=text, success=submitted)
        except Exception as exc:  # noqa: BLE001
            log.warning("回复提交异常: %s", exc)
            return ReplyResult(comment, error=str(exc))

    @staticmethod
    async def _click_reply_trigger(target) -> bool:
        """在评论节点内点击"回复"（候选选择器 + 文本兜底）。"""
        for selector in _REPLY_TRIGGERS:
            try:
                loc = target.locator(selector).first
                if await loc.count() and await loc.is_visible():
                    await loc.click()
                    return True
            except Exception:  # noqa: BLE001
                continue
        # 文本兜底：在节点内找包含"回复"的元素
        try:
            return await target.evaluate(
                """(node) => {
                    const els = Array.from(node.querySelectorAll('*'));
                    const t = els.find(e => (e.textContent||'').trim() === '回复' ||
                        (e.textContent||'').trim() === '回复TA');
                    if (!t) return false;
                    t.click(); return true;
                }"""
            )
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    async def _fill_reply_input(page, text: str) -> bool:
        """填回复输入框（候选选择器逐个尝试）。"""
        for selector in _REPLY_INPUTS:
            try:
                loc = page.locator(selector).last  # 回复框通常是最后一个
                if await loc.count() and await loc.is_visible():
                    await loc.fill(text)
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False


async def page_breath(seconds: float = 1.5) -> None:
    """回复间隔，避免动作过快触发风控。"""
    await asyncio.sleep(seconds)

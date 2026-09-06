"""Content_Generator 技能：根据热点 + 平台风格生成文案，并支持 AI 自我审核择优。

流程：
  1. generate()：DeepSeek 按平台 System Prompt 一次性产出 count 份结构化草稿（JSON）
  2. review_and_pick()：DeepSeek 对每份草稿打分(0-10)并给出理由，返回最高分草稿
  3. 无 API Key / LLM 失败时：降级启发式评分（标题长度、正文长度、话题数量）

输出模型见 docs/API.md 的 ContentDraft。
"""
from __future__ import annotations

from typing import Any

from ..config import Settings, llm_ready
from ..llm_client import DeepSeekClient, LLMError
from ..logger import log
from ..models import ContentDraft, HotTopic, SkillResult
from .base import Skill

# 各平台风格 System Prompt（可被 tone 参数追加要求）
_PLATFORM_PROMPTS: dict[str, str] = {
    "xiaohongshu": (
        "你是一名资深小红书运营。输出风格要求：\n"
        "1. 标题 15-25 字，带情绪钩子（如「救命」「谁懂啊」「超绝」），可含 emoji\n"
        "2. 正文 150-300 字，口语化、分点或短句，真诚种草感，突出真实体验与实用信息\n"
        "3. 话题标签 5-8 个，贴合内容与热点\n"
        "4. 配图提示词：适合小红书审美的场景化画面描述（构图/色调/氛围）\n"
        "5. 严禁违规词、医疗功效承诺、夸大宣传"
    ),
    "douyin": (
        "你是一名资深抖音短视频运营。输出风格要求：\n"
        "1. 标题/口播文案 30-60 字，前 3 秒抓人，口语化、节奏快\n"
        "2. 正文（视频简介）80-200 字，可含互动引导（评论区聊聊…）\n"
        "3. 话题标签 3-6 个\n"
        "4. 配图提示词：适合抖音竖屏的视觉描述\n"
        "5. 严禁违规词、夸大宣传"
    ),
    "wechat_channels": (
        "你是一名微信视频号运营。输出风格要求：\n"
        "1. 标题 6-16 字，简洁克制，信息明确\n"
        "2. 正文/简介 60-150 字，可带适度情绪价值，引导点赞关注\n"
        "3. 话题标签 2-4 个\n"
        "4. 配图提示词：适合视频号封面的画面描述\n"
        "5. 严禁违规词、夸大宣传"
    ),
}

_GENERATE_SYSTEM = (
    "你只输出一个合法 JSON 对象，结构为："
    '{"drafts":[{"title":"…","content":"…","tags":["…"],"image_prompt":"…"}]}。'
    "硬性要求：\n"
    "1. 每份草稿必须围绕给定热点展开，但**禁止照搬热点标题原文**，禁止复述新闻内容本身；\n"
    "2. 必须有**独到的观点或角度**：给出反常识判断、行业洞察、个人痛点、实用方法中的至少一个，"
    "避免空泛的'我觉得很好/值得关注'式陈述；\n"
    "3. 开头 1-2 句要制造冲突或悬念，拒绝平铺直叙；\n"
    "4. 内容要有信息增量（具体数字/细节/亲身经历/对比），拒绝正确的废话；\n"
    "5. 结尾给一个可执行的建议或引发讨论的问题。"
)

_REVIEW_SYSTEM = (
    "你是社交媒体内容审核专家。请对每一份草稿独立打分(0-10 分)，"
    "评分维度：标题吸引力(30%)、正文完整度与质量(40%)、话题与热点契合度(20%)、合规性(10%)。"
    "你只输出一个合法 JSON 对象，结构为："
    '{"reviews":[{"index":0,"score":8.5,"note":"一句话理由"}]}'
)


class ContentGenerator(Skill):
    """文案生成 + 自我审核。"""

    name = "content_generator"
    description = "根据热点与平台风格生成结构化文案（标题/正文/标签/配图提示词）并自我审核"

    def __init__(self, settings: Settings):
        self._settings = settings
        self._llm = DeepSeekClient(settings)

    async def run(
        self,
        topics: list[HotTopic],
        platform: str,
        count: int = 3,
        tone: str = "",
        custom: dict | None = None,
        **_: Any,
    ) -> SkillResult:
        """生成草稿。

        Args:
            topics: 热点列表（可传入空列表，此时按自由创作处理）
            platform: xiaohongshu | douyin | wechat_channels
            count: 草稿份数（默认 3，供后续自审择优）
            tone: 追加语气要求（如 "专业严谨"）
            custom: 定制化内容配置（config/custom_content.yaml 的结构），
                    用于混合生成：账号人设/自有内容库/固定栏目/排除话题
        """
        if platform not in _PLATFORM_PROMPTS:
            return SkillResult(success=False, error=f"不支持的平台: {platform}")

        # 定制化配置里的排除话题命中 → 过滤热点
        if custom:
            exclude = custom.get("account", {}).get("exclude_topics") or []
            if exclude:
                before = len(topics)
                topics = [t for t in topics if not any(k in t.title for k in exclude)]
                if len(topics) < before:
                    log.info("定制配置排除 %d 条命中排除话题的热点", before - len(topics))

        drafts = await self._generate(topics, platform, count, tone, custom=custom)
        if not drafts:
            return SkillResult(success=False, error="文案生成失败（LLM 不可用或返回空）")

        best = await self._review_and_pick(drafts)
        log.info("生成 %d 份草稿，自审选中第 %d 份(%.1f 分): %s",
                 len(drafts), drafts.index(best) + 1, best.score, best.title)
        return SkillResult(success=True, data={"drafts": drafts, "best": best})

    # ---------------- 生成 ----------------
    async def _generate(
        self, topics: list[HotTopic], platform: str, count: int, tone: str,
        custom: dict | None = None,
    ) -> list[ContentDraft]:
        """调用默认模型生成 count 份草稿；失败返回空列表。"""
        if not llm_ready(self._settings):
            log.warning("未配置可用 LLM API Key（WebUI → 设置 → 模型与密钥），跳过文案生成")
            return []
        if not topics:
            return []  # 无热点时由 Task 层决定处理方式

        topic_text = "\n".join(
            f"{i + 1}. [{t.platform}] {t.title}" + (f"（热度{t.heat}）" if t.heat else "")
            for i, t in enumerate(topics[:10])
        )
        style = _PLATFORM_PROMPTS[platform]
        if tone:
            style += f"\n额外要求：{tone}"

        # 定制化内容注入
        custom_block = _format_custom_prompt(custom) if custom else ""
        user = (
            f"平台风格要求：\n{style}\n\n"
            f"今日热点话题：\n{topic_text}\n\n"
            + (f"账号定制化内容（请按比例混合融入，勿生硬硬广）：\n{custom_block}\n\n" if custom_block else "")
            + f"请基于以上热点生成 {count} 份不同切入角度的草稿。"
        )
        try:
            payload = self._llm.chat_json(_GENERATE_SYSTEM, user, temperature=0.8)
        except LLMError as exc:
            log.error("文案生成 LLM 调用失败: %s", exc)
            return []

        drafts: list[ContentDraft] = []
        for item in (payload.get("drafts") or [])[:count]:
            title = (item.get("title") or "").strip()
            content = (item.get("content") or "").strip()
            if not title or not content:
                continue
            drafts.append(
                ContentDraft(
                    platform=platform,
                    title=title,
                    content=content,
                    tags=[t.strip().lstrip("#") for t in (item.get("tags") or []) if t.strip()],
                    image_prompt=(item.get("image_prompt") or "").strip(),
                    topic_ref=topics[0].title if topics else "",
                )
            )
        return drafts

    # ---------------- 自审择优 ----------------
    async def _review_and_pick(self, drafts: list[ContentDraft]) -> ContentDraft:
        """对草稿打分并返回最优；LLM 不可用时用启发式评分兜底。"""
        if llm_ready(self._settings):
            scored = await self._review_with_llm(drafts)
            if scored:
                return max(scored, key=lambda d: d.score)
        # 兜底：启发式评分
        for draft in drafts:
            draft.score = self._heuristic_score(draft)
            draft.review_note = "启发式评分（LLM 不可用）"
        return max(drafts, key=lambda d: d.score)

    async def _review_with_llm(self, drafts: list[ContentDraft]) -> list[ContentDraft]:
        """用 DeepSeek 批量打分，失败返回空列表（走兜底）。"""
        text = "\n\n".join(
            f"[{i}] 标题：{d.title}\n正文：{d.content}\n标签：{' '.join(d.tags)}"
            for i, d in enumerate(drafts)
        )
        try:
            payload = self._llm.chat_json(_REVIEW_SYSTEM, f"请审核以下草稿：\n\n{text}")
        except LLMError as exc:
            log.error("自审 LLM 调用失败，使用兜底评分: %s", exc)
            return []

        reviews = {r.get("index"): r for r in (payload.get("reviews") or [])}
        for i, draft in enumerate(drafts):
            review = reviews.get(i, {})
            try:
                draft.score = float(review.get("score", 0))
            except (TypeError, ValueError):
                draft.score = 0.0
            draft.review_note = str(review.get("note", ""))
        return drafts

    @staticmethod
    def _heuristic_score(draft: ContentDraft) -> float:
        """无 LLM 时的启发式评分：标题 15-25 字、正文 100-300 字、标签 3-8 个。"""
        score = 5.0
        title_len = len(draft.title)
        if 15 <= title_len <= 25:
            score += 2
        elif title_len >= 6:
            score += 1
        content_len = len(draft.content)
        if 100 <= content_len <= 300:
            score += 2
        elif content_len >= 50:
            score += 1
        tag_count = len(draft.tags)
        if 3 <= tag_count <= 8:
            score += 1
        return min(score, 10.0)


def _format_custom_prompt(custom: dict) -> str:
    """把定制化内容配置格式化成提示词文本块。"""
    lines: list[str] = []
    account = custom.get("account") or {}
    if account.get("persona"):
        lines.append(f"账号人设：{account['persona']}")
    if account.get("tone"):
        lines.append(f"语气要求：{account['tone']}")
    if account.get("fixed_hashtags"):
        lines.append(f"固定话题（每篇必带）：{' '.join('#' + t for t in account['fixed_hashtags'])}")

    materials = custom.get("custom_materials") or []
    if materials:
        lines.append("\n自有内容素材（可融入）：")
        for m in materials[:5]:
            detail = m.get("detail") or ""
            hooks = m.get("hooks") or []
            lines.append(f"- {m.get('name', '')}：{detail}"
                         + (f"；卖点：{'；'.join(hooks[:3])}" if hooks else ""))
    columns = custom.get("columns") or []
    if columns:
        lines.append("\n固定栏目：")
        for c in columns[:3]:
            lines.append(f"- {c.get('name', '')}：{c.get('schedule_hint', '')}；{c.get('topic_hint', '')}")
    mix = custom.get("mix") or {}
    if mix:
        lines.append(
            f"\n混合要求：约 {int(float(mix.get('hot_topic_weight', 0.6)) * 100)}% 内容源于热点灵感，"
            f"约 {int(float(mix.get('custom_weight', 0.4)) * 100)}% 融入定制素材/栏目，自然结合、不生硬"
        )
    return "\n".join(lines)

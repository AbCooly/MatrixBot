"""Content_Generator 测试：mock LLM，不访问真实 API。"""
from __future__ import annotations

from dataclasses import replace

from agent.llm_client import LLMError
from agent.models import ContentDraft, HotTopic
from agent.skills.content_generator import ContentGenerator

TOPIC = HotTopic(platform="weibo", rank=1, title="测试热点话题", heat=1000)


def _keyed(settings):
    """带假 API Key 的配置（配合 mock LLM 使用，不会发起真实请求）。"""
    return replace(settings, deepseek_api_key="sk-test")

DRAFTS_JSON = {
    "drafts": [
        {"title": "标题一：超绝体验", "content": "正文内容一" * 20, "tags": ["美食", "探店"], "image_prompt": "暖色调餐厅"},
        {"title": "标题二：谁懂啊", "content": "正文内容二" * 20, "tags": ["生活"], "image_prompt": "生活场景"},
        {"title": "标题三：救命", "content": "正文内容三" * 20, "tags": ["日常"], "image_prompt": "街拍"},
    ]
}

REVIEW_JSON = {
    "reviews": [
        {"index": 0, "score": 8.5, "note": "标题有钩子"},
        {"index": 1, "score": 9.2, "note": "最佳"},
        {"index": 2, "score": 7.0, "note": "普通"},
    ]
}


class TestContentGenerator:
    async def test_generate_with_llm(self, settings, monkeypatch):
        """mock chat_json 返回固定草稿。"""
        skill = ContentGenerator(_keyed(settings))
        calls = []

        def fake_chat_json(system, user, temperature=0.4, max_tokens=2048):
            calls.append(user)
            return DRAFTS_JSON

        monkeypatch.setattr(skill._llm, "chat_json", fake_chat_json)
        drafts = await skill._generate([TOPIC], "xiaohongshu", 3, "")
        assert len(drafts) == 3
        assert drafts[0].platform == "xiaohongshu"
        assert drafts[0].tags == ["美食", "探店"]
        assert drafts[0].topic_ref == "测试热点话题"
        # System Prompt 应包含平台风格
        assert "小红书" in calls[0]

    async def test_generate_without_key(self, settings):
        """无 API Key 时返回空列表。"""
        skill = ContentGenerator(settings)
        drafts = await skill._generate([TOPIC], "xiaohongshu", 3, "")
        assert drafts == []

    async def test_review_and_pick_llm(self, settings, monkeypatch):
        """自审应选中最高分草稿。"""
        skill = ContentGenerator(_keyed(settings))
        drafts = [
            ContentDraft(platform="douyin", title="A", content="x" * 100, tags=["a"]),
            ContentDraft(platform="douyin", title="B", content="y" * 100, tags=["b"]),
            ContentDraft(platform="douyin", title="C", content="z" * 100, tags=["c"]),
        ]
        monkeypatch.setattr(skill._llm, "chat_json", lambda *a, **k: REVIEW_JSON)
        best = await skill._review_and_pick(drafts)
        assert best.title == "B"
        assert best.score == 9.2

    async def test_review_and_pick_llm_fail_fallback(self, settings, monkeypatch):
        """LLM 审核失败时走启发式评分兜底，仍能选出最优。"""
        skill = ContentGenerator(_keyed(settings))

        def boom(*a, **k):
            raise LLMError("mock 失败")

        monkeypatch.setattr(skill._llm, "chat_json", boom)
        drafts = [
            ContentDraft(platform="douyin", title="短", content="短", tags=[]),
            ContentDraft(platform="douyin", title="这是一个十五字的完整标题", content="正文" * 60, tags=["a", "b", "c"]),
        ]
        best = await skill._review_and_pick(drafts)
        assert best.title == "这是一个十五字的完整标题"
        assert "启发式" in best.review_note

    def test_heuristic_score(self):
        """启发式评分：好的草稿分数更高。"""
        good = ContentDraft(platform="douyin", title="这是一个十五字的完整标题", content="正" * 150, tags=["a", "b", "c"])
        bad = ContentDraft(platform="douyin", title="短", content="短", tags=[])
        assert ContentGenerator._heuristic_score(good) > ContentGenerator._heuristic_score(bad)

    async def test_run_unsupported_platform(self, settings):
        skill = ContentGenerator(settings)
        result = await skill.run(topics=[TOPIC], platform="qq空间")
        assert result.success is False

    async def test_full_run(self, settings, monkeypatch):
        """整条 run() 链路：生成 → 自审 → 返回 drafts+best。"""
        skill = ContentGenerator(_keyed(settings))
        # 第一次调用（生成）返回 DRAFTS_JSON，第二次调用（审核）返回 REVIEW_JSON
        calls = {"n": 0}

        def fake_chat_json(system, user, temperature=0.4, max_tokens=2048):
            calls["n"] += 1
            return DRAFTS_JSON if calls["n"] == 1 else REVIEW_JSON

        monkeypatch.setattr(skill._llm, "chat_json", fake_chat_json)
        result = await skill.run(topics=[TOPIC], platform="xiaohongshu", count=3)
        assert result.success
        assert len(result.data["drafts"]) == 3
        assert result.data["best"].score == 9.2

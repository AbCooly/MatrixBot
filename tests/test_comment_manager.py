"""Comment_Manager 测试：mock 浏览器相关方法，只测业务逻辑。"""
from __future__ import annotations

from agent.models import Comment
from agent.skills.comment_manager import CommentManager
from agent.skills.uploader.browser import BrowserPool


def _make_manager(settings):
    pool = BrowserPool(settings)
    return CommentManager(settings, pool)


def _comment(cid: str, content: str = "好喜欢这条内容！") -> Comment:
    return Comment(
        platform="xiaohongshu",
        comment_id=cid,
        user_name="粉丝",
        content=content,
        note_url="https://note/1",
    )


class TestCommentManager:
    async def test_generate_reply_template_without_key(self, settings):
        """无 API Key 时返回模板回复（非空）。"""
        manager = _make_manager(settings)
        reply = await manager.generate_reply(_comment("1"))
        assert reply
        assert "感谢" not in reply  # 模板不以套话开头

    async def test_auto_reply_dedupe_and_blacklist(self, settings, monkeypatch):
        """自动回复应跳过已回复与黑名单评论，并持久化去重。"""
        manager = _make_manager(settings)
        # 预置一条已回复记录
        manager._replied = {"xiaohongshu": {"https://note/1": {"1": "已回"}}}

        comments = [
            _comment("1", "已被回复过"),
            _comment("2", "加微信领福利"),  # 黑名单
            _comment("3", "正常评论一条"),
            _comment("4", "另一条正常评论"),
        ]

        async def fake_fetch(platform, url, limit=10):
            return comments

        monkeypatch.setattr(manager, "fetch_comments", fake_fetch)
        monkeypatch.setattr(manager, "_do_reply", _fake_do_reply)

        results = await manager.auto_reply("xiaohongshu", "https://note/1", max_replies=10)
        replied_ids = {r.comment.comment_id for r in results if r.success}
        assert replied_ids == {"3", "4"}  # 1 已回复、2 黑名单被跳过
        # 持久化落盘
        assert manager._replied_path.exists()
        assert "3" in manager._replied["xiaohongshu"]["https://note/1"]

    async def test_auto_reply_max_replies(self, settings, monkeypatch):
        """max_replies 应限制回复条数。"""
        manager = _make_manager(settings)
        comments = [_comment(f"c{i}") for i in range(8)]

        async def fake_fetch(platform, url, limit=10):
            return comments

        monkeypatch.setattr(manager, "fetch_comments", fake_fetch)
        monkeypatch.setattr(manager, "_do_reply", _fake_do_reply)
        results = await manager.auto_reply("xiaohongshu", "https://note/1", max_replies=3)
        assert sum(1 for r in results if r.success) == 3

    async def test_replied_persistence_reload(self, settings):
        """重新加载应恢复已回复记录。"""
        manager = _make_manager(settings)
        manager._mark_replied(_comment("x1"), "回复文本")
        await manager._save_replied()

        manager2 = _make_manager(settings)  # 同 state 目录
        assert manager2._replied["xiaohongshu"]["https://note/1"]["x1"] == "回复文本"

    async def test_auto_reply_forwards_persona_to_generate(self, settings, monkeypatch):
        """auto_reply 应把账号定制（人设/口吻/素材/红线/主题）传给回复生成。"""
        manager = _make_manager(settings)
        calls: dict = {}

        async def fake_fetch(platform, url, limit=10):
            return [_comment("a1", "你们家桂花茶好喝吗？")]

        async def fake_gen(comment, topic="", persona="", tone="", materials=None, taboos=None):
            calls.update(topic=topic, persona=persona, tone=tone,
                         materials=materials, taboos=taboos)
            return "桂花龙井冷萃真的绝，欢迎来店里试喝呀"

        monkeypatch.setattr(manager, "fetch_comments", fake_fetch)
        monkeypatch.setattr(manager, "generate_reply", fake_gen)
        monkeypatch.setattr(manager, "_do_reply", _fake_do_reply)
        results = await manager.auto_reply(
            "xiaohongshu", "https://note/1", max_replies=5,
            topic="冷泡茶上新", persona="新中式茶馆老板娘", tone="温柔亲切",
            materials=[{"name": "冷泡茶", "detail": "桂花龙井冷萃"}],
            taboos=["不许发链接"],
        )
        assert calls["persona"] == "新中式茶馆老板娘"
        assert calls["topic"] == "冷泡茶上新"
        assert calls["tone"] == "温柔亲切"
        assert calls["materials"][0]["name"] == "冷泡茶"
        assert calls["taboos"] == ["不许发链接"]
        assert sum(1 for r in results if r.success) == 1

    async def test_generate_reply_includes_persona_in_prompt(self, settings, monkeypatch):
        """有人设/素材/红线时，提示词里应体现账号定制口吻。"""
        import agent.skills.comment_manager as cm

        manager = _make_manager(settings)
        captured: dict = {}

        class FakeLLM:
            def chat(self, system, user, **kwargs):
                captured["system"] = system
                captured["user"] = user
                return "好喝！欢迎常来呀～"

        manager._llm = FakeLLM()
        monkeypatch.setattr(cm, "llm_ready", lambda s: True)
        reply = await manager.generate_reply(
            _comment("2", "好喝吗？"), topic="冷泡茶上新",
            persona="新中式茶馆老板娘", tone="温柔亲切",
            materials=[{"name": "冷泡茶", "detail": "桂花龙井冷萃，回味甘甜"}],
            taboos=["不发链接", "不提价格"],
        )
        assert reply == "好喝！欢迎常来呀～"
        assert "新中式茶馆老板娘" in captured["system"]
        assert "桂花龙井冷萃" in captured["system"]
        assert "不发链接" in captured["system"]
        assert "冷泡茶上新" in captured["user"]


async def _fake_do_reply(platform, note_url, comment, text):
    from agent.models import ReplyResult

    return ReplyResult(comment=comment, reply_text=text, success=True)

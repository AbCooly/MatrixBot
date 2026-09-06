"""Task 层编排测试：用 Stub Skill 替换真实技能，验证编排逻辑与报告。"""
from __future__ import annotations

from agent.models import ContentDraft, HotTopic, MediaAsset, PublishResult, SkillResult
from agent.tasks.base import TaskContext
from agent.tasks.daily_post import DailyPostTask
from agent.tasks.maintain_traffic import MaintainTrafficTask

TOPIC = HotTopic(platform="weibo", rank=1, title="测试热点", heat=100)
DRAFT = ContentDraft(
    platform="xiaohongshu", title="测试标题", content="测试正文内容" * 10,
    tags=["测试", "热点"], image_prompt="一张图", score=9.0,
)
ASSET = MediaAsset(path="/tmp/cover.png", provider="pillow")
VIDEO_ASSET = MediaAsset(path="/tmp/slideshow.mp4", kind="video", provider="ffmpeg")


class StubSearch:
    def __init__(self, topics):
        self._topics = topics

    async def execute(self, **kwargs):
        return SkillResult(success=True, data=self._topics)


class StubContent:
    def __init__(self, drafts=None):
        self._drafts = drafts or [DRAFT]

    async def execute(self, **kwargs):
        best = max(self._drafts, key=lambda d: d.score)
        return SkillResult(success=True, data={"drafts": self._drafts, "best": best})


class StubMedia:
    """同时支持旧 image 模式（execute）与 slideshow 模式（generate_cards/视频）。"""

    def __init__(self):
        self.card_calls = 0
        self.video_calls = 0

    async def execute(self, **kwargs):
        return SkillResult(success=True, data=ASSET)

    async def generate_cards(self, draft, image_count=4, theme=None):
        """直接返回卡片列表（与 MediaCreator.generate_cards 契约一致）。"""
        self.card_calls += 1
        return [ASSET] * image_count

    async def create_slideshow_video(self, images, music=None, output=None, duration_per_image=3.0):
        """直接返回视频资产（与 MediaCreator.create_slideshow_video 契约一致）。"""
        self.video_calls += 1
        return VIDEO_ASSET


class StubUploader:
    def __init__(self):
        self.published: list[dict] = []
        from agent.config import Settings
        from agent.skills.platform_uploader import PlatformUploader

        # 用真实注册表提供平台能力判断
        self.registry = PlatformUploader(Settings()).registry

    async def publish(self, payload, profile=None):
        self.published.append({"payload": payload, "profile": profile})
        return PublishResult(payload.platform, True, "published", "ok")


class StubComments:
    def __init__(self):
        self.calls: list[dict] = []

    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        return SkillResult(success=True, data=[])


def _ctx(settings, uploader=None, comments=None, task_config=None, media=None) -> TaskContext:
    return TaskContext(
        settings=settings,
        search=StubSearch([TOPIC]),
        content=StubContent(),
        media=media or StubMedia(),
        uploader=uploader or StubUploader(),
        comments=comments or StubComments(),
        task_config=task_config or {},
    )


class TestDailyPostTask:
    async def test_slideshow_flow(self, settings):
        """slideshow 模式：卡片 → 视频 → 以视频发布，报告落盘。"""
        uploader = StubUploader()
        media = StubMedia()
        ctx = _ctx(settings, uploader=uploader, media=media,
                   task_config={"platforms": ["xiaohongshu", "douyin"], "mode": "slideshow"})
        report = await DailyPostTask().execute(ctx)

        assert report.success is True
        assert media.card_calls == 2
        # 平台能力分流：douyin 发视频，xiaohongshu 发图文
        assert media.video_calls == 1
        assert len(uploader.published) == 2
        by_platform = {p["payload"].platform: p["payload"] for p in uploader.published}
        assert by_platform["douyin"].video == "/tmp/slideshow.mp4"
        assert by_platform["xiaohongshu"].video is None
        assert by_platform["xiaohongshu"].images
        assert by_platform["douyin"].title == "测试标题"

        import json

        from pathlib import Path

        reports_dir = settings.state_dir.parent / "logs" / "reports"
        files = list(Path(reports_dir).glob("daily_post_*.json"))
        assert files, "报告未落盘"
        data = json.loads(files[0].read_text(encoding="utf-8"))
        assert data["task_name"] == "daily_post"

    async def test_slideshow_xiaohongshu_uses_images(self, settings):
        """小红书不支持视频 → slideshow 模式下自动改用图文卡片发布。"""
        uploader = StubUploader()
        ctx = _ctx(settings, uploader=uploader, media=StubMedia(),
                   task_config={"platforms": ["xiaohongshu"], "mode": "slideshow"})
        report = await DailyPostTask().execute(ctx)
        assert report.success is True
        pub = uploader.published[0]["payload"]
        assert pub.video is None, "小红书不应发视频"
        assert len(pub.images) == 4, "小红书应发图文卡片"

    async def test_image_mode_flow(self, settings):
        """image 模式：单图图文发布。"""
        uploader = StubUploader()
        ctx = _ctx(settings, uploader=uploader,
                   task_config={"platforms": ["douyin"], "mode": "image"})
        report = await DailyPostTask().execute(ctx)
        assert report.success is True
        assert uploader.published[0]["payload"].images == ["/tmp/cover.png"]
        assert uploader.published[0]["payload"].video is None
        # 报告落盘
        import json

        from pathlib import Path

        reports_dir = settings.state_dir.parent / "logs" / "reports"
        files = list(Path(reports_dir).glob("daily_post_*.json"))
        assert files, "报告未落盘"
        data = json.loads(files[0].read_text(encoding="utf-8"))
        assert data["task_name"] == "daily_post"

    async def test_no_trends(self, settings):
        """热点获取失败时，任务不崩，标记 failed 步骤并继续。"""
        ctx = _ctx(settings)
        ctx.search = StubSearch([])  # 无热点
        report = await DailyPostTask().execute(ctx)
        assert report.success is False
        assert any(s["step"] == "content_xiaohongshu" and s["status"] == "failed" for s in report.steps)

    async def test_publish_failure_recorded(self, settings):
        """发布失败不应中断任务，报告 success=False。"""
        uploader = StubUploader()

        async def _fail(payload, profile=None):
            return PublishResult(payload.platform, False, "failed", "模拟失败")

        uploader.publish = _fail
        ctx = _ctx(settings, uploader=uploader, task_config={"platforms": ["douyin"]})
        report = await DailyPostTask().execute(ctx)
        assert any(s["step"] == "publish_douyin" and s["status"] == "failed" for s in report.steps)
        assert report.success is False


class TestMaintainTrafficTask:
    async def test_reply_flow(self, settings):
        """对每个配置的作品链接执行一次自动回复。"""
        comments = StubComments()
        ctx = _ctx(
            settings,
            comments=comments,
            task_config={
                "notes": [
                    {"platform": "xiaohongshu", "url": "https://note/1"},
                    {"platform": "douyin", "url": "https://video/2"},
                ],
                "max_replies": 5,
            },
        )
        report = await MaintainTrafficTask().execute(ctx)
        assert report.success is True
        assert len(comments.calls) == 2
        assert comments.calls[0]["max_replies"] == 5

    async def test_no_notes(self, settings):
        """未配置作品链接时报告失败并给出提示。"""
        ctx = _ctx(settings, task_config={"notes": []})
        report = await MaintainTrafficTask().execute(ctx)
        assert report.success is False
        assert any(s["step"] == "config" for s in report.steps)

    async def test_custom_persona_injected_into_comment_call(self, settings):
        """流量维护应把账号定制人设/素材/主题/红线透传给评论自动回复。"""
        comments = StubComments()
        ctx = _ctx(
            settings,
            comments=comments,
            task_config={
                "notes": [{"platform": "douyin", "url": "https://v/1", "topic": "冷泡茶上新"}],
                "max_replies": 3,
                "custom_content": {
                    "account": {"persona": "新中式茶馆老板娘", "fixed_hashtags": ["茶"]},
                    "custom_materials": [{"name": "桂花龙井冷萃", "detail": "店内招牌"}],
                    "exclude_topics": ["别发价格"],
                },
            },
        )
        report = await MaintainTrafficTask().execute(ctx)
        assert report.success is True
        call = comments.calls[0]
        assert call["persona"] == "新中式茶馆老板娘"
        assert call["topic"] == "冷泡茶上新"
        assert call["max_replies"] == 3
        assert call["materials"][0]["name"] == "桂花龙井冷萃"
        assert call["taboos"] == ["别发价格"]

    def test_load_custom_account_shape(self):
        """定制内容 account 内嵌结构应被展开为顶层 persona/tone。"""
        custom = MaintainTrafficTask._load_custom(
            {"account": {"persona": "店主小周", "tone": "温柔"}, "tone": "顶层语气"}
        )
        assert custom["persona"] == "店主小周"
        assert custom["tone"] == "顶层语气"  # 顶层优先于 account 内嵌


class TestInlineCustomContent:
    def test_load_custom_content_dict(self):
        """daily_post 支持 WebUI 内联的定制内容 dict。"""
        from agent.tasks.daily_post import DailyPostTask

        custom = {"account": {"persona": "探店博主"}, "custom_materials": [{"name": "柠檬茶店"}]}
        loaded = DailyPostTask._load_custom_content(custom)
        assert loaded == custom

    def test_load_custom_content_path(self, tmp_path):
        """也支持 YAML 文件路径。"""
        from agent.tasks.daily_post import DailyPostTask

        p = tmp_path / "custom.yaml"
        p.write_text("account:\n  persona: 测试\n", encoding="utf-8")
        loaded = DailyPostTask._load_custom_content(str(p))
        assert loaded == {"account": {"persona": "测试"}}

    def test_load_custom_content_none(self):
        from agent.tasks.daily_post import DailyPostTask

        assert DailyPostTask._load_custom_content(None) is None
        assert DailyPostTask._load_custom_content("不存在.yaml") is None

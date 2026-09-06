"""Media_Creator 测试：只测离线 Pillow 封面（不依赖任何生图 API）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.skills.media_creator import MediaCreator


class TestMediaCreator:
    async def test_compose_cover(self, settings, tmp_path):
        """Pillow 封面应生成正确尺寸的 PNG。"""
        skill = MediaCreator(settings, output_dir=tmp_path / "gen")
        asset = await skill.compose_cover("测试标题文字", size="1:1")
        assert Path(asset.path).exists()
        assert asset.provider == "pillow"
        assert asset.width == 1080
        assert asset.height == 1080

    async def test_compose_cover_vertical(self, settings, tmp_path):
        skill = MediaCreator(settings, output_dir=tmp_path / "gen2")
        asset = await skill.compose_cover("竖屏标题", size="3:4")
        assert asset.width == 1080
        assert asset.height == 1440

    async def test_run_falls_back_to_pillow_without_key(self, settings, tmp_path):
        """无生图 Key 时，run() 应自动降级为文字封面。"""
        skill = MediaCreator(settings, output_dir=tmp_path / "gen3")
        result = await skill.run(prompt="一张测试图", title="今日热点", size="1:1")
        assert result.success
        assert result.data.provider == "pillow"
        assert Path(result.data.path).exists()

    async def test_run_with_title_only(self, settings, tmp_path):
        """只有标题（无提示词）时直接合成封面。"""
        skill = MediaCreator(settings, output_dir=tmp_path / "gen4")
        result = await skill.run(title="纯标题封面")
        assert result.success
        assert result.data.provider == "pillow"


class TestCardsAndSlideshow:
    async def test_generate_cards(self, settings, tmp_path):
        """批量生成竖屏图文卡片（封面+内容+结尾）。"""
        from agent.models import ContentDraft

        from agent.skills.media_creator import pick_music

        skill = MediaCreator(settings, output_dir=tmp_path / "cards")
        draft = ContentDraft(
            platform="douyin", title="十一个字的标题测试", content="第一句内容。第二句内容。第三句内容。",
            tags=["美食", "探店"],
        )
        cards = await skill.generate_cards(draft, image_count=4)
        assert len(cards) == 4
        for c in cards:
            assert Path(c.path).exists()
            assert c.width == 1080 and c.height == 1920

    async def test_create_slideshow_video(self, settings, tmp_path):
        """ffmpeg 合成竖屏视频（真实执行，ffprobe 校验）。"""
        import shutil
        import subprocess

        import pytest

        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            pytest.skip("未安装 ffmpeg / ffprobe")
        from PIL import Image

        skill = MediaCreator(settings, output_dir=tmp_path / "video")
        imgs = []
        for i in range(3):
            p = tmp_path / f"v{i}.png"
            Image.new("RGB", (1080, 1920), (200, 100 + i * 40, 120)).save(p)
            imgs.append(str(p))
        video = await skill.create_slideshow_video(imgs, duration_per_image=1.0)
        assert Path(video.path).exists()
        assert video.kind == "video"
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,duration", "-of", "csv=p=0", video.path],
            capture_output=True, text=True,
        )
        out = r.stdout.strip().split(",")
        assert out[0] == "1080" and out[1] == "1920"
        assert abs(float(out[2]) - 3.0) < 0.5  # 3 张 × 1 秒

    async def test_slideshow_without_ffmpeg(self, settings, tmp_path, monkeypatch):
        """无 ffmpeg 时应抛 SlideshowError 而非崩溃。"""
        from agent.skills.media_creator import SlideshowError

        skill = MediaCreator(settings, output_dir=tmp_path / "novid")
        monkeypatch.setattr("shutil.which", lambda *a, **k: None)
        with pytest.raises(SlideshowError):
            await skill.create_slideshow_video(["/tmp/a.png"])

    def test_pick_music(self, tmp_path):
        """音乐选择：从目录随机选；空目录/不存在返回 None。"""
        from agent.skills.media_creator import pick_music

        assert pick_music(None) is None
        assert pick_music(tmp_path / "不存在") is None
        (tmp_path / "a.mp3").write_bytes(b"x")
        (tmp_path / "b.wav").write_bytes(b"x")
        (tmp_path / "c.txt").write_bytes(b"x")
        assert pick_music(tmp_path) in {str(tmp_path / "a.mp3"), str(tmp_path / "b.wav")}


class TestResolveMusicNoBuiltin:
    def test_no_music_returns_none(self, settings):
        """未上传任何音乐时 resolve_music 返回 None（无声），不再内置合成。"""
        from agent.skills.media_creator import resolve_music

        assert resolve_music(settings, {}) is None
        assert resolve_music(settings, {"music": "不存在的歌.mp3"}) is None

    def test_uploaded_music_resolved(self, settings):
        """用户上传到素材库的音乐应被选中。"""
        from pathlib import Path

        from agent.skills.media_creator import media_root, resolve_music

        root = media_root(settings, "music")
        root.mkdir(parents=True, exist_ok=True)
        (root / "my_song.mp3").write_bytes(b"fake-mp3")
        got = resolve_music(settings, {"music": "my_song.mp3"})
        assert got
        assert Path(got).exists()
        assert got.endswith("my_song.mp3")


class TestTextLayout:
    def test_bundled_font_exists(self):
        """项目内置中文字体存在（避免用户机器缺字体导致乱码）。"""
        from pathlib import Path

        font = Path(__file__).resolve().parent.parent / "assets" / "fonts" / "DroidSansFallbackFull.ttf"
        assert font.exists(), "内置中文字体缺失"

    def test_wrap_by_font_width(self):
        """长文本按字体宽度折行，不超出卡片宽度。"""
        from agent.skills.media_creator import MediaCreator

        font = MediaCreator._find_cjk_font(96)
        from agent.skills.media_creator import _wrap_text

        long_text = "这家手打柠檬茶店我真的会反复回购夏天一口下去整个人都活过来了" * 2
        lines = _wrap_text(long_text, 10, font=font, max_px=920)
        assert len(lines) > 1
        for line in lines:
            assert font.getlength(line) <= 920 + 1, f"折行后仍超宽: {line}"

    async def test_generate_cards_no_overflow(self, settings, tmp_path):
        """生成的卡片左右边缘无文字像素（无超宽被裁切）。"""
        from PIL import Image

        from agent.models import ContentDraft

        from agent.skills.media_creator import MediaCreator

        skill = MediaCreator(settings, output_dir=tmp_path / "cards")
        draft = ContentDraft(platform="douyin", title="十个字标题测试一二三", content="第一句内容。第二句内容。第三句内容。", tags=["美食"])
        cards = await skill.generate_cards(draft, image_count=3)
        for c in cards:
            px = Image.open(c.path).convert("L")
            w, h = px.size
            for x in (2, w - 3):
                edge = sum(1 for y in range(0, h, 8) if px.getpixel((x, y)) < 100)
                assert edge == 0, f"卡片 {c.path} 边缘 {x}px 处有文字像素（超宽被裁切）"

"""Media_Creator 技能：素材处理。

生成配图的供应商降级链：
  1. fal.ai Flux（需 FAL_KEY，质量最高）
  2. Stability AI（需 STABILITY_API_KEY）
  3. Pillow 本地合成封面（无需任何 Key，纯离线兜底）

安全规则：外部 API 全部 try/except；产物统一落到 assets/generated/。
"""
from __future__ import annotations

import asyncio
import hashlib
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ..config import Settings
from ..logger import log
from ..models import MediaAsset, SkillResult
from .base import Skill

_TIMEOUT = httpx.Timeout(120.0)  # 生图较慢
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"


class MediaCreator(Skill):
    """AI 生图 / 封面合成。"""

    name = "media_creator"
    description = "根据提示词生成配图（Flux/Stability/Pillow 降级链）或合成文字封面"

    def __init__(self, settings: Settings, output_dir: Path | None = None):
        self._settings = settings
        self._output_dir = output_dir or (settings.state_dir.parent / "assets" / "generated")
        self._output_dir.mkdir(parents=True, exist_ok=True)

    # ---------------- 主入口 ----------------
    async def run(self, prompt: str = "", title: str = "", size: str = "1:1", **_: Any) -> SkillResult:
        """生成配图。

        Args:
            prompt: 生图提示词（优先）
            title: 标题（prompt 为空时用于合成文字封面）
            size: "1:1" | "3:4" | "4:3" | "9:16"
        """
        if prompt:
            asset = await self._create_with_ai(prompt, size)
            if asset is not None:
                return SkillResult(success=True, data=asset, meta={"provider": asset.provider})
            log.warning("AI 生图全部失败，降级为文字封面")
        asset = await self.compose_cover(title or "今日热点", size=size)
        return SkillResult(success=True, data=asset, meta={"provider": "pillow"})

    # ---------------- AI 生图（降级链） ----------------
    async def _create_with_ai(self, prompt: str, size: str) -> MediaAsset | None:
        """按 fal → stability 顺序尝试；全部失败返回 None。"""
        if self._settings.fal_key:
            try:
                asset = await self._fal_flux(prompt, size)
                if asset is not None:
                    return asset
            except Exception as exc:  # noqa: BLE001
                log.warning("fal.ai Flux 生图失败: %s", exc)

        if self._settings.stability_api_key:
            try:
                asset = await self._stability(prompt, size)
                if asset is not None:
                    return asset
            except Exception as exc:  # noqa: BLE001
                log.warning("Stability AI 生图失败: %s", exc)

        return None

    @staticmethod
    def _aspect(size: str) -> str:
        """把 "1:1/3:4/4:3/9:16" 映射到 fal 的参数。"""
        return {"1:1": "square", "3:4": "portrait_4_3", "4:3": "landscape_4_3", "9:16": "portrait_16_9"}.get(
            size, "square"
        )

    async def _fal_flux(self, prompt: str, size: str) -> MediaAsset | None:
        """fal.ai Flux.schnell（快速模型）。"""
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": _UA}) as client:
            resp = await client.post(
                "https://queue.fal.run/fal-ai/flux/schnell",
                headers={"Authorization": f"Key {self._settings.fal_key}"},
                json={"prompt": prompt, "image_size": self._aspect(size), "num_images": 1},
            )
            resp.raise_for_status()
            payload = resp.json()
            image = (payload.get("images") or [{}])[0]
            url = image.get("url")
            if not url:
                raise RuntimeError("fal 响应缺少图片 URL")
            img_resp = await client.get(url)
            img_resp.raise_for_status()
            path = self._output_dir / self._filename("flux", "png")
            path.write_bytes(img_resp.content)
            return MediaAsset(
                path=str(path), width=int(image.get("width") or 0), height=int(image.get("height") or 0),
                provider="flux",
            )

    async def _stability(self, prompt: str, size: str) -> MediaAsset | None:
        """Stability AI Stable Image（sd3，multipart 表单）。"""
        aspect = {"1:1": "1:1", "3:4": "3:4", "4:3": "4:3", "9:16": "9:16"}.get(size, "1:1")
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": _UA}) as client:
            resp = await client.post(
                "https://api.stability.ai/v2beta/stable-image/generate/sd3",
                headers={"Authorization": f"Bearer {self._settings.stability_api_key}", "Accept": "image/*"},
                data={"prompt": prompt, "aspect_ratio": aspect, "output_format": "png"},
            )
            resp.raise_for_status()
            path = self._output_dir / self._filename("stability", "png")
            path.write_bytes(resp.content)
            return MediaAsset(path=str(path), provider="stability")

    @staticmethod
    def _filename(provider: str, ext: str) -> str:
        """生成唯一文件名（时间戳+随机）。"""
        import time

        return f"{provider}_{int(time.time() * 1000)}_{random.randint(1000, 9999)}.{ext}"

    # ---------------- Pillow 兜底封面 ----------------
    async def compose_cover(self, title: str, size: str = "1:1") -> MediaAsset:
        """Pillow 合成封面：渐变背景 + 居中标题文字（离线可用）。"""
        from PIL import Image, ImageDraw, ImageFont

        w, h = {"1:1": (1080, 1080), "3:4": (1080, 1440), "4:3": (1440, 1080), "9:16": (1080, 1920)}.get(
            size, (1080, 1080)
        )

        # 基于标题哈希挑选一组柔和渐变色（同标题颜色稳定）
        seed = int(hashlib.md5(title.encode("utf-8")).hexdigest()[:8], 16)
        rng = random.Random(seed)
        top = tuple(rng.randint(60, 180) for _ in range(3))
        bottom = tuple(rng.randint(20, 90) for _ in range(3))

        img = Image.new("RGB", (w, h))
        draw = ImageDraw.Draw(img)
        for y in range(h):
            ratio = y / h
            color = tuple(int(top[i] + (bottom[i] - top[i]) * ratio) for i in range(3))
            draw.line([(0, y), (w, y)], fill=color)

        font = self._find_cjk_font(min(w, h) // 12)
        small_font = self._find_cjk_font(min(w, h) // 30)

        # 标题换行居中（每行最多 10 个汉字）
        lines: list[str] = []
        for i in range(0, len(title), 10):
            lines.append(title[i : i + 10])
        line_height = min(w, h) // 8
        start_y = h // 2 - (len(lines) * line_height) // 2
        for idx, line in enumerate(lines):
            bbox = draw.textbbox((0, 0), line, font=font)
            x = (w - (bbox[2] - bbox[0])) // 2
            draw.text((x, start_y + idx * line_height), line, font=font, fill=(255, 255, 255))

        # 底部水印
        bbox = draw.textbbox((0, 0), "DeepSeek Social Agent", font=small_font)
        draw.text(((w - (bbox[2] - bbox[0])) // 2, h - int(h * 0.08)), "DeepSeek Social Agent",
                  font=small_font, fill=(255, 255, 255))

        path = self._output_dir / self._filename("pillow", "png")
        img.save(path, "PNG")
        log.info("Pillow 封面已生成: %s (%dx%d)", path, w, h)
        return MediaAsset(path=str(path), width=w, height=h, provider="pillow")

    _FONT_LOG_ONCE: set = set()

    @staticmethod
    def _find_cjk_font(size: int):
        """找能真正画出中文的字体（避免渲染成「口」字方块）：

        1. 依次探测：项目内置字体 → 系统常见 CJK 字体（Noto/WQY/文泉驿/苹方/微软雅黑）；
        2. 每个候选加载成功后还要用「中」实测字形宽度——有些字体文件存在但缺 CJK
           glyph（选了会整段中文变方块），宽度为 0 视为不可用继续找；
        3. 每次进程首次选中某字体打一条日志（方便排查卡片乱码）。
        """
        from PIL import ImageFont

        # 项目内置字体（如有随项目分发）
        bundled = Path(__file__).resolve().parent.parent.parent / "assets" / "fonts" / "DroidSansFallbackFull.ttf"
        candidates = [
            str(bundled),
            # Debian/Ubuntu fonts-noto-cjk 常见安装位置
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
            "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/truetype/arphic/uming.ttc",
            # macOS / Windows
            "/System/Library/Fonts/PingFang.ttc",
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simhei.ttf",
        ]
        for path in candidates:
            if not Path(path).exists():
                continue
            try:
                font = ImageFont.truetype(path, size)
            except Exception:  # noqa: BLE001 —— 加载失败继续找
                continue
            try:
                # 实测中文字形宽度：为 0 说明该字体不包含 CJK glyph，选中=整段方块
                bbox = font.getbbox("中")
                if bbox[2] - bbox[0] <= 0:
                    continue
            except Exception:  # noqa: BLE001
                continue
            if path not in MediaCreator._FONT_LOG_ONCE:
                MediaCreator._FONT_LOG_ONCE.add(path)
                log.info("卡片/封面使用中文字体: %s", path)
            return font
        log.warning("未找到可用的中文字体，封面中文将显示为方块；请安装 Noto Sans CJK"
                    "（Debian/Ubuntu: apt install fonts-noto-cjk）")
        return ImageFont.load_default()

    # ---------------- 图文卡片 + 幻灯片视频 ----------------
    async def generate_cards(self, draft, image_count: int = 4, theme: dict | None = None) -> list[MediaAsset]:
        """根据草稿批量生成竖屏图文卡片（封面卡 + 内容卡 + 结尾卡）。

        theme: 视觉主题配置 {"style": "<背景风格id>", "image": "<背景图文件名>", ...}，
               传 None 时用经典渐变（向后兼容）。
        """
        return await generate_cards_impl(self, draft, image_count, theme=theme)

    async def create_slideshow_video(
        self,
        images: list[str],
        output: str | None = None,
        music: str | None = None,
        duration_per_image: float = 3.0,
    ) -> MediaAsset:
        """把多张图片合成竖屏幻灯片视频（ffmpeg，可配背景音乐）。"""
        return await create_slideshow_video_impl(
            self, images, output=output, music=music, duration_per_image=duration_per_image
        )


# ============================================================================
# 图文卡片 + 幻灯片视频（多图 + 文字 + 音乐合成视频，替代昂贵的 AI 视频生成）
# ============================================================================

@dataclass
class CardSpec:
    """一张图文卡片的规格。"""

    text: str                # 卡片主文字
    subtitle: str = ""       # 副标题（小字）
    kind: str = "content"    # cover | content | ending
    accent: str | None = None  # 强调色（hex），默认按序号取


class SlideshowError(RuntimeError):
    """幻灯片视频合成失败。"""


def _split_sentences(text: str, max_len: int = 30) -> list[str]:
    """把正文切成适合卡片展示的短句（按标点切分）。"""
    import re

    parts = re.split(r"[。！？!?；;]", text)
    out = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if len(p) > max_len:
            # 超长则按逗号再切
            for q in re.split(r"[，,、]", p):
                q = q.strip()
                if q:
                    out.append(q[:max_len])
        else:
            out.append(p[:max_len])
    return out or [text[:max_len]]


# ---------------- 卡片视觉主题（背景模板引擎） ----------------
# 每个风格在任意标题/尺寸下都能稳定复现（哈希种子）；"auto" 按卡片序号走经典渐变。
CARD_BG_STYLES: list[dict] = [
    {"id": "aurora", "name": "极光渐变", "desc": "流动双色极光 + 柔和光晕，适合情绪/氛围内容"},
    {"id": "galaxy", "name": "深空星云", "desc": "深色星空 + 星点与星云，适合科技/知识/夜话"},
    {"id": "ink", "name": "新中式水墨", "desc": "宣纸米色 + 淡墨晕染，适合茶饮/文创/国学"},
    {"id": "sakura", "name": "樱花粉调", "desc": "柔和樱花色 + 花瓣飘落，适合美妆/生活/情感"},
    {"id": "morandi", "name": "莫兰迪色块", "desc": "低饱和撞色块面，高级感，适合家居/穿搭"},
    {"id": "paper", "name": "奶油杂志", "desc": "暖白 + 马卡龙色块卡片风，适合探店/手作"},
    {"id": "minimal", "name": "极简线条", "desc": "浅底 + 细线网格与几何线框，适合职场/知识"},
    {"id": "tech", "name": "科技霓虹", "desc": "深蓝 + 网格透视线与霓虹光斑，适合数码/AI"},
    {"id": "lime", "name": "柠檬气泡", "desc": "清爽黄绿 + 气泡圆点，适合茶饮/夏日/活力"},
]


def _bg_seed(seed_text: str) -> int:
    return int(hashlib.md5(seed_text.encode("utf-8")).hexdigest()[:8], 16)


def _lerp(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _vertical_gradient(w: int, h: int, stops: list[tuple[float, tuple]]) -> "Image.Image":
    """按 stops=[(0,color), (1,color)...] 画竖向渐变。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    n = len(stops)
    for y in range(h):
        t = y / (h - 1) if h > 1 else 0
        seg = min(n - 2, int(t * (n - 1)))
        f0, c0 = stops[seg]
        f1, c1 = stops[seg + 1]
        local = 0.0 if f1 == f0 else (t - f0) / (f1 - f0)
        d.line([(0, y), (w, y)], fill=_lerp(c0, c1, local))
    return img


def _card_palette(index: int) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """按序号返回一组渐变主色（避免相邻卡片同色）。"""
    palettes = [
        ((255, 154, 158), (250, 208, 196)),   # 蜜桃
        ((162, 155, 254), (203, 220, 255)),   # 雾蓝
        ((255, 204, 100), (255, 237, 188)),   # 奶黄
        ((168, 230, 207), (208, 244, 231)),   # 薄荷
        ((255, 183, 197), (255, 218, 226)),   # 樱花
        ((180, 216, 255), (224, 238, 255)),   # 天蓝
    ]
    return palettes[index % len(palettes)]


def _render_background(w: int, h: int, style_id: str, seed_text: str) -> "Image.Image":
    """按主题风格渲染一张竖屏背景（纯 Pillow 离线，确定性输出）。

    style_id: aurora | galaxy | ink | sakura | morandi | paper | minimal | tech | lime
    """
    from PIL import Image, ImageDraw

    rng = random.Random(_bg_seed(seed_text))
    img = _vertical_gradient(w, h, [(0.0, (245, 243, 238)), (1.0, (235, 230, 220))])
    d = ImageDraw.Draw(img)
    cx, cy = w * 0.5, h * 0.5

    if style_id == "aurora":
        img = _vertical_gradient(w, h, [
            (0.0, (15, 10, 45)), (0.45, (48, 20, 88)), (0.8, (110, 50, 120)), (1.0, (24, 16, 60)),
        ])
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        for i, (cx0, cy0, rr, col, al) in enumerate([
            (w * 0.25, h * 0.28, w * 0.9, (90, 220, 255), 42),
            (w * 0.8, h * 0.42, w * 0.95, (255, 110, 210), 34),
            (w * 0.55, h * 0.75, w * 0.8, (120, 255, 200), 26),
        ]):
            ld.ellipse([cx0 - rr, cy0 - rr, cx0 + rr, cy0 + rr], fill=col + (al,))
        img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
    elif style_id == "galaxy":
        img = _vertical_gradient(w, h, [(0.0, (8, 10, 28)), (0.6, (24, 18, 54)), (1.0, (8, 6, 24))])
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        for _ in range(140):
            x, y = rng.randint(0, w - 1), rng.randint(0, h - 1)
            r = rng.randint(1, 3)
            ld.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, rng.randint(70, 200)))
        for (gx, gy, gr, col, al) in [
            (w * 0.7, h * 0.2, w * 0.5, (130, 90, 255), 40),
            (w * 0.2, h * 0.6, w * 0.6, (70, 160, 255), 30),
        ]:
            ld.ellipse([gx - gr, gy - gr, gx + gr, gy + gr], fill=col + (al,))
        img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
    elif style_id == "ink":
        # 宣纸底色 + 淡墨山峦 + 印章点
        img = _vertical_gradient(w, h, [(0.0, (252, 249, 240)), (1.0, (244, 238, 224))])
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        for i, yb in enumerate([h * 0.78, h * 0.92]):
            pts = [(0, yb)]
            for x in range(0, w + 240, 240):
                pts.append((x, yb - rng.randint(70, 260) * (0.3 + i * 0.7)))
            pts += [(w, h), (0, h)]
            ld.polygon(pts, fill=(40, 34, 28, 45 if i == 0 else 66))
        img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
        d = ImageDraw.Draw(img)
        d.ellipse([w * 0.76, h * 0.08, w * 0.76 + w * 0.11, h * 0.08 + w * 0.11], fill=(196, 62, 46))
        for _ in range(16):
            x, y = rng.randint(0, w), rng.randint(0, h // 3)
            d.ellipse([x, y, x + 3, y + 3], fill=(70, 62, 52))
    elif style_id == "sakura":
        img = _vertical_gradient(w, h, [
            (0.0, (255, 232, 240)), (0.55, (255, 244, 247)), (1.0, (250, 220, 232)),
        ])
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        ld.ellipse([cx - w * 0.7, cy - h * 0.55, cx + w * 0.5, cy + h * 0.55],
                   fill=(255, 190, 210, 70))
        for _ in range(90):
            x, y = rng.randint(0, w), rng.randint(0, h)
            pet = rng.randint(3, 9)
            col = (255, 200, 214, rng.randint(80, 190))
            ld.ellipse([x, y, x + pet, y + pet], fill=col)
        img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
    elif style_id == "morandi":
        img = _vertical_gradient(w, h, [(0.0, (240, 238, 233)), (1.0, (232, 230, 224))])
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        for (sx, sy, sw, col) in [
            (0.10, 0.13, 0.42, (183, 166, 158, 130)), (0.60, 0.2, 0.46, (158, 176, 176, 120)),
            (0.66, 0.72, 0.5, (188, 178, 152, 130)), (0.02, 0.84, 0.36, (162, 150, 168, 110)),
        ]:
            x0, y0 = int(sx * w), int(sy * h)
            rw, rh = int(sw * w), int(sw * w * 1.1)
            ld.rounded_rectangle([x0, y0, x0 + rw, y0 + rh], radius=int(rw * 0.25), fill=col)
        img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
    elif style_id == "paper":
        img = _vertical_gradient(w, h, [(0.0, (255, 252, 246)), (1.0, (248, 244, 236))])
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        for (sx, sy, swr, col, al) in [
            (0.06, 0.08, 0.5, (255, 214, 165), 90), (0.6, 0.16, 0.42, (170, 205, 190), 90),
            (0.7, 0.8, 0.55, (255, 180, 190), 80), (0.1, 0.62, 0.4, (165, 190, 220), 80),
        ]:
            x0, y0 = int(sx * w), int(sy * h)
            sw = int(swr * w)
            ld.rounded_rectangle([x0, y0, x0 + sw, y0 + int(sw * 0.75)], radius=60,
                                 fill=col + (al,))
        img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
    elif style_id == "minimal":
        img = _vertical_gradient(w, h, [(0.0, (250, 250, 252)), (1.0, (242, 244, 250))])
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        step = w // 5
        for x in range(0, w + step, step):
            ld.line([(x, 0), (x, h)], fill=(210, 214, 224, 80), width=1)
        for y in range(0, h + step, step):
            ld.line([(0, y), (w, y)], fill=(210, 214, 224, 80), width=1)
        ld.arc([w * 0.08, h * 0.12, w * 0.92, h * 0.5], 0, 360, fill=(170, 178, 200, 120), width=3)
        ld.line([w * 0.12, h * 0.78, w * 0.88, h * 0.78], fill=(170, 178, 200, 120), width=2)
        img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
    elif style_id == "tech":
        img = _vertical_gradient(w, h, [(0.0, (10, 16, 40)), (0.6, (14, 28, 66)), (1.0, (6, 12, 34))])
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        # 透视网格
        for k in range(0, 20):
            y = int(h * (k / 19) ** 1.25)
            ld.line([(0, y), (w, y)], fill=(60, 120, 255, 55), width=1)
        for k in range(-6, 7):
            x0 = w / 2 + k * w * 0.12
            ld.line([(x0, 0), (w / 2 + k * w * 0.3, h)], fill=(60, 120, 255, 45), width=1)
        img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        ld.ellipse([w * 0.66, h * 0.18, w * 0.66 + w * 0.5, h * 0.18 + w * 0.5],
                   fill=(0, 255, 220, 30))
        for _ in range(26):
            x, y = rng.randint(0, w), rng.randint(0, h)
            col = rng.choice([(0, 255, 220), (0, 160, 255), (255, 255, 255)])
            ld.ellipse([x, y, x + 5, y + 5], fill=col + (150,))
        img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
    elif style_id == "lime":
        img = _vertical_gradient(w, h, [
            (0.0, (228, 250, 214)), (0.5, (244, 252, 226)), (1.0, (206, 244, 190)),
        ])
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        for _ in range(70):
            x, y = rng.randint(0, w), rng.randint(0, h)
            r = rng.randint(10, 60)
            col = rng.choice([(255, 255, 255, 60), (180, 230, 120, 60), (250, 214, 120, 50)])
            ld.ellipse([x - r, y - r, x + r, y + r], fill=col)
        img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
    else:  # 兜底：经典渐变（同序卡片）
        top, bottom = _card_palette(rng.randint(0, 5))
        img = _vertical_gradient(w, h, [(0.0, top), (1.0, bottom)])
    return img


def render_background_preview(style_id: str, w: int = 240, h: int = 420) -> "Image.Image":
    """渲染一张背景小样（WebUI 风格选择器用）。"""
    return _render_background(w, h, style_id, "preview")


def _load_theme_image(theme: dict | None) -> "Image.Image | None":
    """主题指定了自定义背景图文件时载入并裁剪成 9:16 底图。"""
    from PIL import Image

    path = (theme or {}).get("image")
    if not path:
        return None
    try:
        src = Image.open(path).convert("RGB")
    except Exception as exc:  # noqa: BLE001
        log.warning("背景图加载失败（跳过，改用风格背景）: %s", exc)
        return None
    # cover 缩放裁切到 9:16
    tw, th = 1080, 1920
    s = max(tw / src.width, th / src.height)
    nw, nh = max(tw, int(src.width * s)), max(th, int(src.height * s))
    src = src.resize((nw, nh), Image.LANCZOS)
    x0, y0 = (nw - tw) // 2, (nh - th) // 2
    return src.crop((x0, y0, x0 + tw, y0 + th))


def _dim_bottom(img: "Image.Image", strength: int = 110) -> "Image.Image":
    """底部压暗（让白字在任意明亮背景上都可读）。"""
    from PIL import Image, ImageDraw

    w, h = img.size
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for y in range(h):
        t = y / h
        a = int(strength * max(0.0, t - 0.45) / 0.55)
        if a:
            d.line([(0, y), (w, y)], fill=(0, 0, 0, a))
    return Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")


def _text_pill(draw, x0: int, y0: int, x1: int, y1: int, alpha: int = 130) -> None:
    """半透明深色圆角底（提升文字对比度的"玻璃卡"）。"""
    draw.rounded_rectangle([x0, y0, x1, y1], radius=36, fill=(10, 8, 24, alpha))


def _compose_card(spec: CardSpec, index: int, output_dir: Path, theme: dict | None = None) -> Path:
    """合成一张竖屏图文卡片（1080x1920）。

    - theme 无/空 → 经典柔和渐变（旧版观感）；
    - theme.style → 程序化背景模板；theme.image → 自定义上传背景图（优先）。
    """
    from PIL import Image, ImageDraw

    w, h = 1080, 1920
    style_id = (theme or {}).get("style") or ""
    custom_img = _load_theme_image(theme)
    if custom_img is not None:
        img = _dim_bottom(custom_img, 120)
    elif style_id and style_id != "auto":
        img = _dim_bottom(_render_background(w, h, style_id, spec.text or "auto"), 96)
    else:
        img = _render_background(w, h, "", f"card-{index}")
        img = _dim_bottom(img, 70)

    # 文字与"玻璃卡"统一画在 RGBA 覆盖层上再合成（保证半透明填充可靠）
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font_big = MediaCreator._find_cjk_font(96)
    font_mid = MediaCreator._find_cjk_font(60)
    font_small = MediaCreator._find_cjk_font(40)

    if spec.kind == "cover":
        # 封面：居中玻璃卡 + 大标题 + 副标题
        lines = _wrap_text(spec.text, 8, font=font_big, max_px=920)
        line_h = 138
        block_h = len(lines) * line_h + (70 if spec.subtitle else 30)
        top0 = h // 2 - block_h // 2
        pad = 60
        max_w = max((draw.textbbox((0, 0), ln, font=font_big)[2] for ln in lines), default=200) + pad * 2
        bx0 = (w - max_w) // 2
        _text_pill(draw, bx0 - 20, top0 - pad + 30, bx0 + max_w + 20, top0 + block_h + pad - 10, alpha=110)
        y = top0 + 30
        for ln in lines:
            bbox = draw.textbbox((0, 0), ln, font=font_big)
            draw.text(((w - (bbox[2] - bbox[0])) // 2, y), ln, font=font_big, fill=(255, 255, 255))
            y += line_h
        if spec.subtitle:
            bbox = draw.textbbox((0, 0), spec.subtitle, font=font_mid)
            draw.text(((w - (bbox[2] - bbox[0])) // 2, y + 6), spec.subtitle, font=font_mid,
                      fill=(235, 235, 245))
    elif spec.kind == "ending":
        # 结尾：居中引导关注
        lines = _wrap_text(spec.text, 10, font=font_big, max_px=920)
        line_h = 128
        block_h = len(lines) * line_h + (70 if spec.subtitle else 20)
        pad = 60
        max_w = max((draw.textbbox((0, 0), ln, font=font_big)[2] for ln in lines), default=200) + pad * 2
        bx0 = (w - max_w) // 2
        top0 = h // 2 - block_h // 2 + 120
        _text_pill(draw, bx0 - 20, top0 - pad + 30, bx0 + max_w + 20, top0 + block_h + pad - 10, alpha=110)
        y = top0 + 30
        for ln in lines:
            bbox = draw.textbbox((0, 0), ln, font=font_big)
            draw.text(((w - (bbox[2] - bbox[0])) // 2, y), ln, font=font_big, fill=(255, 255, 255))
            y += line_h
        if spec.subtitle:
            bbox = draw.textbbox((0, 0), spec.subtitle, font=font_mid)
            draw.text(((w - (bbox[2] - bbox[0])) // 2, y + 8), spec.subtitle, font=font_mid,
                      fill=(235, 235, 245))
    else:
        # 内容卡：上部玻璃卡装文字，中下部大留白，页码沉底
        lines = _wrap_text(spec.text, 10, font=font_big, max_px=860)
        line_h = 116
        pad = 56
        max_w = max((draw.textbbox((0, 0), ln, font=font_big)[2] for ln in lines), default=200) + pad * 2
        bx0 = (w - max_w) // 2
        block_h = len(lines) * line_h
        top0 = 430
        _text_pill(draw, bx0 - 20, top0 - pad + 30, bx0 + max_w + 20, top0 + block_h + pad - 10, alpha=120)
        y = top0 + 28
        for ln in lines:
            bbox = draw.textbbox((0, 0), ln, font=font_big)
            draw.text(((w - (bbox[2] - bbox[0])) // 2, y), ln, font=font_big, fill=(255, 255, 255))
            y += line_h
        # 内容卡中央放一条主题装饰线
        draw.line([w * 0.3, top0 + block_h + pad + 30, w * 0.7, top0 + block_h + pad + 30],
                  fill=(255, 255, 255, 90), width=3)
        if spec.subtitle:
            bbox = draw.textbbox((0, 0), spec.subtitle, font=font_mid)
            draw.text(((w - (bbox[2] - bbox[0])) // 2, h - 210), spec.subtitle, font=font_mid,
                      fill=(255, 255, 255))

    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    path = output_dir / f"card_{index}_{spec.kind}_{int(time.time() * 1000)}.png"
    img.save(path, "PNG")
    return path


def _wrap_text(text: str, per_line: int, font=None, max_px: int = 960) -> list[str]:
    """按宽度换行：优先用字体实测宽度（中文字符宽度≈字号，但 emoji/西文更窄）。

    若 font 可用，按 max_px 宽度折行（避免"文字超出卡片宽度"）；
    否则按每行字数兜底（per_line 字符/行）。
    """
    if font is not None:
        lines: list[str] = []
        current = ""
        for ch in text:
            if font.getlength(current + ch) <= max_px:
                current += ch
            else:
                if current:
                    lines.append(current)
                current = ch
        if current:
            lines.append(current)
        return lines or [""]
    return [text[i : i + per_line] for i in range(0, len(text), per_line)] or [""]


async def generate_cards_impl(media: MediaCreator, draft, image_count: int = 4,
                              theme: dict | None = None) -> list[MediaAsset]:
    """根据草稿批量生成竖屏图文卡片（封面卡 + 内容卡 + 结尾卡）。

    theme: 视觉主题 {"style": 风格id, "image": 自定义背景图绝对路径}
    """
    specs: list[CardSpec] = []
    specs.append(CardSpec(text=draft.title[:12], subtitle=" ".join(draft.tags[:3]), kind="cover"))
    sentences = _split_sentences(draft.content)
    for i in range(max(1, image_count - 2)):
        idx = i % len(sentences)
        specs.append(CardSpec(text=sentences[idx], subtitle=f"{i + 2}/{image_count}", kind="content"))
    specs.append(CardSpec(text="关注我，不错过每一期精彩", subtitle="点赞 ♥ 收藏 ★ 评论 💬", kind="ending"))

    assets: list[MediaAsset] = []
    for i, spec in enumerate(specs[:image_count]):
        path = await asyncio.to_thread(_compose_card, spec, i, media._output_dir, theme)
        assets.append(MediaAsset(path=str(path), kind="image", width=1080, height=1920, provider="pillow"))
    log.info("已生成 %d 张图文卡片", len(assets))
    return assets


async def create_slideshow_video_impl(
    media: MediaCreator,
    images: list[str],
    output: str | None = None,
    music: str | None = None,
    duration_per_image: float = 3.0,
) -> MediaAsset:
    """把多张图片合成竖屏幻灯片视频（ffmpeg）。

    - 每张图展示 duration_per_image 秒，整体竖屏 1080x1920
    - music 为背景音乐路径（可选；不提供则无声视频）
    - 需要系统安装 ffmpeg
    """
    import shutil
    import subprocess

    if shutil.which("ffmpeg") is None:
        raise SlideshowError(
            "未找到 ffmpeg，请先安装：Windows 用 winget install Gyan.FFmpeg（或下载 ffmpeg.exe 加入 PATH）；"
            "Linux 用 apt install ffmpeg；macOS 用 brew install ffmpeg"
        )
    if not images:
        raise SlideshowError("至少需要 1 张图片")

    output = output or str(media._output_dir / f"slideshow_{int(time.time() * 1000)}.mp4")
    Path(output).parent.mkdir(parents=True, exist_ok=True)

    cmd = ["ffmpeg", "-y"]
    for img in images:
        cmd += ["-loop", "1", "-t", str(duration_per_image), "-i", img]
    has_music = bool(music) and Path(music).exists()
    if has_music:
        cmd += ["-i", music]

    n = len(images)
    parts = []
    for i in range(n):
        parts.append(
            f"[{i}:v]scale=1080:1920:force_original_aspect_ratio=decrease,"
            f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,"
            f"format=yuv420p[v{i}]"
        )
    concat_inputs = "".join(f"[v{i}]" for i in range(n))
    parts.append(f"{concat_inputs}concat=n={n}:v=1:a=0[vout]")
    cmd += ["-filter_complex", ";".join(parts), "-map", "[vout]"]
    if has_music:
        cmd += ["-map", f"{n}:a", "-c:a", "aac", "-shortest"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", output]

    log.info("ffmpeg 合成视频: %d 张图, 音乐=%s -> %s", n, "有" if has_music else "无", output)
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=240)
    except asyncio.TimeoutError:
        # 超时后主动杀掉 ffmpeg，避免僵尸进程继续吃 CPU/内存
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        log.error("ffmpeg 合成超过 240s 超时终止: %s", output)
        raise SlideshowError(
            "ffmpeg 合成超过 240 秒已终止（图片过大或编码卡住）。"
            "请检查输入图片尺寸/磁盘空间，或到 agent 容器里手动跑 ffmpeg 排查"
        ) from None
    if proc.returncode != 0:
        raise SlideshowError(f"ffmpeg 失败: {stderr.decode('utf-8', 'replace')[-500:]}")
    log.info("视频已生成: %s", output)
    return MediaAsset(path=str(output), kind="video", width=1080, height=1920, provider="ffmpeg")


def pick_music(music_dir: str | Path | None) -> str | None:
    """从音乐目录随机选一首（mp3/m4a/wav）；目录不存在或为空返回 None。"""
    if not music_dir:
        return None
    music_dir = Path(music_dir)
    if not music_dir.exists():
        return None
    files = [p for p in music_dir.iterdir() if p.suffix.lower() in {".mp3", ".m4a", ".wav", ".aac"}]
    if not files:
        return None
    return str(random.choice(files))


# 背景音乐说明：不再内置/合成任何 BGM，配乐一律使用用户在
# 「AI 运营工坊 → 素材库」上传的音乐（state/media/music 持久化）。


def media_root(settings: Settings, kind: str) -> Path:
    """素材持久化目录（state/media/<music|backgrounds>，agent 与 scheduler 共享卷）。"""
    root = Path(settings.state_dir) / "media" / kind
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_music(settings: Settings, cfg: dict | None) -> str | None:
    """任务视频配乐解析（只用用户上传的音乐）：
    1. cfg["music"] 指定文件名 → 在 state/media/music 与 assets/music 里找；
    2. 未指定时 legacy cfg["music_dir"] 目录里有歌 → 随机一首；
    3. 都没有 → 返回 None，合成无声视频。
    """
    music_root = media_root(settings, "music")
    cfg = cfg or {}
    want = (cfg.get("music") or "").strip()
    repo_music = Path(settings.state_dir).parent / "assets" / "music"
    if want:
        for base in (music_root, repo_music):
            p = base / want
            if p.exists():
                return str(p)
        log.warning("指定的 BGM 不存在（%s），请到 AI 工坊素材库上传", want)
    legacy = cfg.get("music_dir")
    if legacy:
        chosen = pick_music(legacy)
        if chosen:
            return chosen
    files = [p for p in music_root.iterdir() if p.suffix.lower() in {".mp3", ".m4a", ".wav", ".aac"}]
    return str(random.choice(files)) if files else None


def resolve_background(settings: Settings, cfg: dict | None) -> dict | None:
    """解析任务视觉主题配置 → media_creator.generate_cards 的 theme 参数。

    cfg["media"] 形如 {"style": "aurora", "image": "自拍.jpg"}；
    image 仅在 state/media/backgrounds 或 assets/backgrounds 找到时生效，否则退回风格。
    """
    media = (cfg or {}).get("media") or {}
    style = (media.get("style") or "").strip()
    image_name = (media.get("image") or "").strip()
    theme: dict = {}
    if image_name:
        for base in (media_root(settings, "backgrounds"),
                     Path(settings.state_dir).parent / "assets" / "backgrounds"):
            p = base / image_name
            if p.exists():
                theme["image"] = str(p)
                break
    theme["style"] = style or "auto"
    if not theme.get("image") and not theme.get("style"):
        return None
    return theme

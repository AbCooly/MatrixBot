"""Platform_Uploader 技能：跨平台发布的总入口。

职责：
  1. 维护适配器注册表（抖音/小红书/视频号/知乎/头条）
  2. 提供 publish / check_login 两个统一能力
  3. 任何异常封装为 SkillResult，不向上抛

说明：这是全项目最复杂的技能——平台改版会破坏发布选择器。
登录不在此处：一律由人工在「远程驾驶舱」完成，联调时先开驾驶舱登录，
再用 `python -m agent.main --verify` 验证 check_login。
"""
from __future__ import annotations

from typing import Any

from ..config import Settings
from ..logger import log
from ..models import PostPayload, PublishResult, SkillResult
from .base import Skill
from .uploader.base import AdapterRegistry
from .uploader.browser import BrowserPool
from .uploader.helpers import fit_text
from .uploader.douyin import DouyinAdapter
from .uploader.wechat_channels import WechatChannelsAdapter
from .uploader.xiaohongshu import XiaohongshuAdapter
from .uploader.zhihu import ZhihuAdapter
from .uploader.toutiao import ToutiaoAdapter


class PlatformUploader(Skill):
    """跨平台发布技能。"""

    name = "platform_uploader"
    description = "驱动浏览器在抖音/小红书/微信视频号发布图文或视频"

    def __init__(self, settings: Settings):
        self._settings = settings
        self.pool = BrowserPool(settings)
        self.registry = AdapterRegistry(self.pool)
        # 注册适配器（新增平台在此追加）
        self.registry.register(DouyinAdapter)
        self.registry.register(XiaohongshuAdapter)
        self.registry.register(WechatChannelsAdapter)
        self.registry.register(ZhihuAdapter)
        self.registry.register(ToutiaoAdapter)

    def _adapter(self, platform: str):
        adapter = self.registry.get(platform)
        if adapter is None:
            raise ValueError(f"未注册的平台: {platform}（可选: {self.registry.platforms()}）")
        return adapter

    # ---------------- 统一能力 ----------------
    async def run(self, action: str = "publish", **kwargs) -> SkillResult:
        """统一入口。

        Args:
            action: publish | check_login
        """
        try:
            if action == "publish":
                result = await self.publish(kwargs["payload"])
            elif action == "check_login":
                result = await self.check_login(kwargs["platform"])
            else:
                return SkillResult(success=False, error=f"未知 action: {action}")
        except ValueError as exc:
            return SkillResult(success=False, error=str(exc))
        return SkillResult(success=True, data=result)

    # ---------------- 具体方法 ----------------
    # 各平台「标题/首行」硬上限（超限会被平台拒绝或强制截断）
    _TITLE_LIMITS = {
        "xiaohongshu": 20,
        "douyin": 30,
        "toutiao": 30,
        "zhihu": 100,
        "wechat_channels": 200,
    }

    async def publish(self, payload: PostPayload, profile: str | None = None) -> PublishResult:
        """按载荷类型路由到对应适配器（profile 指定账号，None=默认账号）。"""
        # 发布前统一收敛标题长度（任何来源：AI 草稿 / 模拟测试 / WebUI 手动都安全）
        limit = self._TITLE_LIMITS.get(payload.platform)
        if limit and payload.title and len(payload.title) > limit:
            log.info("标题超限（%d>%d）自动截断 [%s]", len(payload.title), limit, payload.platform)
            payload.title = fit_text(payload.title, limit)
        adapter = self._adapter(payload.platform)
        log.info("开始发布 [%s%s] visibility=%s images=%d video=%s",
                 payload.platform, f"/{profile}" if profile else "",
                 payload.visibility, len(payload.images), bool(payload.video))
        if payload.video and adapter.capabilities.get("video"):
            result = await adapter.publish_video(payload, profile=profile)
        elif payload.images and adapter.capabilities.get("image"):
            result = await adapter.publish_image(payload, profile=profile)
        elif adapter.capabilities.get("article"):
            # 纯文本文章平台（知乎）：无视频/图片也能发
            result = await adapter.publish_article(payload, profile=profile)
        else:
            return PublishResult(payload.platform, False, "failed",
                                 "发布载荷与平台能力不匹配（缺少图片/视频，或平台不支持该类型）")
        log.info("发布结果 [%s]: %s - %s", payload.platform, result.status, result.message)
        return result

    async def check_login(self, platform: str, profile: str | None = None) -> bool:
        return await self._adapter(platform).check_login(profile=profile)

    async def close(self) -> None:
        """释放浏览器资源。"""
        await self.pool.close_all()

"""PlatformAdapter 抽象基类 + 适配器注册表。

设计借鉴 social_media_pubulish_MCP 的 PlatformAdapter 接口（Apache-2.0），
用 Python 重写并增强。

登录策略：
  登录一律由**人工**在「远程驾驶舱」里完成（扫码 / 拖滑块 / 收短信验证码）。
  平台验证码形态与风控策略频繁变动，自动登录选择器必然持续腐化，因此
  本模块只保留 check_login（登录态检测），不再承载任何自动登录流程。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from ...logger import log
from ...models import PostPayload, PublishResult

if TYPE_CHECKING:
    from .browser import BrowserPool


class PlatformAdapter(ABC):
    """单个社交平台的浏览器自动化适配器。"""

    platform: str = ""
    capabilities: dict = {}  # {"image": True, "video": False, "article": False}

    def __init__(self, pool: BrowserPool):
        self._pool = pool

    # ---- 登录态检测 ----
    @abstractmethod
    async def check_login(self, profile: str | None = None) -> bool:
        """判断当前持久化上下文是否已登录（登录动作由人工在驾驶舱完成）。"""

    # ---- 发布相关 ----
    async def publish_image(self, payload: PostPayload, profile: str | None = None) -> PublishResult:
        """发布图文（默认不支持，子类按需覆写）。"""
        return PublishResult(platform=self.platform, success=False, status="failed",
                             message="该平台暂不支持图文发布")

    async def publish_video(self, payload: PostPayload, profile: str | None = None) -> PublishResult:
        """发布视频（默认不支持，子类按需覆写）。"""
        return PublishResult(platform=self.platform, success=False, status="failed",
                             message="该平台暂不支持视频发布")

    async def publish_article(self, payload: PostPayload, profile: str | None = None) -> PublishResult:
        """发布文章/想法（纯文本，默认不支持，知乎等子类覆写）。"""
        return PublishResult(platform=self.platform, success=False, status="failed",
                             message="该平台暂不支持文章发布")


class AdapterRegistry:
    """平台名 → 适配器类的注册表（工厂模式）。"""

    def __init__(self, pool: BrowserPool):
        self._pool = pool
        self._adapters: dict[str, PlatformAdapter] = {}

    def register(self, adapter_cls: type[PlatformAdapter]) -> None:
        """注册适配器类（懒实例化）。"""
        instance = adapter_cls(self._pool)
        self._adapters[instance.platform] = instance
        log.info("已注册平台适配器: %s (capabilities=%s)", instance.platform, instance.capabilities)

    def get(self, platform: str) -> PlatformAdapter | None:
        return self._adapters.get(platform)

    def platforms(self) -> list[str]:
        return list(self._adapters)

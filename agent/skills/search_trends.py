"""Search_Trends 技能：热点搜寻。

数据源（按优先级，逐个降级）：
  1. 微博官方 ajax 接口（weibo.com/ajax/side/hotSearch，免登录 JSON）
  2. 百度热搜（top.baidu.com HTML 内嵌 JSON）
  3. 抖音热榜（vvhan 聚合 API，免费无 Key）
  4. 全部失败 → 返回空列表并记录错误（由 Task 层决定是否中止）

安全规则：所有请求带浏览器 UA；全部 try/except；超时兜底。
"""
from __future__ import annotations

import json
import re
from typing import Any

import httpx

from ..logger import log
from ..models import HotTopic, SkillResult
from .base import Skill

# 伪装浏览器 UA，降低被反爬拦截的概率
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_TIMEOUT = httpx.Timeout(15.0)


class SearchTrends(Skill):
    """热点搜寻：拉取微博/百度/抖音热搜。"""

    name = "search_trends"
    description = "获取当日热门话题与关键词（微博/百度/抖音热搜榜）"

    # 平台 → 数据源函数映射
    _SOURCES = {
        "weibo": "_fetch_weibo",
        "baidu": "_fetch_baidu",
        "douyin": "_fetch_douyin",
        "60s": "_fetch_60s",
    }

    async def run(
        self,
        platforms: list[str] | None = None,
        limit: int = 10,
        **_: Any,
    ) -> SkillResult:
        """获取热点。

        Args:
            platforms: 平台列表，默认全部 ["weibo", "baidu", "douyin", "60s"]
            limit: 每个平台最多取几条
        """
        targets = platforms or list(self._SOURCES)
        topics: list[HotTopic] = []
        errors: list[str] = []

        async with httpx.AsyncClient(
            headers={"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9"},
            timeout=_TIMEOUT,
            follow_redirects=True,
        ) as client:
            for platform in targets:
                fetcher = getattr(self, self._SOURCES.get(platform, ""), None)
                if fetcher is None:
                    errors.append(f"未知平台: {platform}")
                    continue
                try:
                    items = await fetcher(client, limit)
                    topics.extend(items)
                    log.info("热点源 [%s] 获取 %d 条", platform, len(items))
                except Exception as exc:  # noqa: BLE001 —— 单源失败不中断整体
                    errors.append(f"{platform}: {exc}")
                    log.warning("热点源 [%s] 失败: %s", platform, exc)

        ok = bool(topics) or not errors
        return SkillResult(
            success=ok,
            data=topics,
            error="; ".join(errors) if errors else None,
            meta={"sources": targets, "total": len(topics)},
        )

    # ---------------- 微博 ----------------
    async def _fetch_weibo(self, client: httpx.AsyncClient, limit: int) -> list[HotTopic]:
        """微博热搜：weibo.com/ajax/side/hotSearch 返回 JSON。"""
        resp = await client.get("https://weibo.com/ajax/side/hotSearch")
        resp.raise_for_status()
        data = resp.json()
        realtime = (data.get("data") or {}).get("realtime") or []
        out: list[HotTopic] = []
        for item in realtime[:limit]:
            word = (item.get("word") or "").strip()
            if not word:
                continue
            note = item.get("note") or ""
            out.append(
                HotTopic(
                    platform="weibo",
                    rank=item.get("rank", len(out) + 1),
                    title=note or word,
                    url=f"https://s.weibo.com/weibo?q=%23{word}%23",
                    heat=int(item.get("num") or 0),
                    category=item.get("category") or "",
                )
            )
        return out

    # ---------------- 百度 ----------------
    async def _fetch_baidu(self, client: httpx.AsyncClient, limit: int) -> list[HotTopic]:
        """百度热搜：top.baidu.com HTML 内嵌数据（Vue SSR 的 <!--s-data:{...}--> 注释）。

        说明：旧版页面用 <script id="app"> 内嵌 JSON，新版改为 HTML 注释，两种都兼容。
        """
        resp = await client.get("https://top.baidu.com/board?tab=realtime")
        resp.raise_for_status()
        html = resp.text
        data = None
        # 新版：<!--s-data:{...}-->
        match = re.search(r"<!--s-data:(.*?)-->", html, re.S)
        if match:
            try:
                data = json.loads(match.group(1).strip())
            except json.JSONDecodeError:
                data = None
        # 旧版：<script ... id="app">...</script>
        if data is None:
            match = re.search(r'<script[^>]*id="app"[^>]*>(.*?)</script>', html, re.S)
            if match:
                data = json.loads(match.group(1))
        if data is None:
            raise RuntimeError("百度热搜页面未找到内嵌数据（页面结构可能已改版）")
        cards = ((data.get("data") or {}).get("cards") or [])
        out: list[HotTopic] = []
        for card in cards:
            for item in (card.get("content") or [])[:limit]:
                word = (item.get("word") or "").strip()
                if not word:
                    continue
                out.append(
                    HotTopic(
                        platform="baidu",
                        rank=int(item.get("index") or len(out) + 1),
                        title=word,
                        url=item.get("url") or f"https://www.baidu.com/s?wd={word}",
                        heat=int(item.get("hotScore") or 0),
                        category=item.get("desc") or "",
                    )
                )
                if len(out) >= limit:
                    return out
        return out

    # ---------------- 抖音 ----------------
    async def _fetch_douyin(self, client: httpx.AsyncClient, limit: int) -> list[HotTopic]:
        """抖音热榜：vvhan 聚合 API（免费无 Key）。失败时由调用方降级。"""
        resp = await client.get("https://api.vvhan.com/api/hotlist/douyinHot")
        resp.raise_for_status()
        data = resp.json()
        # vvhan 结构: {success, data: [{title, url, hot, index, ...}]}
        items = (data.get("data") or [])[:limit]
        out: list[HotTopic] = []
        for i, item in enumerate(items, start=1):
            title = (item.get("title") or "").strip()
            if not title:
                continue
            out.append(
                HotTopic(
                    platform="douyin",
                    rank=int(item.get("index") or i),
                    title=title,
                    url=item.get("url") or "",
                    heat=_parse_heat(item.get("hot")),
                    category="",
                )
            )
        if not out:
            raise RuntimeError("抖音热榜接口返回空数据")
        return out

    # ---------------- 60s 每日速览（额外兜底源） ----------------
    async def _fetch_60s(self, client: httpx.AsyncClient, limit: int) -> list[HotTopic]:
        """60s 读懂世界（60s.viki.moe/v2/60s）：每日新闻速览，作为通用兜底源。"""
        resp = await client.get("https://60s.viki.moe/v2/60s")
        resp.raise_for_status()
        data = resp.json()
        news = ((data.get("data") or {}).get("news") or [])[:limit]
        out: list[HotTopic] = []
        for i, item in enumerate(news, start=1):
            if isinstance(item, dict):
                title = (item.get("title") or "").strip()
                url = item.get("url") or ""
            else:
                title, url = str(item).strip(), ""
            if not title:
                continue
            out.append(HotTopic(platform="60s", rank=i, title=title, url=url))
        if not out:
            raise RuntimeError("60s 数据源返回空数据")
        return out


def _parse_heat(value) -> int:
    """把 "10万"/"1.2亿"/"12345" 等热度字符串转成 int。"""
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value or "0").replace(",", "").strip()
    try:
        if s.endswith("万"):
            return int(float(s[:-1]) * 10_000)
        if s.endswith("亿"):
            return int(float(s[:-1]) * 100_000_000)
        return int(float(s))
    except ValueError:
        return 0


async def fetch_hot_topics(platforms: list[str] | None = None, limit: int = 10) -> list[HotTopic]:
    """便捷函数：供 Task 层直接调用（省略 SkillResult 包装）。"""
    skill = SearchTrends()
    result = await skill.execute(platforms=platforms, limit=limit)
    return result.data or []

"""Search_Trends 技能测试：用 FakeAsyncClient 注入假响应，不访问真实网络。"""
from __future__ import annotations

import json

from agent.skills import search_trends as mod
from agent.skills.search_trends import SearchTrends
from tests.conftest import FakeAsyncClient, FakeResponse

WEIBO_JSON = {
    "data": {
        "realtime": [
            {"word": "话题A", "note": "话题A详情", "rank": 1, "num": 12345, "category": "社会"},
            {"word": "话题B", "rank": 2, "num": 9000},
        ]
    }
}

BAIDU_HTML = (
    '<html><body><!--s-data:' + json.dumps(
        {
            "data": {
                "cards": [
                    {
                        "content": [
                            {"word": "百度话题1", "index": 1, "hotScore": "8888", "url": "http://x/1", "desc": "热点"},
                            {"word": "百度话题2", "index": 2},
                        ]
                    }
                ]
            }
        }
    ) + '--></body></html>'
)

BAIDU_HTML_OLD = (
    '<html><body><script type="application/json" id="app">'
    + json.dumps(
        {
            "data": {
                "cards": [
                    {
                        "content": [
                            {"word": "旧版话题1", "index": 1, "hotScore": "100", "url": "http://x/old"}
                        ]
                    }
                ]
            }
        }
    )
    + "</script></body></html>"
)

DOUYIN_JSON = {
    "success": True,
    "data": [
        {"title": "抖音话题1", "url": "https://t/1", "hot": "10万", "index": 1},
        {"title": "抖音话题2", "index": 2, "hot": "5万"},
    ],
}

NEWS60_JSON = {
    "code": 200,
    "data": {"news": [{"title": "60s新闻一", "url": "https://n/1"}, "纯文本新闻二"]},
}


def _routes() -> dict:
    return {
        "https://weibo.com/ajax/side/hotSearch": FakeResponse(json_data=WEIBO_JSON),
        "https://top.baidu.com/board?tab=realtime": FakeResponse(text=BAIDU_HTML),
        "https://api.vvhan.com/api/hotlist/douyinHot": FakeResponse(json_data=DOUYIN_JSON),
        "https://60s.viki.moe/v2/60s": FakeResponse(json_data=NEWS60_JSON),
    }


async def _run_with_client(skill: SearchTrends, client: FakeAsyncClient):
    """用 FakeAsyncClient 替换技能内部的 AsyncClient 并执行 run()。"""
    original = mod.httpx.AsyncClient

    class _FakeCtx:
        def __init__(self, c):
            self.c = c

        async def __aenter__(self):
            return self.c

        async def __aexit__(self, *a):
            return False

    mod.httpx.AsyncClient = lambda *a, **k: _FakeCtx(client)
    try:
        return await skill.run(limit=10)
    finally:
        mod.httpx.AsyncClient = original


class TestSearchTrends:
    async def test_weibo_parser(self):
        skill = SearchTrends()
        client = FakeAsyncClient({"https://weibo.com/ajax/side/hotSearch": FakeResponse(json_data=WEIBO_JSON)})
        topics = await skill._fetch_weibo(client, limit=10)
        assert len(topics) == 2
        assert topics[0].platform == "weibo"
        assert topics[0].title == "话题A详情"  # note 优先于 word
        assert topics[0].heat == 12345

    async def test_baidu_parser(self):
        skill = SearchTrends()
        client = FakeAsyncClient({"https://top.baidu.com/board?tab=realtime": FakeResponse(text=BAIDU_HTML)})
        topics = await skill._fetch_baidu(client, limit=10)
        assert topics[0].platform == "baidu"
        assert topics[0].title == "百度话题1"
        assert topics[0].heat == 8888

    async def test_baidu_parser_old_structure(self):
        """兼容旧版 <script id="app"> 结构。"""
        skill = SearchTrends()
        client = FakeAsyncClient({"https://top.baidu.com/board?tab=realtime": FakeResponse(text=BAIDU_HTML_OLD)})
        topics = await skill._fetch_baidu(client, limit=10)
        assert topics[0].title == "旧版话题1"

    async def test_douyin_parser_with_unit(self):
        """"10万" 应解析为 100000。"""
        skill = SearchTrends()
        client = FakeAsyncClient({"https://api.vvhan.com/api/hotlist/douyinHot": FakeResponse(json_data=DOUYIN_JSON)})
        topics = await skill._fetch_douyin(client, limit=10)
        assert topics[0].title == "抖音话题1"
        assert topics[0].heat == 100_000

    async def test_60s_parser(self):
        """60s 数据源：支持 dict 与纯文本混合的 news 列表。"""
        skill = SearchTrends()
        client = FakeAsyncClient({"https://60s.viki.moe/v2/60s": FakeResponse(json_data=NEWS60_JSON)})
        topics = await skill._fetch_60s(client, limit=10)
        assert len(topics) == 2
        assert topics[0].platform == "60s"
        assert topics[0].title == "60s新闻一"
        assert topics[1].title == "纯文本新闻二"

    async def test_run_all_sources(self):
        skill = SearchTrends()
        result = await _run_with_client(skill, FakeAsyncClient(_routes()))
        assert result.success
        assert len(result.data) == 8
        assert {t.platform for t in result.data} == {"weibo", "baidu", "douyin", "60s"}

    async def test_all_sources_fail(self):
        """全部数据源失败时返回 success=False 与空数据，不抛异常。"""
        skill = SearchTrends()
        result = await _run_with_client(skill, FakeAsyncClient({}, default=FakeResponse(status=500)))
        assert result.success is False
        assert result.data == []
        assert result.error

    async def test_partial_source_failure(self):
        """部分数据源失败时，其余源正常返回，success=True。"""
        skill = SearchTrends()
        routes = _routes()
        routes["https://api.vvhan.com/api/hotlist/douyinHot"] = FakeResponse(status=500)
        result = await _run_with_client(skill, FakeAsyncClient(routes))
        assert result.success is True
        assert {t.platform for t in result.data} == {"weibo", "baidu", "60s"}

    async def test_parse_heat(self):
        assert mod._parse_heat("10万") == 100_000
        assert mod._parse_heat("1.2亿") == 120_000_000
        assert mod._parse_heat("1,234") == 1234
        assert mod._parse_heat("abc") == 0
        assert mod._parse_heat(None) == 0

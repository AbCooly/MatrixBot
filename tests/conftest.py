"""pytest 公共夹具。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 保证 agent 包可导入
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    """无 Key 的测试配置，state 目录指向临时目录。"""
    from agent.config import Settings

    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("FAL_KEY", raising=False)
    monkeypatch.delenv("STABILITY_API_KEY", raising=False)
    return Settings(
        deepseek_api_key="",
        state_dir=tmp_path / "state",
    )


class FakeResponse:
    """模拟 httpx.Response 的最小实现。"""

    def __init__(self, text: str = "", json_data=None, status: int = 200):
        self._text = text
        self._json = json_data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json if self._json is not None else {}

    @property
    def text(self):
        return self._text

    @property
    def content(self):
        return self._text.encode("utf-8")


class FakeAsyncClient:
    """模拟 httpx.AsyncClient：按 URL 返回预置响应。"""

    def __init__(self, routes: dict[str, FakeResponse], default: FakeResponse | None = None):
        self.routes = routes
        self.default = default
        self.calls: list[str] = []

    async def get(self, url: str, **kwargs):
        self.calls.append(url)
        return self.routes.get(url, self.default or FakeResponse(status=404))

    async def post(self, url: str, **kwargs):
        self.calls.append(url)
        return self.routes.get(url, self.default or FakeResponse(status=404))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

"""config 与 LLM 解析工具测试。"""
from __future__ import annotations

import json

import pytest

from agent.config import (KEY_MASK_PREFIX, load_settings, llm_ready,
                          resolve_llm_provider, write_runtime_config)
from agent.llm_client import LLMError, DeepSeekClient


class TestConfig:
    def test_defaults(self, monkeypatch):
        """未设置任何环境变量时使用默认值（DeepSeek 兜底 provider）。"""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "")  # 隔离仓库根目录 .env 的真实 Key
        settings = load_settings()
        assert settings.deepseek_base_url == "https://api.deepseek.com"
        assert settings.deepseek_model == "deepseek-chat"
        # 无模型池配置 → 自动兜底一个 provider
        assert len(settings.llm_providers) == 1
        assert settings.llm_providers[0]["id"] == "default"
        assert settings.llm_active == "default"
        assert llm_ready(settings) is False

    def test_env_override(self, monkeypatch, tmp_path):
        """环境变量正确覆盖默认值。"""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        monkeypatch.delenv("STATE_DIR", raising=False)
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        monkeypatch.setenv("STATE_DIR", str(tmp_path / "st"))
        settings = load_settings()
        assert settings.deepseek_api_key == "sk-test"
        assert settings.log_level == "DEBUG"
        assert settings.state_dir.exists()
        # env 的 Key 进入兜底 provider → LLM 可用
        assert llm_ready(settings) is True

    def test_runtime_config_overrides(self, monkeypatch, tmp_path):
        """state/config_runtime.json（WebUI 写入）覆盖环境变量：模型池 + 密钥。"""
        state_dir = tmp_path / "state"
        monkeypatch.setenv("STATE_DIR", str(state_dir))
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env")
        write_runtime_config(state_dir, {
            "llm": {
                "active": "ds",
                "providers": [
                    {"id": "ds", "name": "DeepSeek", "kind": "openai",
                     "base_url": "https://api.deepseek.com", "api_key": "sk-rt", "model": "deepseek-chat"},
                    {"id": "qwen", "name": "通义", "kind": "openai",
                     "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "api_key": "", "model": "qwen-max"},
                ],
            },
            "fal_key": "fal-rt",
        })
        settings = load_settings()
        assert len(settings.llm_providers) == 2
        assert settings.llm_active == "ds"
        assert settings.fal_key == "fal-rt"
        prov = resolve_llm_provider(settings)
        assert prov is not None and prov["api_key"] == "sk-rt"
        # 非 active provider 的 key 是否保留由 pool 决定；ready 只看 active
        assert llm_ready(settings) is True

    def test_runtime_active_fallback_first_provider(self, monkeypatch, tmp_path):
        """active 指向不存在的 id 时回落到第一个 provider。"""
        state_dir = tmp_path / "state"
        monkeypatch.setenv("STATE_DIR", str(state_dir))
        write_runtime_config(state_dir, {
            "llm": {
                "active": "ghost",
                "providers": [
                    {"id": "a", "name": "A", "kind": "openai",
                     "base_url": "https://x", "api_key": "", "model": "m"},
                ],
            },
        })
        settings = load_settings()
        assert settings.llm_active == "a"

    def test_bool_parsing(self):
        from agent.config import _as_bool

        assert _as_bool("1", False) is True
        assert _as_bool("true", False) is True
        assert _as_bool("yes", False) is True
        assert _as_bool("on", False) is True
        assert _as_bool("0", True) is False
        assert _as_bool("", True) is True
        assert _as_bool(None, True) is True

    def test_runtime_file_roundtrip(self, tmp_path):
        """写入的运行时配置可被再次读取（原子写不破坏 JSON）。"""
        state_dir = tmp_path / "state"
        data = {"llm": {"active": "x", "providers": []}, "fal_key": "k1"}
        write_runtime_config(state_dir, data)
        from agent.config import read_runtime_config

        assert read_runtime_config(state_dir) == data


class TestParseJson:
    def test_plain_json(self):
        assert DeepSeekClient._parse_json('{"a": 1}') == {"a": 1}

    def test_markdown_fence(self):
        raw = '```json\n{"drafts": [{"title": "你好"}]}\n```'
        assert DeepSeekClient._parse_json(raw)["drafts"][0]["title"] == "你好"

    def test_surrounding_text(self):
        raw = '好的，这是结果：\n{"score": 9}\n希望对你有帮助'
        assert DeepSeekClient._parse_json(raw) == {"score": 9}

    def test_garbage_raises(self):
        with pytest.raises(LLMError):
            DeepSeekClient._parse_json("完全没有 JSON 的内容")

"""统一配置加载：.env 为底座，WebUI 可写的 state/config_runtime.json 运行时覆盖。

解析优先级（后者覆盖前者）：
  1. 默认值（见 Settings dataclass）
  2. 环境变量 / .env（deepseek/fal/stability/browser 等）
  3. state/config_runtime.json（WebUI「设置」写入的 API Key / 模型池 / 生图密钥）

为什么做两层：容器里 .env 不可热改，而 WebUI 要能改 Key、换模型；运行时文件
放在 state（共享卷），WebUI 改完写文件 + 重载 _settings 即全站生效。

安全规则：敏感信息只存 .env / 运行时文件，严禁硬编码。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from dotenv import load_dotenv

# WebUI「设置」可写的运行时配置文件（相对 state_dir）
RUNTIME_CONFIG_FILE = "config_runtime.json"

# 密钥遮蔽前缀：WebUI 回读时用来表示「有值但不回传明文」
KEY_MASK_PREFIX = "••••"


@dataclass(frozen=True)
class Settings:
    """应用配置（不可变，集中管理）。"""

    # LLM（环境默认/兜底；实际生效值见 llm_providers + llm_active）
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    # 生图（可选）
    fal_key: str = ""
    stability_api_key: str = ""
    # 浏览器（仅支持容器化 Browser-GUI 部署）
    # 浏览器守护地址：agent 与 browser-gui 共享网络栈，容器内填 http://127.0.0.1:9100
    browser_mgr_url: str = ""
    # noVNC 网页远程桌面地址（WebUI“远程桌面/人工接管”入口），如 http://<host>:6080/vnc.html
    novnc_url: str = ""
    log_level: str = "INFO"
    browser_timeout_ms: int = 30_000
    state_dir: Path = field(default_factory=lambda: Path("state"))
    # ---- 运行时模型池（由 load_settings 解析生成，勿手工构造） ----
    # 每个元素: {id,name,kind,base_url,api_key,model}
    llm_providers: tuple = ()
    # 默认使用的 provider id
    llm_active: str = ""


def _load_env() -> None:
    """加载项目根目录的 .env（不存在则静默跳过）。"""
    root = Path(__file__).resolve().parent.parent
    load_dotenv(root / ".env")


def _as_bool(value: str | None, default: bool) -> bool:
    """宽松地把字符串转布尔：'1/true/yes/on' 视为 True。"""
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# ---------------- 运行时配置（WebUI 可写） ----------------
def runtime_config_path(state_dir: Path) -> Path:
    return Path(state_dir) / RUNTIME_CONFIG_FILE


def read_runtime_config(state_dir: Path) -> dict:
    """读取运行时配置；文件不存在/损坏时返回空 dict。"""
    path = runtime_config_path(state_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning("运行时配置解析失败（%s），忽略", path)
        return {}


def write_runtime_config(state_dir: Path, data: dict) -> None:
    """写入运行时配置（保证原子性：先写临时文件再改名）。"""
    path = runtime_config_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp.replace(path)


def mask_key(key: str) -> str:
    """展示用掩码：只留后 4 位，前部用 •。"""
    if not key:
        return ""
    if len(key) <= 6:
        return KEY_MASK_PREFIX + key[-2:]
    return KEY_MASK_PREFIX + key[-4:]


def _seed_default_provider(settings: "Settings") -> list[dict]:
    """无运行时模型池时，用 .env 的 DeepSeek 配置兜底成一个 provider。"""
    return [{
        "id": "default",
        "name": "DeepSeek（.env 配置）",
        "kind": "openai",
        "base_url": settings.deepseek_base_url,
        "api_key": settings.deepseek_api_key,
        "model": settings.deepseek_model,
    }]


def _resolve_llm(settings: "Settings", runtime: dict) -> tuple[tuple, str]:
    """根据运行时配置 + 环境变量解析出 (providers, active_id)。"""
    llm_cfg = runtime.get("llm") if isinstance(runtime.get("llm"), dict) else {}
    raw = llm_cfg.get("providers")
    if isinstance(raw, list) and raw:
        providers = []
        for p in raw:
            if not isinstance(p, dict):
                continue
            pid = str(p.get("id") or "").strip()
            if not pid:
                continue
            providers.append({
                "id": pid,
                "name": str(p.get("name") or pid).strip(),
                "kind": str(p.get("kind") or "openai").strip() or "openai",
                "base_url": str(p.get("base_url") or "").strip(),
                "api_key": str(p.get("api_key") or "").strip(),
                "model": str(p.get("model") or "").strip(),
            })
        if providers:
            active = str(llm_cfg.get("active") or "").strip()
            if active not in {p["id"] for p in providers}:
                active = providers[0]["id"]
            return tuple(providers), active
    # 无运行时池 → 环境默认
    providers = _seed_default_provider(settings)
    return tuple(providers), providers[0]["id"]


def resolve_llm_provider(settings: "Settings") -> dict | None:
    """当前生效的 LLM provider（含 key）。没有可用 provider 时返回 None。"""
    for p in settings.llm_providers:
        if p.get("id") == settings.llm_active:
            return p
    if settings.llm_providers:
        return settings.llm_providers[0]
    return None


def llm_ready(settings: "Settings") -> bool:
    """当前是否配置了可用的 LLM（存在带 API Key 的生效 provider）。

    兼容直接构造 Settings（不经 load_settings、无模型池）的调用方：
    只要 deepseek_api_key 非空即视为可用。
    """
    prov = resolve_llm_provider(settings)
    if prov and prov.get("api_key"):
        return True
    return bool(settings.deepseek_api_key)


def load_settings() -> Settings:
    """从环境变量构造配置；无 DEEPSEEK key 时仅告警不报错（离线模式）。"""
    _load_env()

    settings = Settings(
        deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", "").strip(),
        deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip(),
        deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip(),
        fal_key=os.getenv("FAL_KEY", "").strip(),
        stability_api_key=os.getenv("STABILITY_API_KEY", "").strip(),
        browser_mgr_url=os.getenv("BROWSER_MGR_URL", "").strip(),
        novnc_url=os.getenv("NOVNC_URL", "").strip(),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        browser_timeout_ms=int(os.getenv("BROWSER_TIMEOUT_MS", "30000")),
        state_dir=Path(os.getenv("STATE_DIR", "state")),
    )
    settings.state_dir.mkdir(parents=True, exist_ok=True)

    # 运行时配置覆盖：模型池 / 生图密钥
    runtime = read_runtime_config(settings.state_dir)
    providers, active = _resolve_llm(settings, runtime)
    fal_key = str(runtime.get("fal_key") or "").strip() or settings.fal_key
    stability_key = str(runtime.get("stability_api_key") or "").strip() or settings.stability_api_key
    settings = replace(
        settings,
        llm_providers=providers,
        llm_active=active,
        fal_key=fal_key,
        stability_api_key=stability_key,
    )

    if not llm_ready(settings):
        logging.getLogger(__name__).warning(
            "未配置任何 LLM API Key（文案生成与审核将不可用）。"
            "可到 WebUI「设置」填写，或在 .env 设置 DEEPSEEK_API_KEY。"
        )
    return settings

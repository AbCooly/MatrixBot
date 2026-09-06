"""AI 运营工坊：按一句话需求生成「运营定制包」，并一键落地为定时任务。

能力范围（对应 WebUI「AI 工坊」页）：
  - generate_pack(): 预置行业运营提示词，把用户的一句话需求扩写成结构化定制包
  - 定制包（pack）字段与 content_generator/custom_content.yaml 对齐，
    便于直接作为 daily_post 任务的 custom_content 内联配置使用；
  - 素材库：内置背景模板（CARD_BG_STYLES）+ 自定义背景图 + 用户上传的 BGM；
  - packs 模板库：state/studio_packs.json 持久化，可复用、可删除。

约定：所有素材路径都基于 settings.state_dir/media/<music|backgrounds>（共享卷持久化）。
背景模板为纯算法离线生成；背景音乐不内置、不合成，一律由用户上传。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from .config import Settings
from .llm_client import LLMError, LLMClient
from .logger import log
from .skills.media_creator import CARD_BG_STYLES, media_root

# 常见的"一句话需求"示例（前端展示为可点击填充的行业预设）
INDUSTRY_PRESETS: list[dict] = [
    {
        "label": "新中式茶馆",
        "brief": "我在老城区开了一家新中式茶馆，主营冷泡茶、茶饮和茶器文创，客群是 20-40 岁注重体验的女性。想结合节气、国风和本地热点把线上流量引到店里，节假日做活动。",
    },
    {
        "label": "家庭烘焙工作室",
        "brief": "我的家庭烘焙工作室主打低糖手作蛋糕和节气礼盒，靠私域复购。想在抖音和小红书持续输出，结合节日热点涨粉并接单。",
    },
    {
        "label": "社区咖啡馆",
        "brief": "社区咖啡馆，卖咖啡+简餐，附近有大学和白领。希望用热点话题（如晒猫、自习、新店打卡）做内容引流到店消费。",
    },
    {
        "label": "美妆集合店",
        "brief": "线上美妆集合店主做平价国货护肤测评与开箱，想蹭明星同款、换季护肤等热点稳定出爆款。",
    },
    {
        "label": "健身私教工作室",
        "brief": "小型健身私教，主打减脂塑形与体态纠正，目标 25-40 岁城市白领。想结合健康/减肥类热点做科普涨粉，线上约课。",
    },
    {
        "label": "宠物用品店",
        "brief": "宠物零食与玩具店，想用萌宠日常、人宠互动等热点内容建立人设，在抖音快手带货。",
    },
    {
        "label": "汉服/文创店",
        "brief": "汉服与国风文创品牌，粉丝多为 Z 世代，想结合传统节日、影视剧国风热点做话题营销并上新种草。",
    },
    {
        "label": "本地水果/农特产",
        "brief": "卖本地果园直发水果与农特产的店，想结合时令、助农与美食热点做内容，把直播间流量转化到私域。",
    },
]

_SYS_PROMPT = """你是一位同时精通「新媒体矩阵运营」「平台流量算法」「内容合规」的资深运营顾问。
用户只会用一句大白话描述他的生意和诉求，你需要把它拆解成一份可直接执行、可交给 AI 排版发布系统落地的「运营定制包」。

务必遵守以下规范：
1. 只输出一个 JSON 对象，不要任何解释、前后缀或 Markdown。
2. JSON 结构严格如下（字段一个都不能少，数组至少 3 项）：
{
  "pack_name": "≤12字的中文档案名，好记好改，如『山野茶事·国风茶馆』",
  "account": {
    "persona": "账号人设一句话（含称呼），如『主理人阿茶』",
    "tone": "统一文风/口吻：温和分享型/干货科普型/活泼种草型……再附 2 句范例语气",
    "fixed_hashtags": ["3-6个固定话题标签，含 # 符号"],
    "taboos": ["2-4条绝对不能碰的内容红线（含平台明令禁止项）"]
  },
  "custom_materials": [
    {"name": "素材名称", "type": "卖点|知识|故事|热点结合|人设|互动|产品展示|探店|答疑", "detail": "一段可直接改写的底稿要点（150字内），注意口语化、有钩子"}
  ],
  "columns": [
    {"name": "栏目名", "cadence_hint": "周几或什么场景更新", "purpose": "栏目目的（种草/涨粉/转化/人设）"}
  ],
  "mix": {"hot": 热点素材占比(0-100整数), "custom": 自有素材占比(0-100整数)},
  "media_style": {
    "bg_style": "推荐的一个背景风格id，从下面风格表里选：aurora 极光渐变 / galaxy 深空星云 / ink 新中式水墨 / sakura 樱花粉调 / morandi 莫兰迪色块 / paper 奶油杂志 / minimal 极简线条 / tech 科技霓虹 / lime 柠檬气泡",
    "bg_reason": "为什么用这个风格的 1 句话",
    "music_vibe": "从 ambient 舒缓氛围 / piano 钢琴静夜 / chinese 国风古韵 / upbeat 轻快明亮 / none 选一个最贴合账号调性的",
    "poster_tone": "卡片文案配色的整体气质，如『留白多一点，高级感』"
  },
  "publishing": {
    "best_times": ["1-3个建议发布时间点，格式 '周三 20:00' 之类"],
    "cadence": "建议更新频率，如『一周 3-4 条』",
    "advice": ["2-4条针对本行业/本账号的运营建议，含蹭热点与流量维护的具体打法"]
  },
  "compliance": ["2-3条本行业合规提醒：如食品行业不得宣传功效，广告要标注等"]
}
3. 内容必须贴合用户描述的行业/门店/人群/目的，禁止泛泛而谈；热点结合要落到这个行业能用的场景。
4. 涉及医疗、金融、食品功效等表述一律收敛为「普通生活分享」，不得承诺效果、不得绝对化用语。
"""


# ---------------- 生成 ----------------
def generate_pack(settings: Settings, brief: str, platforms: list[str], goal: str = "") -> dict:
    """把一句话需求扩写成「运营定制包」JSON（已规整/纠错）。"""
    if not (brief or "").strip():
        raise ValueError("请先描述你的店铺/行业和需求")
    llm = LLMClient(settings)
    if not llm.available:
        raise LLMError(
            "尚未配置可用的模型密钥：请先到「设置 → 模型与密钥」为默认模型填写 API Key"
        )
    platform_txt = ("、".join(platforms)) if platforms else "由 AI 判断的主流平台"
    user_prompt = (
        f"我的情况（一句话需求）：{brief.strip()}\n\n"
        f"计划运营的平台：{platform_txt}\n"
        f"运营目标：{goal.strip() if goal.strip() else '涨粉 + 引流到店/到户 + 稳定出内容'}\n\n"
        "请按系统要求输出完整 JSON。"
    )
    raw = llm.chat_json(_SYS_PROMPT, user_prompt, temperature=0.5, max_tokens=3200)
    return normalize_pack(raw)


def normalize_pack(pack: Any) -> dict:
    """规整 LLM 返回，保证后续渲染/落库字段齐全、类型正确。"""
    if not isinstance(pack, dict):
        raise ValueError("AI 未返回有效结构，请重试")
    account = pack.get("account") or {}
    media_style = pack.get("media_style") or {}
    publishing = pack.get("publishing") or {}
    bg_style = str(media_style.get("bg_style") or "aurora")
    valid_styles = {s["id"] for s in CARD_BG_STYLES}
    if bg_style not in valid_styles:
        # 尝试从 id 里模糊匹配，匹配不上回退 aurora
        for s in CARD_BG_STYLES:
            if s["id"] in bg_style or bg_style in s["name"]:
                bg_style = s["id"]
                break
        else:
            bg_style = "aurora"
    # 音乐不再内置：music_vibe 仅作为“适合什么气质的音乐”的口味提示（纯文案，不映射文件）
    music_vibe = str(media_style.get("music_vibe") or "")[:24]
    return {
        "pack_name": str(pack.get("pack_name") or "我的定制包")[:20],
        "account": {
            "persona": str(account.get("persona") or ""),
            "tone": str(account.get("tone") or ""),
            "fixed_hashtags": _as_str_list(account.get("fixed_hashtags")),
            "taboos": _as_str_list(account.get("taboos")),
        },
        "custom_materials": _materials(pack.get("custom_materials")),
        "columns": _columns(pack.get("columns")),
        "mix": {
            "hot": int(pack.get("mix", {}).get("hot") or 60) if pack.get("mix", {}).get("hot") is not None else 60,
            "custom": int(pack.get("mix", {}).get("custom") or 40) if pack.get("mix", {}).get("custom") is not None else 40,
        },
        "media_style": {
            "bg_style": bg_style,
            "bg_reason": str(media_style.get("bg_reason") or ""),
            "music_vibe": music_vibe,
            "poster_tone": str(media_style.get("poster_tone") or ""),
        },
        "publishing": {
            "best_times": _as_str_list(publishing.get("best_times")),
            "cadence": str(publishing.get("cadence") or ""),
            "advice": _as_str_list(publishing.get("advice")),
        },
        "compliance": _as_str_list(pack.get("compliance")),
    }


def _as_str_list(v: Any) -> list[str]:
    if not v:
        return []
    if isinstance(v, str):
        return [x.strip() for x in re.split(r"[,，;；\n]+", v) if x.strip()]
    out = []
    for x in v:
        s = str(x).strip()
        if s:
            out.append(s)
    return out


def _materials(v: Any) -> list[dict]:
    if not isinstance(v, list):
        return []
    out = []
    for m in v[:12]:
        if not isinstance(m, dict):
            continue
        out.append({
            "name": str(m.get("name") or "素材"),
            "type": str(m.get("type") or "卖点"),
            "detail": str(m.get("detail") or ""),
        })
    return out


def _columns(v: Any) -> list[dict]:
    if not isinstance(v, list):
        return []
    out = []
    for c in v[:8]:
        if not isinstance(c, dict):
            continue
        out.append({
            "name": str(c.get("name") or "栏目"),
            "cadence_hint": str(c.get("cadence_hint") or ""),
            "purpose": str(c.get("purpose") or ""),
        })
    return out


def pack_to_custom_content(pack: dict) -> dict:
    """定制包 → daily_post 的 custom_content 内联配置（与 custom_content.yaml 对齐）。"""
    account = pack.get("account") or {}
    return {
        "persona": account.get("persona") or "",
        "tone": account.get("tone") or "",
        "custom_materials": pack.get("custom_materials") or [],
        "columns": pack.get("columns") or [],
        "mix": pack.get("mix") or {"hot": 60, "custom": 40},
        "fixed_hashtags": account.get("fixed_hashtags") or [],
        "exclude_topics": account.get("taboos") or [],
        "compliance": pack.get("compliance") or [],
    }


def task_payload_from_pack(pack: dict, options: dict) -> dict:
    """定制包 + 调度选项 → 可写入 tasks.yaml 的任务负载。

    options: {platforms, drafts, visibility, mode, image_count, music_file,
              bg_style, cadence_time("HH:MM"), note}
    """
    media_style = pack.get("media_style") or {}
    bg_style = options.get("bg_style") or media_style.get("bg_style") or "aurora"
    bg_file = str(options.get("bg_file") or "")
    # BGM 只用用户上传到素材库的文件（music_file）；未选择则无声，不做内置音乐
    music_file = str(options.get("music_file") or "")
    payload: dict = {
        "platforms": [p for p in (options.get("platforms") or []) if p] or ["xiaohongshu"],
        "drafts": int(options.get("drafts") or 3),
        "visibility": options.get("visibility") or "private",
        "mode": options.get("mode") or "slideshow",
        "image_count": int(options.get("image_count") or 4),
        "music": music_file,
        "media": {"style": bg_style, "image": bg_file},
        "custom_content": pack_to_custom_content(pack),
    }
    return payload


# ---------------- 定制包模板库（state/studio_packs.json） ----------------
def packs_path(settings: Settings) -> Path:
    return Path(settings.state_dir) / "studio_packs.json"


def list_packs(settings: Settings) -> list[dict]:
    try:
        return json.loads(packs_path(settings).read_text(encoding="utf-8")) if packs_path(settings).exists() else []
    except Exception:  # noqa: BLE001
        return []


def save_pack(settings: Settings, entry: dict) -> dict:
    packs = list_packs(settings)
    now = time.strftime("%Y-%m-%d %H:%M")
    if entry.get("id"):
        for i, p in enumerate(packs):
            if p.get("id") == entry["id"]:
                merged = {**p, **entry, "updated": now}
                packs[i] = merged
                break
        else:
            return save_pack(settings, {**entry, "id": ""})
    else:
        base = re.sub(r"[^a-z0-9]+", "-", (entry.get("name") or "pack").lower()).strip("-")
        entry["id"] = base[:36] or ("pk" + format(int(time.time() * 1000), "x"))
        entry["created"] = now
        entry["updated"] = now
        entry["brief"] = entry.get("brief") or ""
        entry["platforms"] = entry.get("platforms") or []
        entry["goal"] = entry.get("goal") or ""
        packs.append(entry)
    packs_path(settings).write_text(
        json.dumps(packs, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return entry


def delete_pack(settings: Settings, pack_id: str) -> bool:
    packs = list_packs(settings)
    left = [p for p in packs if p.get("id") != pack_id]
    if len(left) == len(packs):
        return False
    packs_path(settings).write_text(json.dumps(left, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


# ---------------- 素材库清单 ----------------
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
_AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".aac", ".flac"}


def list_media(settings: Settings) -> dict:
    """返回素材清单：内置背景风格 + 自定义背景图 + 用户上传的音乐。"""
    bg_root = media_root(settings, "backgrounds")
    mu_root = media_root(settings, "music")

    styles = [{"id": s["id"], "name": s["name"], "desc": s["desc"], "kind": "style"}
              for s in CARD_BG_STYLES]
    backgrounds = [{"file": p.name, "kind": "image", "size": p.stat().st_size}
                   for p in sorted(bg_root.iterdir()) if p.is_file() and p.suffix.lower() in _IMAGE_EXTS]
    music = [{"file": p.name, "name": p.stem, "kind": "upload", "size": p.stat().st_size}
             for p in sorted(mu_root.iterdir())
             if p.is_file() and p.suffix.lower() in _AUDIO_EXTS]
    return {"styles": styles, "backgrounds": backgrounds, "music": music}


def _safe_name(filename: str) -> str:
    name = Path(filename or "").name.strip().replace(" ", "_")
    if not name or name in {".", ".."}:
        raise ValueError("无效文件名")
    return name


def save_upload(settings: Settings, kind: str, filename: str, data: bytes) -> dict:
    if kind not in {"music", "backgrounds"}:
        raise ValueError("不支持的上传类型")
    name = _safe_name(filename)
    ext = Path(name).suffix.lower()
    allow = _AUDIO_EXTS if kind == "music" else _IMAGE_EXTS
    if ext not in allow:
        raise ValueError(f"格式不支持：{ext or '未知'}，允许 {','.join(sorted(allow))}")
    if len(data) > 80 * 1024 * 1024:
        raise ValueError("文件超过 80MB")
    dest = media_root(settings, kind) / name
    dest.write_bytes(data)
    log.info("素材上传成功: %s/%s (%d bytes)", kind, name, len(data))
    return {"kind": kind, "file": name, "size": len(data)}


def delete_upload(settings: Settings, kind: str, name: str) -> bool:
    if kind not in {"music", "backgrounds"}:
        return False
    name = _safe_name(name)
    mu_root = media_root(settings, "music")
    target = (mu_root if kind == "music" else media_root(settings, "backgrounds")) / name
    if target.exists():
        target.unlink()
        return True
    return False

"""全部数据模型（dataclass）。Skill/Task/Scheduler 之间只通过这些模型通信（见 docs/API.md）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------- 热点 ----------------
@dataclass
class HotTopic:
    """热搜词条。"""

    platform: str      # "weibo" | "baidu" | "douyin"
    rank: int          # 榜单排名（1 起）
    title: str         # 热搜词条
    url: str = ""      # 详情链接
    heat: int = 0      # 热度值（无则 0）
    category: str = ""  # 分类（如 娱乐/社会）


# ---------------- 内容草稿 ----------------
@dataclass
class ContentDraft:
    """平台化文案草稿。"""

    platform: str      # "xiaohongshu" | "douyin" | "wechat_channels"
    title: str
    content: str
    tags: list[str] = field(default_factory=list)
    image_prompt: str = ""     # 配图提示词（交给 Media_Creator）
    topic_ref: str = ""        # 关联热点标题（溯源）
    score: float = 0.0         # 自审得分 0-10
    review_note: str = ""      # 自审理由


# ---------------- 素材资产 ----------------
@dataclass
class MediaAsset:
    """生成的图片/视频资产。"""

    path: str          # 本地绝对路径
    kind: str = "image"  # "image" | "video"
    width: int = 0
    height: int = 0
    provider: str = ""   # "flux" | "stability" | "pillow"


# ---------------- 发布 ----------------
@dataclass
class PostPayload:
    """一次跨平台发布的完整载荷。"""

    platform: str      # "douyin" | "xiaohongshu" | "wechat_channels"
    title: str
    content: str
    tags: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)  # 本地图片绝对路径
    video: str | None = None                          # 本地视频绝对路径（可选）
    visibility: str = "private"   # "public" | "private" | "friends"
    scheduled_at: str | None = None  # ISO 时间（平台支持时生效）


@dataclass
class PublishResult:
    """发布结果。status: published | draft_created | login_required | verification_required | failed"""

    platform: str
    success: bool
    status: str = "failed"
    message: str = ""
    url: str | None = None


# ---------------- 评论与回复 ----------------
@dataclass
class Comment:
    """一条粉丝评论。"""

    platform: str
    comment_id: str
    user_name: str
    content: str
    like_count: int = 0
    created_at: str = ""
    note_url: str = ""


@dataclass
class ReplyResult:
    """一次回复的结果。"""

    comment: Comment
    reply_text: str = ""
    success: bool = False
    error: str = ""


# ---------------- Skill 统一结果 ----------------
@dataclass
class SkillResult:
    """Skill 的统一返回值：任何异常都被封装，不向上抛裸异常。"""

    success: bool
    data: Any = None
    error: str | None = None
    meta: dict = field(default_factory=dict)


# ---------------- 任务报告 ----------------
@dataclass
class TaskReport:
    """Task 执行报告，落盘 logs/reports/<task>_<date>.json。"""

    task_name: str
    started_at: str
    finished_at: str
    success: bool
    steps: list[dict] = field(default_factory=list)  # [{step, status, detail, took_ms}]
    error: str = ""

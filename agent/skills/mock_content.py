"""无 Key 模拟图文生成器：为发布链路测试提供零 token 消耗的假内容。

背景
  账号矩阵刚开始接平台时，若还没配 DeepSeek/Stability/Flux token，无法生成
  AI 文案与配图。本模块用本地模板池 + Pillow 卡片（media_creator 的降级链）
  生成带 [测试] 水印的确定性假内容，专用于打通"登录→建素材→发布→状态回收"
  整条链路，方便在各平台后台定位并删除测试内容。

设计
  - 每个平台一套标题/正文模板 + 话题池
  - 标题含当天日期，正文尾部附时间戳水印，保证每次内容不重复、可溯源
  - 同一平台同一天内容确定（模板按日轮转），重试不产生重复测试内容
"""
from __future__ import annotations

import random
from datetime import datetime

from ..models import ContentDraft

# 标题池（可复用；拼接日期后天然唯一）
_TITLES = {
    "douyin": ["一条普通工作日的记录", "把周末过成小假期", "最近在认真生活", "整理房间也整理心情"],
    "xiaohongshu": ["通勤路上发现的 3 个细节", "最近在小红书看到的神仙好物", "出租屋改造第 7 天", "周末小城漫游指南"],
    "wechat_channels": ["晚间随想：慢慢来的生活", "一分钟看完今天的花", "下雨天适合做什么", "生活切片：路边摊的烟火气"],
    "zhihu": ["如何看待「慢慢来也是一种选择」？", "你有哪些坚持了很久的小习惯？", "普通人的一天能有多治愈？", "怎样让生活多一点确定性？"],
    "toutiao": ["AI 时代，普通人如何保持竞争力？", "周末短途游的正确打开方式", "一个观察：城市里的小确幸", "聊聊近期生活中的小变化"],
}

_CONTENT_HEAD = {
    "douyin": "记录一下今天。没有宏大叙事，就是些细碎的光。",
    "xiaohongshu": "这里是模拟测试内容，主要用来验证发布链路是否打通。\n如果你看到这条，说明小红书适配器链路正常。",
    "wechat_channels": "这是一条视频号链路测试内容。\n如果你在视频号看到了这条视频，说明发布链路已经打通。",
    "zhihu": "先声明：本条为系统链路测试内容，用于验证知乎发布通道，请勿当真。\n下面是正文占位：",
    "toutiao": "本条为头条链路测试内容，用于验证微头条/图文发布通道。",
}

_CONTENT_BODY = [
    "早上出门前把窗开了十分钟，回来时屋里带着风的味道。"
    "通勤路上耳机里正好放到喜欢的歌，感觉一整天都有了节奏。",
    "整理了一下午抽屉，扔掉了三年没碰过的东西，房间变轻了，心情也是。"
    "原来断舍离不只是整理物品，也是在给生活腾位置。",
    "楼下新开了一家早餐店，老板记得每一个熟客的口味。"
    "这种具体而微的确定性，会让人觉得日子是可以信任的。",
    "傍晚去河边走了走，风很轻，天是渐变色的。"
    "手机拍不出十分之一的颜色，但眼睛记住了就够。",
]

_TOPIC_TAGS = ["测试", "记录", "生活", "随记"]


def make_mock_draft(platform: str) -> ContentDraft:
    """生成一份带 [测试] 标记 + 时间戳的模拟草稿（确定性、零 token）。"""
    now = datetime.now()
    day_no = now.toordinal()  # 自公历 1 年 1 月 1 日以来天数（每日轮换）
    titles = _TITLES.get(platform, _TITLES["toutiao"])
    title = titles[day_no % len(titles)]
    # 标题带日期，保证平台侧可识别且当天重试不重复
    stamp = now.strftime("%m-%d")
    title = f"[测试] {title} · {stamp}"

    heads = _CONTENT_HEAD[platform]
    bodies = _CONTENT_BODY
    # 正文：头 + 1~2 段正文 + 时间戳水印，保证足够长可上卡片
    start = day_no % len(bodies)
    body_pick = [bodies[(start + i) % len(bodies)] for i in range(1 + (day_no % 2))]
    content = "\n\n".join([heads, *body_pick])
    content += f"\n（模拟内容 {now.strftime('%Y-%m-%d %H:%M')}，链路测试专用）"

    tags = list(_TOPIC_TAGS)
    if "测试" not in content:
        content = f"[链路测试]\n{content}"

    return ContentDraft(
        platform=platform,
        title=title,
        content=content,
        tags=tags,
        image_prompt="",  # 不使用 AI 生图，直接走 Pillow 卡片降级
        topic_ref="mock",
        score=5.0,
        review_note="模拟测试内容（无 AI/无 Key）",
    )


def make_mock_dict(platform: str) -> dict:
    """以 JSON 友好形式返回模拟草稿（供 WebUI「填入表单」）。"""
    draft = make_mock_draft(platform)
    return {"title": draft.title, "content": draft.content, "tags": draft.tags}

"""Task_Daily_Post：每日发帖任务（AI 全自动运营核心）。

流程（见 docs/architecture.md 3.1）：
  热点搜寻 → 定制化内容混合生成 3 份草稿 → AI 自审择优 →
  素材制作（mode=image 单图 / mode=slideshow 多图卡片+音乐合成视频）→ 跨平台发布 → 报告落盘

配置项（config/tasks.yaml 的 daily_post 段）：
  platforms: 发布平台列表
  drafts: 草稿份数
  visibility: public | private（默认 private 安全模式）
  mode: image | slideshow（默认 slideshow：多图+文字+音乐合成视频发布，替代昂贵 AI 视频）
  image_count: slideshow 模式的图文卡片张数（默认 4）
  music: 用户上传的 BGM 文件名（state/media/music；未指定则无声视频）
  music_dir: 兼容旧配置的背景音乐目录（随机选一首）
  custom_content: 定制化内容（persona/tone/素材 等；dict 内联 或 YAML 路径）
"""
from __future__ import annotations

from pathlib import Path

from ..logger import log
from ..models import PostPayload, TaskReport
from .base import Task, TaskContext


class DailyPostTask(Task):
    """每日发帖：热点 → 文案 → 素材 → 发布。"""

    name = "daily_post"

    async def execute(self, ctx: TaskContext) -> TaskReport:
        self._ctx = ctx  # 供 _step/safe_step 的 progress_cb 使用
        report = self._new_report()
        cfg = ctx.task_config
        platforms: list[str] = cfg.get("platforms") or ["xiaohongshu"]
        drafts_count: int = int(cfg.get("drafts") or 3)
        visibility: str = str(cfg.get("visibility") or "private")
        mode: str = str(cfg.get("mode") or "slideshow")
        image_count: int = int(cfg.get("image_count") or 4)
        music_dir: str | None = cfg.get("music_dir")
        custom: dict | None = self._load_custom_content(cfg.get("custom_content"))
        # 视觉主题（背景风格/自定义背景图）；BGM 只用用户上传音乐（music/music_dir），未配置则无声
        from ..skills.media_creator import resolve_background

        theme = resolve_background(ctx.settings, cfg)
        # 多账号配置：{platform: [profile, ...]}，多个账号按天轮换
        accounts_cfg: dict = cfg.get("accounts") or {}

        # 1) 热点搜寻
        trends = await self.safe_step(
            report, "search_trends",
            ctx.search.execute(limit=10),
            detail="拉取微博/百度/抖音热搜",
        )
        topics = (trends.data if trends and trends.success else None) or []

        # 2-3) 文案生成 + 自审择优（每平台独立进行，注入定制化内容）
        for platform in platforms:
            # 多账号轮换：本平台今天的发布账号
            profile = self._pick_account(platform, accounts_cfg.get(platform), ctx.settings.state_dir)
            account_tag = f"/{profile}" if profile else ""
            if not topics:
                self._step(report, f"content_{platform}", "无热点，跳过文案生成", status="failed")
                if not report.error:
                    report.error = "热点获取失败"
                continue
            gen = await self.safe_step(
                report, f"content_{platform}",
                ctx.content.execute(topics=topics, platform=platform, count=drafts_count, custom=custom),
                detail=f"生成 {drafts_count} 份草稿并自审" + ("（含定制化内容）" if custom else ""),
            )
            if not gen or not gen.success:
                # 业务失败（如无 LLM Key / 无热点）：把步骤标记为 failed
                self._mark_step_failed(
                    report, f"content_{platform}",
                    (gen.error if gen else "") or "文案生成失败（可能未配置 DEEPSEEK_API_KEY）",
                )
                continue
            best = (gen.data or {}).get("best")
            if best is None:
                self._step(report, f"content_{platform}", "未产出可用草稿", status="failed")
                continue

            # 4) 素材制作
            if mode == "slideshow":
                await self._make_slideshow(ctx, report, platform, best, image_count, music_dir,
                                           visibility, profile, theme)
            else:
                await self._make_image_post(ctx, report, platform, best, visibility, profile)

        report = self._finish(report)
        report_path = self.save_report(report, ctx.settings.state_dir.parent / "logs")
        log.info("每日发帖任务完成，报告: %s", report_path)
        return report

    # ---------------- 素材与发布 ----------------
    async def _make_slideshow(
        self, ctx: TaskContext, report: TaskReport, platform: str, best, image_count: int,
        music_dir: str | None, visibility: str, profile: str | None = None,
        theme: dict | None = None,
    ) -> None:
        """模式一：多图卡片 + 音乐 → 合成视频 → 发布视频。"""
        from ..skills.media_creator import SlideshowError, resolve_music

        # 文章类平台（知乎）：纯文本发布，无需卡片/视频素材
        caps = ctx.uploader.registry.get(platform).capabilities
        if caps.get("article"):
            payload = PostPayload(
                platform=platform,
                title=best.title,
                content=best.content,
                tags=best.tags,
                visibility=visibility,
            )
            self._step(report, f"content_{platform}", "文章类平台：直接发布纯文本文章")
            await self._publish(ctx, report, platform, payload, profile)
            return

        # 4a) 生成图文卡片（带视觉主题背景）
        cards = await self.safe_step(
            report, f"cards_{platform}",
            ctx.media.generate_cards(best, image_count=image_count, theme=theme),
            detail=f"生成 {image_count} 张竖屏图文卡片" + (f"（主题背景 {theme.get('style')}）" if theme else ""),
        )
        if not cards:
            self._mark_step_failed(report, f"cards_{platform}", "图文卡片生成失败")
            return
        image_paths = [a.path for a in cards]

        # 4b) 按平台能力分流：video → 合成视频发布；否则（小红书/头条）→ 用卡片直接发图文
        if caps.get("video"):
            # 配乐：任务指定 BGM（music）→ legacy music_dir 随机 → 都没有则无声
            music = resolve_music(ctx.settings, {**ctx.task_config, "music_dir": music_dir})
            try:
                video = await ctx.media.create_slideshow_video(image_paths, music=music)
            except SlideshowError as exc:
                self._mark_step_failed(report, f"video_{platform}", str(exc))
                return
            self._step(report, f"video_{platform}", f"幻灯片视频已合成（{video.path}）")
            payload = PostPayload(
                platform=platform,
                title=best.title,
                content=best.content,
                tags=best.tags,
                video=video.path,
                visibility=visibility,
            )
        else:
            # 平台不支持视频（小红书）：直接用图文卡片发布
            payload = PostPayload(
                platform=platform,
                title=best.title,
                content=best.content,
                tags=best.tags,
                images=image_paths,
                visibility=visibility,
            )
            self._step(report, f"cards_{platform}", f"平台不支持视频，改用 {len(image_paths)} 张图文发布")
        await self._publish(ctx, report, platform, payload, profile)

    async def _make_image_post(
        self, ctx: TaskContext, report: TaskReport, platform: str, best, visibility: str,
        profile: str | None = None,
    ) -> None:
        """模式二：AI 配图 → 发布图文。"""
        image_prompt = best.image_prompt or f"为标题《{best.title}》配一张符合平台调性的图片"
        media = await self.safe_step(
            report, f"media_{platform}",
            ctx.media.execute(prompt=image_prompt, title=best.title),
            detail="AI 生图（Flux→Stability→Pillow 降级链）",
        )
        image_path = getattr(media.data, "path", None) if media and media.success else None
        if not image_path:
            self._mark_step_failed(report, f"media_{platform}", "配图生成失败")
            return
        payload = PostPayload(
            platform=platform,
            title=best.title,
            content=best.content,
            tags=best.tags,
            images=[image_path],
            visibility=visibility,
        )
        await self._publish(ctx, report, platform, payload, profile)

    async def _publish(self, ctx: TaskContext, report: TaskReport, platform: str, payload: PostPayload,
                       profile: str | None = None) -> None:
        """发布并记录结果（profile 指定账号）。"""
        account_tag = f"/{profile}" if profile else ""
        pub = await self.safe_step(
            report, f"publish_{platform}{account_tag}",
            ctx.uploader.publish(payload, profile=profile),
            detail=f"发布到 {platform}{account_tag}（{payload.visibility}）",
        )
        if pub and not pub.success:
            log.warning("发布失败 [%s]: %s", platform, pub.message)
            self._mark_step_failed(report, f"publish_{platform}", pub.message)

    # ---------------- 工具 ----------------
    @staticmethod
    def _load_custom_content(path_or_dict) -> dict | None:
        """读取定制化内容配置。

        支持两种形式：
          - dict：WebUI 任务里内联填写的定制内容（直接使用）
          - str：YAML 文件路径（config/custom_content.yaml）
        不存在/损坏返回 None。
        """
        if isinstance(path_or_dict, dict):
            return path_or_dict if path_or_dict else None
        if not path_or_dict:
            return None
        path = path_or_dict
        cfg_path = Path(path)
        if not cfg_path.exists():
            log.warning("定制化内容配置不存在: %s", path)
            return None
        try:
            import yaml

            with open(cfg_path, encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            log.info("已加载定制化内容配置: %s", path)
            return data if isinstance(data, dict) else None
        except Exception as exc:  # noqa: BLE001
            log.warning("定制化内容配置解析失败: %s", exc)
            return None

    @staticmethod
    def _pick_account(platform: str, profiles: list | None, state_dir: Path) -> str | None:
        """多账号轮换：从账号列表里按天轮换选一个 profile。

        - profiles 为空/未配置 → 返回 None（用平台默认账号）
        - 同一天多次触发不重复轮换（state/rotation.json 按日期记录）
        """
        if not profiles:
            return None
        profiles = [p for p in profiles if p]
        if not profiles:
            return None
        rot_path = state_dir / "rotation.json"
        today = __import__("datetime").date.today().isoformat()
        data = {}
        if rot_path.exists():
            try:
                import json

                data = json.loads(rot_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                data = {}
        entry = data.get(platform) or {}
        if entry.get("date") == today:
            return profiles[entry.get("idx", 0) % len(profiles)]
        idx = entry.get("idx", 0)
        profile = profiles[idx % len(profiles)]
        data[platform] = {"date": today, "idx": (idx + 1) % len(profiles)}
        try:
            rot_path.write_text(
                __import__("json").dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:  # noqa: BLE001
            pass
        return profile

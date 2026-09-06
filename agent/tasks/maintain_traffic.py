"""Task_Maintain_Traffic：流量维护任务（定时巡检 + 评论自动回复）。

流程（见 docs/architecture.md 3.2）：
  对配置中的每个作品链接：抓评论 → 过滤去重 → 按「账号定制人设」生成回复 → 自动提交 → 去重落盘

配置项（config/tasks.yaml 的 maintain_traffic 段）：
  notes: [{platform, url, topic?}, ...]   topic 为该作品的一句话主题（可选，让回复更贴合）
  max_replies: 每个作品最多回复条数
  custom_content: 账号定制内容（persona / tone / custom_materials / exclude_topics），
                  dict 内联（任务调度页的「定制」）或 YAML 路径都支持，用于生成贴合人设的回复
"""
from __future__ import annotations

from pathlib import Path

from ..logger import log
from ..models import TaskReport
from .base import Task, TaskContext


class MaintainTrafficTask(Task):
    """流量维护：回复粉丝评论（使用账号定制人设生成文案）。"""

    name = "maintain_traffic"

    async def execute(self, ctx: TaskContext) -> TaskReport:
        self._ctx = ctx  # 供 _step/safe_step 的 progress_cb 使用
        report = self._new_report()
        cfg = ctx.task_config
        notes: list[dict] = cfg.get("notes") or []
        max_replies: int = int(cfg.get("max_replies") or 10)

        # 账号定制内容：persona/tone/素材/红线（来自 AI 工坊定制包或任务调度页定制）
        custom = self._load_custom(cfg.get("custom_content"))
        persona: str = str((custom or {}).get("persona") or "")
        tone: str = str((custom or {}).get("tone") or "")
        materials: list = (custom or {}).get("custom_materials") or []
        taboos: list = (custom or {}).get("exclude_topics") or (custom or {}).get("taboos") or []

        if not notes:
            self._step(
                report, "config",
                "未配置待巡检作品链接：请在任务配置的「作品链接」里按『平台 作品URL [主题]』填写",
                status="failed",
            )
            report.error = "无巡检目标"
            return self._finish(report)

        total_replied = 0
        for note in notes:
            platform = str(note.get("platform") or "").strip()
            url = str(note.get("url") or "").strip()
            topic = str(note.get("topic") or note.get("title") or "").strip()
            if not platform or not url:
                log.warning("[maintain_traffic] 跳过无效作品配置: %s", note)
                continue
            detail = "用 AI 定制人设巡检并回复 " + url + (f"（主题：{topic}）" if topic else "")
            result = await self.safe_step(
                report, f"reply_{platform}",
                ctx.comments.execute(
                    action="auto_reply", platform=platform, note_url=url,
                    max_replies=max_replies, topic=topic,
                    persona=persona, tone=tone, materials=materials, taboos=taboos,
                ),
                detail=detail,
            )
            if result and result.success:
                replied = [r for r in (result.data or []) if getattr(r, "success", False)]
                total_replied += len(replied)
        report = self._finish(report)
        report_path = self.save_report(report, ctx.settings.state_dir.parent / "logs")
        log.info("流量维护完成，共回复 %d 条，报告: %s", total_replied, report_path)
        return report

    @staticmethod
    def _load_custom(path_or_dict) -> dict | None:
        """读取账号定制内容（persona/tone/素材/红线）。

        支持 dict 内联（任务调度/AI 工坊）或 YAML 文件路径；
        兼容「account」内嵌结构（任务调度页定制表单），统一展开为顶层字段。
        """
        if isinstance(path_or_dict, dict):
            data = path_or_dict or {}
        elif path_or_dict:
            cfg_path = Path(str(path_or_dict))
            try:
                import yaml

                with open(cfg_path, encoding="utf-8") as fh:
                    loaded = yaml.safe_load(fh) or {}
                data = loaded if isinstance(loaded, dict) else {}
            except Exception as exc:  # noqa: BLE001
                log.warning("[maintain_traffic] 定制内容配置解析失败: %s", exc)
                return None
        else:
            return None

        account = data.get("account")
        if isinstance(account, dict):
            merged: dict = {**data}
            # 顶层字段优先，account（任务调度页定制表单）内嵌兜底
            merged["persona"] = str(data.get("persona") or account.get("persona") or "")
            merged["tone"] = str(data.get("tone") or account.get("tone") or "")
            if not merged.get("custom_materials"):
                merged["custom_materials"] = data.get("custom_materials") or []
            merged["exclude_topics"] = (
                data.get("exclude_topics") or account.get("exclude_topics")
                or account.get("taboos") or []
            )
            return merged
        return data

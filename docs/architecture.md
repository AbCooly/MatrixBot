# 架构说明（v2）

> 先读 [README](../README.md)。本文讲代码怎么组织、核心链路怎么走，10 分钟能读完。

## 一、总览

```
浏览器执行层 (docker/browser-gui 容器)
  守护进程 profile_manager.py :9100
    · 每「平台×账号」= 一个独立有头 Chromium（Xvfb 桌面）
      独立 user-data-dir(state/browsers/ 登录态) + 独立 CDP 端口 + noVNC 窗口
    · 资源治理：严格串行 MAX_INSTANCES=1 / 闲置回收 / 人工接管 pin
    · 提供 /browser /browsers /vnc API
          │ HTTP + CDP
业务层 (agent 容器)
  agent/webui.py            WebUI(FastAPI风格 Flask) :8080
    ├─ 账号矩阵/登录态/新建删除
    ├─ 人工接管（prepare → noVNC 全屏页）
    ├─ 内容发布（手动 + 模拟）与实时日志
    ├─ 定时任务列表/触发
    └─ 设置：LLM 模型池 / API Key / 生图密钥 / VNC 密码
  agent/scheduler/          定时调度（可选独立容器/进程）
      └─ 按 cron 触发任务 → 复用同一批浏览器
```

**核心约束（为了内存与风控）**：
- 同一时刻**全局最多 1 个浏览器实例**（默认）。浏览器是稀缺资源，所有功能围绕它调度：
  登录态检测走**后台串行队列**，任务/发布/接管期间互相让路（`webui.py` 的
  `_automation_busy` / `_takeover_until`）。
- 人工接管用 pin 保活：`/api/remote/prepare` → 守护进程 `pin` 该账号 → 前端打开
  `/remote-desktop`（noVNC 全屏）→ 人在真实窗口完成验证 → 登录态落盘。

## 二、关键文件与职责

| 文件 | 职责 |
|---|---|
| `agent/config.py` | 配置统一入口：`.env` 底座 + `state/config_runtime.json` 热覆盖；提供 `llm_ready()`/`resolve_llm_provider()` 等助手 |
| `agent/webui.py` | 应用主体：全部 API + 前端静态页 + 后台串行登录检测队列 + 发布/任务线程 |
| `agent/webui_remote.py` | 人工接管准备（仅 `prepare_managed`，约 80 行） |
| `agent/llm_client.py` | OpenAI 兼容 LLM 客户端，支持多 provider 池；`DeepSeekClient = LLMClient` 别名 |
| `agent/skills/uploader/browser.py` | `BrowserPool`：HTTP(续租/保活) + CDP(连到每账号 Chromium)；`BrowserManagerClient`：调守护 API |
| `agent/skills/platform_uploader.py` | 平台无关发布主流程（context→文案→素材→上传→发布）；各平台适配器在 `skills/uploader/<平台>.py` |
| `agent/skills/*` | 文案生成/图片/视频/评论等原子能力 |
| `agent/tasks/base.py` `tasks/*` | 任务=多个技能按上下文顺序编排，产出报告 |
| `agent/scheduler/*` | cron 调度：读 `config/tasks.yaml`，到点构造 Scheduler 执行 |

## 三、配置分层

```
默认值(Settings dataclass)
   ← 环境变量 / .env（deepseek/fal/stability/browser_mgr_url/novnc_url…）
   ← state/config_runtime.json（WebUI「设置」写入；模型池/密钥/生图 Key）
```

- 运行时文件不存在时，用 `.env` 的 DeepSeek 配置兜底成一个 provider。
- 密钥**永不回传明文**：GET 只返回 `has_key`，POST 时 Key 为空或 `••••` 开头=保持不变。
- WebUI 保存后调用 `_reload_settings()`，后续任务/发布即刻使用新配置，无需重启。

## 四、一次「模拟发布」走查（推荐先跑它）

`内容发布 → 一键模拟测试发布` → `api_mock_publish` 起线程：

```
_mock_publish_worker
  1. PlatformUploader(_settings)   # 实时配置
  2. pool.get_page(platform, profile)   # 向守护 ensure 该账号浏览器(启动/复用)
  3. check_login() -> 未登录则转人工接管
  4. 素材生成：卡片(Pillow) 或 图文/视频(按平台能力，标题带 [测试])
  5. 真实发布（私有可见性）→ 结果实时写 publish_history.jsonl
  6. close_all()：只断 CDP，不关窗口/登录态
```

实时日志进度通过 `_publish_runs[key]` 状态 + 前端轮询呈现。

## 五、登录态检测（为什么首次要等 1-2 分钟）

`login_status(platform, profile)`：
1. 查 120 秒缓存，命中即返回；
2. 若任务/发布占用、或该账号/他账号处于人工接管窗口 → 返回缓存旧值（避免抢浏览器）；
3. 否则提交后台**串行队列** `_status_queue`，`_status_worker` 逐账号真实打开浏览器
   验证登录（复用 `get_page` 与平台域名的页面匹配），写缓存。

好处：面板永不因检测卡住；内存里同一时刻最多 1 个 Chromium。

## 六、设置扩展指南（给后续开发者）

想加一个“WebUI 可配置项”：
1. `config.py` 加字段/解析逻辑（env 默认值 + runtime 覆盖）；
2. `deployment_info()`（webui.py）把它带回前端展示状态；
3. `/api/settings` POST 里加保存逻辑（注意密钥走“留空=保持”约定）；
4. `index.html` 的设置卡片加表单 + `saveLlmConfig` 式的收集/保存函数。

## 七、风险提示与约定

- 平台反风控：**同账号并发 = 高压线**。抢浏览器互斥逻辑集中在 webui.py / 守护进程，
  新功能请复用 `pool` 与队列，不要自己裸起浏览器。
- 每账号独立浏览器目录 = 独立设备指纹，是风控安全设计的根基，勿改成共享 profile。
- 删除功能时优先看 `grep`：`BrowserPool` 外部只暴露 `get_page / new_page / close_all`，
  `webui_remote` 只暴露 `prepare_managed`，接口面收敛后重构很安全。

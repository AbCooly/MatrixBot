# MatrixBot · 多平台社交媒体矩阵运营（Docker 一键部署 WebUI）

把「账号矩阵、人工远程登录接管、AI 内容生成、定时发布、评论维护」整合成一个
WebUI 应用，跑在一台云服务器上。**浏览器全部跑在容器内的虚拟桌面里** ——
风控难缠的扫码 / 滑块 / 验证码，由你在 noVNC 网页远程桌面里手动完成一次，
登录态落盘复用，自动化接管后续的发布与互动。

> 支持的平台：抖音 / 小红书 / 视频号 / 知乎 / 头条（图片 / 视频 / 文章按平台能力分发）。

---

## 🚀 一键部署（Docker，不需要本地装 Python/Chromium）

在一台 **2GB+ 内存的 x86_64 Linux 云服务器**上执行这一行：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/AbCooly/MatrixBot/main/install.sh)
```

脚本会自动完成：安装 Docker → 拉取代码 → 生成带随机密码的 `.env` →
准备 `config/tasks.yaml` → `docker compose` 构建并启动
**WebUI + 远程桌面 + 常驻调度器**。

想先看代码再装，也可以：

```bash
git clone https://github.com/AbCooly/MatrixBot.git
cd MatrixBot && bash install.sh
```

装完后打开 `http://<服务器IP>:8080`：
1. 登录账号与初始密码打印在脚本末尾（也可 `docker compose logs agent` 查看），
   登录后到「设置 → 账号安全」改密；
2. 「设置 → 模型与密钥」填一个 OpenAI 兼容模型 Key（DeepSeek 等），或用内置测试模式；
3. 「账号矩阵 → 新建账号 → 去远程登录」在 noVNC 网页里扫码/拖滑块，等账号变绿；
4. 在「内容发布」先跑一次模拟测试发布，全链路通了再去「AI 工坊 / 任务调度」建定时任务。

> 更多细节见 [docs/docker-deploy.md](docs/docker-deploy.md)（架构、内存模型、运维 FAQ）。

---

## 它能做什么

| 能力 | 说明 |
|---|---|
| 账号矩阵 | 每「平台 × 账号」一个独立浏览器目录与登录态；新建/删除/状态绿点 |
| 人工接管 | 「去远程登录」→ noVNC 全屏远程桌面，手动扫码/拖滑块一次，登录态复用 |
| 内容发布 | 图文 / 视频 / 文章；先「模拟测试发布」（零 token）再真实发布，全程留痕 |
| AI 生成 | 文案/评分/拟人评论；模型池支持多套 OpenAI 兼容模型（DeepSeek/通义/Kimi…） |
| 定时任务 | daily_post（每日发帖）、maintain_traffic（评论维护），标准 cron，**改动 30 秒热加载** |
| AI 工坊 | 一句话需求 → 运营定制包（人设/素材/视觉主题）→ 一键落地为定时任务 |
| 生图素材 | 设置里配 FAL / Stability Key 即用；未配自动降级为 Pillow 文字封面 |
| 素材库 | 背景风格 + 自定义背景图 + **用户上传的 BGM**（不再内置/合成任何音乐） |

## 架构（一句话版）

```
浏览器容器 browser-gui（虚拟桌面 Xvfb + noVNC + 每账号独立 Chromium/CDP）
        ▲  HTTP/CDP
agent（WebUI :8080：账号/发布/设置/调度）── 同卷登录态 ── scheduler（可选，常驻 cron）
```

- **内存友好**：默认严格串行 `MAX_INSTANCES=1` + 闲置 15 分钟回收 + 接管窗口 pin 2 小时，
  内存峰值 ≈ 单个 Chromium（~500MB），不随账号数量上涨。
- **登录态落盘**共享卷 `state/browsers/`，容器重启不丢。
- **配置分层**：`.env` 作底座，WebUI「设置」写入 `state/config_runtime.json` 即时热覆盖。

## 手动部署（已有 Docker 的环境）

```bash
cd docker
cp .env.example .env
# 建议改：WEBUI_PASSWORD（或留空=首启自动随机并打印日志）、GUARDIAN_TOKEN、
#         VNC_PASSWORD（留空=自动随机，WebUI「设置」可查看/修改）
docker compose --profile scheduler up -d --build
```

- WebUI 面板：`http://<服务器IP>:8080`
- noVNC 远程桌面：`http://<服务器IP>:6080/vnc.html`（VNC 密码见 WebUI「设置 → 远程桌面密码」）
- 只想要 WebUI、不开定时调度：去掉 `--profile scheduler`。
- 任务清单 `config/tasks.yaml`：没有则把 `config/tasks.example.yaml` 复制过去；
  WebUI「任务调度」的增删改会写到这里，**scheduler 每 30 秒自动热加载，无需重启**。

## 定时任务说明

- 预置两类任务：`daily_post`（每日发帖）、`maintain_traffic`（按账号人设自动回复评论）。
- 仓库自带的示例任务**默认停用**，避免新手上线即误发真实内容；配好平台/账号后启用。
- 新建任务两种入口：
  1. WebUI「任务调度」：填名称、cron、平台与业务参数（daily_post 文案/图片/账号；
     maintain_traffic 的每行作品链接 `平台 链接 [一句话主题]` 与回复上限）；
  2. WebUI「AI 工坊」：输入运营需求（人设/门店/产品），一键生成定制包并排期。
- 评论回复会注入你在任务里配置的**人设 / 口吻 / 自有素材 / 红线**，让 AI 回复贴合账号
  而不是千篇一律套话。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/architecture.md](docs/architecture.md) | 代码结构与核心流程 |
| [docs/docker-deploy.md](docs/docker-deploy.md) | 部署细节、内存模型、运维与 FAQ |
| [docs/API.md](docs/API.md) | WebUI API 一览 |

## 目录结构

```
MatrixBot/
├── agent/                    # 运营智能体（Python）
│   ├── webui.py              # WebUI 服务与 API
│   ├── config.py / llm_client.py
│   ├── scheduler/            # 定时调度器（tasks.yaml 热加载）
│   ├── tasks/                # daily_post / maintain_traffic
│   ├── skills/               # 文案/图片/评论技能 + uploader/ 平台发布适配器
│   └── webui_static/         # 前端单页（原生 JS，无构建）
├── docker/                   # Docker 编排
│   ├── docker-compose.yml    # browser-gui / agent / scheduler(profile)
│   ├── .env.example
│   ├── agent/                # 智能体镜像
│   └── browser-gui/          # 浏览器容器（守护 :9100 + noVNC :6080）
├── config/
│   ├── tasks.example.yaml    # 定时任务示例（默认停用）
│   └── custom_content.yaml   # 账号人设/素材定制示例
├── assets/fonts/             # 中文渲染字体（镜像构建需要）
├── install.sh                # 一键部署脚本
├── tests/                    # pytest 单测（无需容器/浏览器）
├── docs/
├── requirements.txt
└── pytest.ini
```

运行时隐私数据（账号 Cookie、VNC 密码、回复去重、任务清单、日志）全部在
`state/` / `logs/` / `config/tasks.yaml`，已被 `.gitignore` 排除，**不会进入仓库**；
开源代码里没有任何内置密钥。

## 开发 / 测试

```bash
python -m pytest tests -q          # 单测不依赖容器/浏览器
cd docker && docker compose up -d --build
```

改代码后重建 agent 镜像即可：`docker compose up -d --build agent`。

## 拟人行为层（Humanizer）

平台风控不只看“是不是自动化”，更看**行为指纹**（固定等待、直线匀速鼠标、直达发布页、
均匀打字节奏都是强特征）。部分平台对异常行为不弹任何警告，而是**静默降权/限流/打标**
（内容不进推荐池），因此一套拟人行为噪声层需要覆盖**全部发布平台**而不是只针对
个别会弹警告的站点。Agent 内置了可开关的拟人行为噪声层（默认开启，
`HUMANIZER_ENABLED=0` 关闭），抖音 / 小红书 / 视频号 / 知乎 / 头条的发布链路均已接入：

- **不直奔发布页**：发布前在首页随机滚动浏览、光标游走/悬停（制造“到场感”），
  优先通过页面 UI 入口点进发布页，直达 URL 仅作兜底；
- **生物力学鼠标轨迹**：贝塞尔曲线 + 随机弯度，接近目标时步距收敛（弹道长步 → 末端
  微调），长距离约一半概率过冲再回拉，到位后震颤收敛；mousemove 事件间隔为重尾高熵
  分布（不是均匀/固定步数直线）；
- **真实点击**：瞄准散射 + 悬停迟疑 + “按下-持键-松开”（50~190ms） + 松手回弹，
  不再是一瞬间的 `mouse.click()` 或直线 `mouse.move(steps=n)+click`；
- **键入节奏**：按“脉冲串 + 输入法式选词停顿”组织，单字符间隔重尾分布，标点后长停；
  不再用 `keyboard.type(delay=10)` 这类固定均匀延迟，也不用 `fill()` 瞬时填完；
- **思考停顿**：所有动作间插入对数正态/重尾分布的人类决策间隙，替代固定
  `wait_for_timeout(4000)`；
- **自动化痕迹遮蔽**：守护进程以有头真实 Chromium 启动并带
  `--disable-blink-features=AutomationControlled`，页面层再注入
  `navigator.webdriver=false` 等遮蔽（纵深防御）。

代价：单次发布任务会多花约 10~40s（随机）。注意行为拟人 ≠ 100% 不被识别：
账号/设备指纹、发布频率、CDP 调试通道等仍可能暴露自动化特征，平台策略也在随时变化，
请始终遵循“安全与合规提醒”。

## 安全与合规提醒

- **默认防护**：WebUI 全部页面/API 需登录；守护 API(9100) 只绑容器回环并可用
  `GUARDIAN_TOKEN` 鉴权；noVNC 需 VNC 密码；原生 VNC(5900) 默认只绑宿主回环。
- **公网部署建议**：给 8080 套 HTTPS（Caddy/Nginx）并设 `WEBUI_COOKIE_SECURE=true`；
  非必要时别把 6080 长期裸暴露；用住宅/固定 IP、控制单账号发布频率。
- **合规**：自动化脚本仅用于你自己有运营权限的账号；请遵守各平台服务条款与当地法规，
  内容发布建议保留人工审核环节。本项目按现状提供，使用者自担风险。

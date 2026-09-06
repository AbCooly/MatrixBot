# 容器化部署（Docker + noVNC 网页远程桌面）

> **一键部署**（自动装 Docker、clone、生成随机密码、拉起 WebUI+调度器）：
>
> ```bash
> bash <(curl -fsSL https://raw.githubusercontent.com/AbCooly/MatrixBot/main/install.sh)
> ```
>
> 下面内容为手动/进阶部署细节。面向“云服务器 Linux、无 CDP、无显示器”的实战形态：
> 浏览器跑在容器里的虚拟桌面（Xvfb）上，每个「平台 × 账号」一个独立有头 Chromium；
> 自动化引擎通过 CDP 驱动这些实例；登录被**滑块 / 扫码 / 身份验证**卡住时，
> 在账号矩阵点「🖥 **去远程登录**」，系统会把该账号浏览器窗口打开到桌面并**新标签页
> 打开全屏 noVNC 远程桌面**——你在真实浏览器窗口里拖滑块/扫码，完成后回 WebUI 稍候
> 即自动刷新为“已登录”，自动化继续。登录态照常持久化。

## 架构

```
┌──────────────────────────── 一台云服务器 ────────────────────────────┐
│                                                                      │
│  browser-gui 容器                                                     │
│   ├ Xvfb :99（1920×1080 虚拟桌面） + openbox（窗口管理）              │
│   ├ 账号浏览器守护 API :9100（profile_manager.py）                    │
│   │   每个「douyin / douyin__主号 / xiaohongshu__副号…」= 一个        │
│   │   独立 Chromium 实例：独立 user-data-dir(登录态) + 独立 CDP 端口   │
│   │   → 端口映射落盘 state/.browser-ports.json，容器重启自动恢复      │
│   │   → 内存治理：严格串行 MAX_INSTANCES=1 + 闲置回收 + 接管保留      │
│   ├ x11vnc :5900 + noVNC :6080（网页远程桌面）                        │
│   └ 状态共享卷 shared-state（登录态 state/browsers/ + VNC 密码文件）  │
│                     ▲ CDP (BROWSER_MGR_URL)                           │
│  agent 容器（WebUI :8080）  ──同卷──→  账号发现/登录态/发布报告        │
│  scheduler 容器（可选，定时调度）                                     │
└──────────────────────────────────────────────────────────────────────┘
```

关键点：**人工看到的窗口 = 自动化驱动的浏览器**。没有“截图转发”的信息差，
滑块拖完即完成，不需要额外告诉系统“我操作完了”。

## 内存模型（为什么不会“越开越多浏览器”）

每个有头 Chromium 常驻约 **450-500MB**，因此默认**严格串行**：

- `BROWSER_MAX_INSTANCES=1`（默认）：同一时刻**最多 1 个浏览器实例**。多平台任务
  本来就是顺序执行——切换平台时自动关闭上一实例、拉起当前账号（登录态在各自目录
  落盘，切回自动恢复，不丢）。
- `BROWSER_IDLE_TTL=900`（默认）：浏览器**闲置 15 分钟自动回收**。任务/面板每次
  使用都会自动“续租”，正在跑的发布不会被误收。
- `BROWSER_PIN_TTL=7200`（默认）：你点「去远程登录」打开远程桌面后，该账号窗口被
  **pin 保留 2 小时**，期间不会被闲置回收或淘汰——避免登录做到一半窗口被切走；
  超时自动解除（防忘关一直驻留）。
- WebUI 登录态检测在容器化模式下走**后台串行队列**：一次只检测一个账号，且任务/发布/
  人工接管期间自动让路，不会并发拉起一堆浏览器。
- 把这些变量写进 `.env`（见 `.env.example`）即可调整，重启容器生效。

## 快速开始

```bash
cd docker
cp .env.example .env
# 编辑 .env：VNC_PASSWORD 仅作“初始密码”；DEEPSEEK_API_KEY 可留空——
# 部署后都推荐直接在 WebUI「设置」里配（模型池/API Key/生图密钥即时生效且重启保留）
docker compose up -d --build
```

- WebUI：http://<服务器IP>:8080
- noVNC 远程桌面：http://<服务器IP>:6080/vnc.html
  （密码见 WebUI「设置 → 远程桌面密码」；WebUI 登录卡住时点顶栏
  「🖥 远程桌面 · 人工接管」即全屏打开）

## 使用流程（账号矩阵 + 远程接管，不再弹截图框）

1. 账号矩阵 → “新建账号”（如 抖音 / 主号）→ 出现一张账号卡。
2. 点卡片上的「🖥 **去远程登录** / 远程接管」：
   - 后端自动把该账号浏览器窗口打开到桌面（打开登录/首页），**并 pin 保留**；
   - 浏览器自动**新标签页打开全屏 noVNC 远程桌面**；
   - 输入 VNC 密码进入桌面，点开标题含该平台的浏览器窗口（如
     `creator.douyin.com`），扫码 / 拖滑块 / 输手机验证码——**都是真实鼠标**，
     拖动滑块不再被“截图弹窗”卡住。
3. 完成后关闭远程桌面页，回 WebUI 稍候：普通状态 1-2 分钟内自动刷新；若尚处在该
   账号的远程接管保护窗口内（最长 10 分钟）会自动顺延，稍候即变“已登录”。
4. Cookie 已落盘共享卷，之后调度器/手动发布都直接复用登录态，不需要再登录。
5. AI 能力（文案生成/评分/评论）在 WebUI「设置 → 模型与密钥」配置：
   添加/切换多个 OpenAI 兼容模型（DeepSeek/通义/Kimi…），填 API Key 即可用，
   无需改 .env 重启。

> 为什么需要等 1-2 分钟：为省内存，账号登录态是后台**逐个**（串行）检测的，
> 不是并发拉浏览器；多账号时状态按顺序补齐，请以账号卡最终绿点为准。

## 远程桌面（noVNC）密码管理

- **查看/修改入口**：WebUI →「设置 → 远程桌面密码」卡片（仅容器化部署显示）。
  能看到当前密码明文、来源（`.env` / 随机生成 / 本面板保存）与修改时间。
- **修改后**：立即生效，noVNC 下次连接请使用新密码；写入共享卷，**容器重启后保留**
  （不会退回 `.env` 旧值）。
- `.env` 的 `VNC_PASSWORD` 只在「WebUI 未保存过密码」时作为初始密码使用；
  已保存过则以 WebUI 保存值为准。

## 开启常驻定时调度（可选）

```bash
cd docker
docker compose --profile scheduler up -d
```

说明：
- `agent`（WebUI）与 `scheduler` 是**两个独立进程**，共享同一批浏览器实例。
- 定时任务运行期间请勿在 WebUI 对**同一账号**做发布/登录，避免同账号并发被风控；
  不同账号之间互不影响（各自独立浏览器，严格串行由守护进程统一调度）。

## 日常运维

| 操作 | 命令 |
|---|---|
| 看启动日志 | `docker compose logs -f agent browser-gui`（在 docker/ 下） |
| 看某账号浏览器日志 | `docker exec -it newpj-browser-gui-1 tail -f /app/state/logs/browser-douyin__主号.log` |
| 列出浏览器实例（含 pin/闲置秒数） | `docker exec newpj-browser-gui-1 curl -s http://127.0.0.1:9100/browsers` |
| 关闭某账号浏览器(保留登录态) | `docker exec ... curl -X DELETE -H 'Content-Type: application/json' -d '{"account":"douyin__主号"}' http://127.0.0.1:9100/browser` |
| 查看/修改 VNC 密码 | WebUI「设置 → 远程桌面密码」；命令行：`curl -s http://127.0.0.1:9100/vnc`、`curl -X PUT -H 'Content-Type: application/json' -d '{"password":"新密码"}' http://127.0.0.1:9100/vnc` |
| 完全重置（清登录态） | `docker compose down -v`（会删掉 shared-state 卷） |

## 安全建议（生产必读）

1. **改掉 VNC 密码**：noVNC 页面本身无鉴权，只靠 VNC 密码——部署后第一步在
   WebUI「设置 → 远程桌面密码」改掉默认值。
2. 建议在云服务器上**不要直接暴露 6080/8080 到公网**，用 Caddy / Nginx 反向代理并加
   基本鉴权；noVNC 流量建议走 HTTPS/WSS。
3. WebUI 无内置账号体系，远程使用务必加反代鉴权或仅开放内网。
4. 平台风控：请用住宅/固定 IP 服务器；控制单账号发布频率与并发。

## 常见问题

- **noVNC 黑屏 / 白屏**：检查 `docker compose ps` 是否 healthy；`docker compose logs browser-gui`
  看 x11vnc/novnc 是否就绪；首次拉起 Chromium 需十几秒，稍等再刷新。
- **Chromium 起不来（日志报错）**：容器默认 root + `--no-sandbox`；`--disable-dev-shm-usage`
  避免共享内存不足。看 `/app/state/logs/browser-<账号>.log`。
- **窗口太多找不到 / 忘 pin 被回收**：每个账号窗口标题就是平台站点名
  （creator.douyin.com / creator.xiaohongshu.com 等），Alt+Tab 切换；
  「去远程登录」打开后窗口会自动 pin 保留 2 小时。
- **改了 VNC 密码后 noVNC 连不上**：用新密码；页面弹出密码框取消后需刷新
  `vnc.html` 重进。
- **agent 提示连接账号浏览器失败**：确认 `browser-gui` 服务 healthy
  （守护 API :9100 通）；agent 里 `BROWSER_MGR_URL` 指向 `http://browser-gui:9100`。
- **远程桌面连不上**：宿主防火墙放行 6080。WebUI 的「远程桌面」入口会按你访问面板
  的 Host 自动推导 noVNC 地址（`.env` 配了 `NOVNC_URL` 则优先用它，此时请保证它与你
  的访问地址一致）。
- **scheduler 与 WebUI 并发**：同账号会冲突，错误信息会说明原因；错开即可。

## 关于模式（v2 已移除 local / CDP）

v2 起项目只保留**容器化 Browser-GUI 一种部署方式**：
- agent 不直接开浏览器、不连接外部 Chrome（已删除 Local 持久化上下文与 CDP_URL 连接真实 Chrome 的代码路径）；
- 一切浏览器动作都通过 `BROWSER_MGR_URL` 交给 browser-gui 守护进程（每账号独立有头 Chromium + noVNC 人工接管）；
- 也因此删除「截图驾驶舱弹窗」，人工操作统一走全屏 noVNC 远程桌面。

本地想改代码快速起 WebUI？直接跑 agent 容器即可（`docker compose up agent`），
或在有 venv 的宿主机 `python -m agent.webui`——注意此时浏览器功能不可用，仅可浏览
面板/设置界面（设置项可写 `state/config_runtime.json` 并热生效，适合调试）。

## 目录说明

```
docker/
├── docker-compose.yml        # 编排：browser-gui / agent / scheduler(profile)
├── .env.example              # 复制为 .env 后修改（含 VNC / 浏览器资源模型变量）
├── browser-gui/              # 浏览器容器
│   ├── Dockerfile
│   ├── profile_manager.py    # 账号浏览器守护（HTTP :9100）+ 回收看门狗 + VNC 密码 API
│   ├── supervisord.conf      # Xvfb/openbox/x11vnc/noVNC/守护 托管
│   └── entrypoint.sh         # VNC 密码解析（WebUI 保存 > .env > 随机）+ 拉起 supervisord
└── agent/                    # 智能体容器（WebUI）
    └── Dockerfile
```

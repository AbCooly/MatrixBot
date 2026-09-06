# 接口与数据结构定义（API.md）

> 接口优先原则：本文档是 Skill / Task / Scheduler 之间的唯一契约。实现必须与之一致。

## 1. 数据模型（agent/models.py，全部为 dataclass）

### 1.1 热点

```python
@dataclass
class HotTopic:
    platform: str          # "weibo" | "baidu" | "douyin"
    rank: int              # 榜单排名（1 起）
    title: str             # 热搜词条
    url: str               # 详情链接
    heat: int = 0          # 热度值（无则 0）
    category: str = ""     # 分类标签（如 娱乐/社会）
```

### 1.2 内容草稿

```python
@dataclass
class ContentDraft:
    platform: str          # "xiaohongshu" | "douyin" | "wechat_channels"
    title: str             # 标题
    content: str           # 正文
    tags: list[str]        # 话题标签（不含 #）
    image_prompt: str      # 配图提示词（给 Media_Creator 用）
    topic_ref: str = ""    # 关联热点标题（溯源用）
    score: float = 0.0     # 自审得分（0-10）
    review_note: str = ""  # 自审理由
```

### 1.3 素材资产

```python
@dataclass
class MediaAsset:
    path: str              # 本地绝对路径
    kind: str              # "image" | "video"
    width: int = 0
    height: int = 0
    provider: str = ""     # "flux" | "stability" | "pillow"
```

### 1.4 发布载荷与结果

```python
@dataclass
class PostPayload:
    platform: str          # "douyin" | "xiaohongshu" | "wechat_channels"
    title: str
    content: str
    tags: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)   # 本地图片路径
    video: str | None = None                          # 本地视频路径（可选）
    visibility: str = "private"   # "public" | "private" | "friends"
    scheduled_at: str | None = None  # ISO 时间，平台支持时生效

@dataclass
class PublishResult:
    platform: str
    success: bool
    status: str            # published | draft_created | login_required | verification_required | failed
    message: str = ""
    url: str | None = None # 发布后的作品链接（尽力而为）
```

### 1.5 评论与回复

```python
@dataclass
class Comment:
    platform: str
    comment_id: str
    user_name: str
    content: str
    like_count: int = 0
    created_at: str = ""
    note_url: str = ""     # 所属作品链接

@dataclass
class ReplyResult:
    comment: Comment
    reply_text: str = ""
    success: bool = False
    error: str = ""
```

### 1.6 Skill 统一结果

```python
@dataclass
class SkillResult:
    success: bool
    data: Any = None
    error: str | None = None
    meta: dict = field(default_factory=dict)
```

### 1.7 任务报告

```python
@dataclass
class TaskReport:
    task_name: str
    started_at: str        # ISO
    finished_at: str
    success: bool
    steps: list[dict]      # [{step, status, detail, took_ms}]
    error: str = ""
```

## 2. Skill 接口

```python
class Skill(ABC):
    name: str
    description: str
    async def run(self, **kwargs) -> SkillResult: ...

# --- Search_Trends ---
class SearchTrends(Skill):
    async def get_hot_topics(self, platforms: list[str] | None = None,
                             limit: int = 10) -> list[HotTopic]
    # 数据源：聚合 API(vvhan/hot_news) → 微博 ajax → 百度 HTML 兜底

# --- Content_Generator ---
class ContentGenerator(Skill):
    async def generate(self, topics: list[HotTopic], platform: str,
                       count: int = 3, tone: str = "") -> list[ContentDraft]
    async def review_and_pick(self, drafts: list[ContentDraft]) -> ContentDraft
    # 生成：DeepSeek deepseek-chat，平台风格 System Prompt
    # 审核：对每份草稿打分并输出 JSON，取最高分

# --- Media_Creator ---
class MediaCreator(Skill):
    async def create_image(self, prompt: str, size: str = "1:1") -> MediaAsset
    # Flux 供应商顺序：fal.ai → stability → pillo 兜底
    async def compose_cover(self, title: str, size: tuple[int, int] = (1080, 1080)) -> MediaAsset
    # Pillow：渐变背景 + 标题文字，保证无 Key 可用

# --- Platform_Uploader ---
class PlatformUploader(Skill):
    async def publish(self, payload: PostPayload) -> PublishResult
    async def check_login(self, platform: str) -> bool
    # 内部：BrowserPool 向 browser-gui 守护 ensure 各账号独立浏览器；PlatformAdapter 注册表分发
    # 登录：由人工在 WebUI「远程桌面接管」完成（扫码/滑块/短信），登录态自动落盘

class PlatformAdapter(ABC):
    platform: str
    capabilities: dict                 # {"image": True, "video": False, ...}
    async def check_login(self) -> bool          # 登录态检测（登录动作由远程桌面人工完成）
    async def publish_image(self, payload: PostPayload) -> PublishResult
    async def publish_video(self, payload: PostPayload) -> PublishResult

# --- Comment_Manager ---
class CommentManager(Skill):
    async def fetch_comments(self, platform: str, note_url: str,
                             limit: int = 10) -> list[Comment]
    async def generate_reply(self, comment: Comment, topic: str = "") -> str
    async def reply(self, platform: str, note_url: str,
                    comment: Comment, text: str) -> ReplyResult
    async def auto_reply(self, platform: str, note_url: str,
                         max_replies: int = 10) -> list[ReplyResult]
    # 去重依赖 state/replied.json；回复文本走 DeepSeek 生成
```

## 3. Task 接口

```python
class Task(ABC):
    name: str
    async def execute(self, ctx: TaskContext) -> TaskReport
    # ctx 注入：config、各 Skill 实例、state 目录

class DailyPostTask(Task):      # name="daily_post"
    # 步骤：trends → generate(3) → review_and_pick → media → upload(各平台)

class MaintainTrafficTask(Task):# name="maintain_traffic"
    # 步骤：对每个配置的作品链接 fetch_comments → 过滤去重 → 生成回复 → reply
```

## 4. Scheduler 接口与配置

```python
class Scheduler:
    def __init__(self, yaml_path: str): ...
    def start(self) -> None            # 常驻阻塞
    def run_once(self, task_name: str) -> TaskReport   # 手动单次执行
    def add_task(self, name: str, cron: str, task: Task) -> None
    # cron 为 5 段 cron 表达式（分 时 日 月 周）
```

`config/tasks.yaml`：

```yaml
tasks:
  daily_post:
    enabled: true
    cron: "0 9 * * *"                  # 每天 09:00
    platforms: ["xiaohongshu", "douyin"]
    drafts: 3
    visibility: private                # 安全模式：仅自己可见
  maintain_traffic:
    enabled: true
    cron: "*/30 8-23 * * *"            # 白天每 30 分钟
    notes:                             # 待巡检作品链接
      - platform: xiaohongshu
        url: "https://www.xiaohongshu.com/explore/xxx"
      - platform: douyin
        url: "https://www.douyin.com/video/xxx"
    max_replies: 10
```

## 5. 环境变量（.env.example）

```ini
# v2：Key 也推荐直接在 WebUI「设置 → 模型与密钥」填写（写 state/config_runtime.json，
# 即时生效、重启保留，无需改 .env）。这里只留环境默认值作兜底。
# DeepSeek（环境兜底，WebUI 未配置模型池时启用）
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat

# 生图（可选，可在 WebUI「设置」里改；未配置时自动降级 Pillow 封面）
FAL_KEY=
STABILITY_API_KEY=

# 运行
LOG_LEVEL=INFO
BROWSER_TIMEOUT_MS=30000
STATE_DIR=state

# 浏览器（v2 仅容器化 Browser-GUI 一种模式，以下为 agent 视角的连接地址）
BROWSER_MGR_URL=http://browser-gui:9100   # browser-gui 守护（agent 与 browser-gui 共享网络栈时也可 127.0.0.1:9100）
NOVNC_URL=                                # 可选：用户浏览器可达的 noVNC 地址；留空则按访问面板的 Host 自动推导

# ---- 浏览器资源模型（省内存，见 docker/.env.example）----
# noVNC 初始密码：部署后请在 WebUI「设置 → 远程桌面密码」查看/修改（改后重启保留）
VNC_PASSWORD=change-me-1234
BROWSER_MAX_INSTANCES=1          # 同一时刻最多实例数（有头 Chromium ≈450-500MB/个）
BROWSER_IDLE_TTL=900             # 浏览器闲置多少秒后自动回收（<=0 不回收）
BROWSER_PIN_TTL=7200             # 「远程桌面接管」窗口保留秒数（超时自动解除）
```

## 6. CLI 入口（agent/main.py）

| 命令 | 作用 |
| --- | --- |
| `python -m agent.main` | 常驻：加载 tasks.yaml 启动调度 |
| `python -m agent.main --once daily_post` | 立即执行一次每日发帖 |
| `python -m agent.main --once maintain_traffic` | 立即执行一次流量维护 |
| `python -m agent.main --verify douyin` | 检查各平台登录态 |

> 登录不支持 CLI：在 WebUI「账号矩阵」点「🖥 去远程登录」打开 noVNC 全屏桌面
> （密码见「设置 → 远程桌面密码」），人工完成扫码/滑块/短信验证码登录，登录态自动持久化。

## 7. 容器化 WebUI / 守护补充接口（Managed 模式）

由 `browser-gui`（守护 :9100，浏览器容器内）与 `agent`（WebUI :8080）提供：

| 接口 | 说明 |
| --- | --- |
| `GET /api/settings` | 读设置：LLM 模型池(密钥只回 has_key) + 生效默认模型 + 生图密钥状态 |
| `POST /api/settings` {llm:{providers,active}, fal_key?, stability_api_key?} | 保存设置并热生效（Key 为空或 `••••` 开头=保持原值），写 state/config_runtime.json |
| `POST /api/remote/prepare` {platform, profile} | 「去远程登录」：确保该账号浏览器运行并打开登录页、`pin` 保留；随后前端开 `/remote-desktop`（noVNC 全屏）人工接管 |
| `GET /api/vnc` | 远程桌面密码信息：当前密码明文 + 来源（`webui`/`env`/`auto`）+ 修改时间 |
| `POST /api/vnc/password` {password} | 修改远程桌面密码（≥6 位，立即生效，容器重启后保留） |
| `GET /vnc`（守护） | 同上（守护原生接口） |
| `PUT /vnc`（守护）{password} | 同上（守护原生接口，写盘 + 重启 x11vnc） |
| `GET /browsers`（守护） | 实例列表：`running / pinned / last_used / idle_s / port / cdp_url` |
| `POST /browser`（守护）{account, heartbeat?, pin?} | ensure（默认）/ heartbeat 续租 / pin 接管保留 |
| `DELETE /browser`（守护）{account} | 关闭实例（登录态目录保留） |

登录态检测在 Managed 模式为**后台串行队列**（一次测一个账号、任务/接管期间让路），
账号状态最多延迟 1-2 分钟刷新，请以账号卡最终绿点为准。

"""WebUI：多账号管理面板（Flask + 原生 JS 单页）。

功能：
  - 账号管理：发现各平台已登录账号（按 profile 隔离），新建/删除账号，查看登录状态
  - 人工接管：登录由人工完成——noVNC 全屏远程桌面（真实浏览器窗口），
    扫码/拖滑块/输验证码后登录态落盘；容器化 Browser-GUI 是唯一部署方式
  - 设置：可编辑 LLM 模型池（多 Provider API Key/模型选择）与生图密钥，即时生效
  - 任务操作：触发 daily_post / maintain_traffic，查看任务报告与日志

启动（v2 仅容器化）：
  cd docker && docker compose up -d --build     # agent(8080) + browser-gui(6080/9100)

说明：本模块不承载任何自动短信/二维码登录流程；登录态检测走后台串行队列，
缓存 120 秒（state/browsers/<platform>__<profile>/ 由人工接管完成后落盘）。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import os
import queue
import secrets
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, redirect, request, send_from_directory

from . import webui_remote as remote_mgr
from .config import (KEY_MASK_PREFIX, llm_ready, load_settings,
                     read_runtime_config, resolve_llm_provider,
                     write_runtime_config)
from .logger import log
from .scheduler.cron import Scheduler
from .tasks.daily_post import DailyPostTask
from .tasks.maintain_traffic import MaintainTrafficTask

# 平台展示名
PLATFORM_NAMES = {
    "douyin": "抖音",
    "xiaohongshu": "小红书",
    "wechat_channels": "微信视频号",
    "zhihu": "知乎",
    "toutiao": "今日头条",
}

app = Flask(__name__, static_folder=None)
_settings = load_settings()
_state_dir = _settings.state_dir
_browsers_dir = _state_dir / "browsers"
_browsers_dir.mkdir(parents=True, exist_ok=True)

# ---------------- WebUI 访问认证（云端部署安全） ----------------
# 所有页面与 /api 一律需要登录（会话 Cookie）。凭据来源：
#   · .env 配置 WEBUI_USERNAME / WEBUI_PASSWORD —— 显式、每次启动同步；
#   · 两者都未配置 —— 首次启动随机生成 admin/密码并打印到日志（仅一次，重启保持）。
# 认证数据（会话签名密钥 + 密码 HMAC 摘要，绝不落盘明文）保存在共享卷
# state/.webui-auth.json（权限 0600）。公网部署建议前端套 HTTPS 并设
# WEBUI_COOKIE_SECURE=true。
_AUTH_FILE = _state_dir / ".webui-auth.json"
_auth_ctx: dict = {}


def _auth_hash(secret: str, password: str) -> str:
    return hmac.new(secret.encode("utf-8"), password.encode("utf-8"), hashlib.sha256).hexdigest()


def _auth_load() -> dict:
    try:
        return json.loads(_AUTH_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _auth_save(data: dict) -> None:
    try:
        _AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _AUTH_FILE.with_name(".webui-auth.json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.chmod(0o600)
        os.replace(tmp, _AUTH_FILE)
    except Exception as exc:  # noqa: BLE001
        log.warning("写入 WebUI 认证文件失败: %s", exc)


def _init_auth() -> None:
    global _auth_ctx
    env_user = (os.environ.get("WEBUI_USERNAME") or "").strip()
    env_pass = os.environ.get("WEBUI_PASSWORD") or ""
    existing = _auth_load()
    secret = existing.get("secret")
    if env_user and env_pass:
        # .env 显式配置：首次/未在 UI 改过密码时，用 .env 密码同步（改 .env 即生效）；
        # 若曾在 UI 改过密码（ui_changed=true），以 UI 密码为准、重启不丢
        if existing.get("ui_changed") and existing.get("pass_hash"):
            secret = existing.get("secret") or secrets.token_hex(32)
            _auth_ctx = {
                "secret": secret, "user": existing.get("user") or env_user,
                "pass_hash": existing["pass_hash"], "source": "env", "ui_changed": True,
            }
            log.info("WebUI 认证：使用环境变量账号「%s」（密码为 UI 修改版，已持久化）",
                     _auth_ctx["user"])
        else:
            secret = existing.get("secret") or secrets.token_hex(32)
            _auth_ctx = {
                "secret": secret, "user": env_user,
                "pass_hash": _auth_hash(secret, env_pass), "source": "env",
                "ui_changed": False,
            }
            _auth_save(_auth_ctx)
            log.info("WebUI 认证：使用环境变量账号「%s」（WEBUI_USERNAME/PASSWORD）", env_user)
        return
    if existing.get("secret") and existing.get("user") and existing.get("pass_hash"):
        _auth_ctx = existing
        log.info("WebUI 认证：使用已保存账号「%s」（密码仅存 HMAC 摘要；可在 设置→账号安全 修改）",
                 existing.get("user"))
        return
    # 随机生成（只在日志打印一次，之后只能改密）
    pw = secrets.token_urlsafe(12)
    secret = secret or secrets.token_hex(32)
    _auth_ctx = {
        "secret": secret, "user": "admin",
        "pass_hash": _auth_hash(secret, pw), "source": "auto",
    }
    _auth_save(_auth_ctx)
    log.warning("WebUI 未配置 WEBUI_USERNAME/WEBUI_PASSWORD，已随机生成管理员账号"
                "（仅本次启动打印一次，请登录后在 设置→账号安全 修改密码）: user=admin password=%s", pw)


def _session_sign(payload: str) -> str:
    return hmac.new(
        _auth_ctx.get("secret", "").encode("utf-8"),
        payload.encode("utf-8"), hashlib.sha256,
    ).hexdigest()


def _session_valid(cookie: str | None) -> bool:
    if not cookie or "." not in cookie:
        return False
    payload_hex, sig = cookie.rsplit(".", 1)
    try:
        payload = bytes.fromhex(payload_hex).decode("utf-8")
    except Exception:  # noqa: BLE001
        return False
    if not hmac.compare_digest(_session_sign(payload), sig):
        return False
    try:
        user, exp = payload.rsplit("|", 1)
        return user == _auth_ctx.get("user") and int(exp) > time.time()
    except Exception:  # noqa: BLE001
        return False


def _session_cookie_value() -> str:
    exp = int(time.time()) + 7 * 86400
    payload = f"{_auth_ctx['user']}|{exp}"
    return f"{payload.encode('utf-8').hex()}.{_session_sign(payload)}"


def _verify_login(username: str, password: str) -> bool:
    if not username or not password:
        return False
    user_ok = hmac.compare_digest(username, _auth_ctx.get("user", ""))
    pw_ok = hmac.compare_digest(
        _auth_hash(_auth_ctx.get("secret", ""), password),
        _auth_ctx.get("pass_hash", ""),
    )
    return user_ok and pw_ok


def _cookie_secure() -> bool:
    return os.environ.get("WEBUI_COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes", "on")


@app.before_request
def _require_auth():
    """除登录页/登录接口外，所有页面与 API 都要求有效会话；否则 API 回 401、页面跳登录。"""
    if request.method == "OPTIONS":
        return None
    if request.path in ("/login", "/favicon.ico") or request.path == "/api/login":
        return None
    if _session_valid(request.cookies.get("webui_session")):
        return None
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "message": "未登录或会话已过期"}), 401
    return redirect("/login")


_LOGIN_PAGE = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>登录 · 社交媒体运营面板</title>
<style>
  :root{color-scheme:dark}
  *{box-sizing:border-box;margin:0}
  body{min-height:100vh;display:flex;align-items:center;justify-content:center;background:linear-gradient(160deg,#0f1626,#1b2436 55%,#16203a);font-family:-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;color:#e6ecf7}
  .card{width:340px;background:rgba(23,32,52,.9);border:1px solid #2b3a5e;border-radius:16px;padding:34px 30px 26px;box-shadow:0 18px 50px rgba(0,0,0,.45)}
  h1{font-size:19px;margin-bottom:4px;letter-spacing:.5px}
  .sub{font-size:12px;color:#8fa3c8;margin-bottom:24px}
  label{display:block;font-size:12px;color:#9fb2d6;margin:14px 0 6px}
  input{width:100%;padding:10px 12px;border:1px solid #33456e;border-radius:9px;background:#101a30;color:#eef;font-size:14px;outline:none}
  input:focus{border-color:#4f7cff}
  button{width:100%;margin-top:22px;padding:11px;border:0;border-radius:9px;background:linear-gradient(90deg,#3a66ff,#5b8cff);color:#fff;font-size:15px;cursor:pointer}
  button:disabled{opacity:.6;cursor:not-allowed}
  .err{display:none;margin-top:14px;font-size:12.5px;color:#ff8d8d;text-align:center}
  .tip{margin-top:18px;font-size:11.5px;color:#5f739b;text-align:center;line-height:1.6}
</style>
</head>
<body>
<div class="card">
  <h1>社交媒体运营面板</h1>
  <div class="sub">登录后管理多平台账号与发布任务</div>
  <label for="u">用户名</label><input id="u" autocomplete="username" autofocus>
  <label for="p">密码</label><input id="p" type="password" autocomplete="current-password">
  <div class="err" id="err"></div>
  <button id="btn">登 录</button>
  <div class="tip">账号密码未设置时，首次启动会自动生成并打印到容器日志；<br>如忘记密码，见部署文档或删除共享卷中的 .webui-auth.json 后重启</div>
</div>
<script>
const u=document.getElementById('u'),p=document.getElementById('p'),err=document.getElementById('err'),btn=document.getElementById('btn');
function show(e){err.style.display=e?'block':'none';err.textContent=e||''}
async function login(){
  if(btn.disabled)return;
  btn.disabled=true;show('');
  try{
    const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u.value.trim(),password:p.value})});
    if(r.ok){location.href='/';return;}
    let msg='用户名或密码错误';
    try{const j=await r.json();if(j&&j.message)msg=j.message;}catch(e){}
    show(msg);
  }catch(e){show('网络错误，请重试');}
  btn.disabled=false;
}
btn.onclick=login;
u.addEventListener('keydown',e=>{if(e.key==='Enter')login()});
p.addEventListener('keydown',e=>{if(e.key==='Enter')login()});
</script>
</body>
</html>"""


@app.get("/login")
def login_page():
    return _LOGIN_PAGE


# 简单防爆破：同 IP 10 分钟内失败 8 次则锁定该 IP 5 分钟
_login_failures: dict[str, list[float]] = {}


@app.post("/api/login")
def api_login():
    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    ip = request.remote_addr or "?"
    now = time.time()
    fails = [t for t in _login_failures.get(ip, []) if now - t < 600]
    if len(fails) >= 8:
        _login_failures[ip] = fails
        log.warning("WebUI 登录限速触发（暴力破解防护）: ip=%s", ip)
        return jsonify({"ok": False, "message": "失败次数过多，请 5 分钟后再试"}), 429
    if not _verify_login(username, password):
        fails.append(now)
        _login_failures[ip] = fails[-8:]
        log.warning("WebUI 登录失败: ip=%s", ip)
        return jsonify({"ok": False, "message": "用户名或密码错误"}), 401
    _login_failures.pop(ip, None)
    resp = jsonify({"ok": True, "user": _auth_ctx["user"]})
    resp.set_cookie(
        "webui_session", _session_cookie_value(),
        max_age=7 * 86400, httponly=True, samesite="Lax", path="/",
        secure=_cookie_secure(),
    )
    log.info("WebUI 登录成功: user=%s ip=%s", username, ip)
    return resp


@app.post("/api/session/logout")
def api_session_logout():
    resp = jsonify({"ok": True})
    resp.delete_cookie("webui_session", path="/")
    return resp


@app.get("/api/auth/info")
def api_auth_info():
    return jsonify({
        "ok": True, "user": _auth_ctx.get("user", ""),
        "source": _auth_ctx.get("source", ""),
    })


@app.post("/api/auth/password")
def api_auth_password():
    """管理员修改密码（需要先通过会话；旧密码校验通过后替换 HMAC 摘要）。"""
    body = request.get_json(silent=True) or {}
    old = body.get("old_password") or ""
    new = (body.get("new_password") or "").strip()
    if not _verify_login(_auth_ctx.get("user", ""), old):
        return jsonify({"ok": False, "message": "当前密码不正确"}), 400
    if len(new) < 8:
        return jsonify({"ok": False, "message": "新密码至少 8 位"}), 400
    _auth_ctx["pass_hash"] = _auth_hash(_auth_ctx["secret"], new)
    _auth_ctx["ui_changed"] = True  # 标记：重启后以 UI 改的密码为准，不被 .env 覆盖
    _auth_save(_auth_ctx)
    log.info("WebUI 管理员密码已修改: user=%s", _auth_ctx["user"])
    return jsonify({"ok": True, "message": "密码已更新，下次登录请用新密码"})


_init_auth()

# ---------------- 登录态检测与浏览器资源调度 ----------------
# 登录态缓存：避免每次轮询都开浏览器。仅容器化部署（v2）：
# 每个有头 Chromium 常驻约 500MB，受 browser-gui 守护进程 MAX_INSTANCES=1
# （严格串行）约束，检测改由后台串行队列逐账号执行——与任务/发布错峰，
# 避免并发拉起多个浏览器实例把内存打爆，缓存 120 秒。
_status_cache: dict[str, tuple[float, bool]] = {}
_status_lock = threading.RLock()
_STATUS_TTL = 120.0
_status_queue: "queue.Queue[str]" = queue.Queue()   # 待检测账号 key（后台串行消费）
_status_pending: set[str] = set()                   # 已在队列/检测中的 key（去重）
_status_recent: dict[str, float] = {}               # 本进程最近检测完成的账号 → 时间戳
_status_worker_started = False

# 人工接管窗口：WebUI 点「去远程登录」打开某账号窗口后，接管窗口（10 分钟）内：
#   1) 跳过该账号的自动登录检测（避免后台导航把用户正在扫码/拖滑块打断）；
#   2) 该账号窗口已被守护进程 pin，其它账号的检测也不再新起实例——
#      避免为检测其它账号而临时突破 MAX_INSTANCES、把内存顶到 2 个浏览器。
# 窗口结束后的下一轮 dashboard 轮询会自动补齐所有账号状态。
_takeover_until: dict[str, float] = {}
_TAKEOVER_WINDOW_S = 10 * 60
# 用户手动点「立即检测」的账号 key：后台检测线程放行一次（接管窗口内也执行），
# 用于“刚在远程桌面登录完 → 点 ⟳ 主动刷新”，不用等缓存/窗口过期。
_force_check: set[str] = set()


def _is_takeover_active(key: str) -> bool:
    """该账号是否正处于人工接管窗口（开着远程桌面操作中）。"""
    return _takeover_until.get(key, 0) > time.time()


def _automation_busy() -> bool:
    """是否有任务/手动发布正在占用浏览器（此时后台检测让路，避免互相淘汰实例）。"""
    if any(t.get("running") for t in _task_runs.values()):
        return True
    return any(r.get("running") for r in _publish_runs.values())


def _enqueue_status(key: str) -> None:
    with _status_lock:
        if key in _status_pending:
            return
        _status_pending.add(key)
    _status_queue.put(key)


def _browser_account(platform: str, profile: str | None) -> str:
    """WebUI 侧账号 key → 守护进程侧浏览器账号名。

    WebUI 用「platform/profile」表示命名账号、守护浏览器目录/账号名统一用
    「platform__profile」；默认账号两者均为 platform。此处统一成守护侧格式，
    供 _mgr_has_active_elsewhere / _status_recent 与守护返回的 account 比较。
    """
    return f"{platform}__{profile}" if profile else platform


def _mgr_has_active_elsewhere(key: str) -> bool:
    """browser-gui 守护里是否存在『别的账号』浏览器正在被活跃使用（最近 90 秒）。

    agent 的 BrowserPool 持有浏览器期间每 45 秒向守护心跳续租，因此守护侧
    idle_s 可反映"是否有其它进程（任务/发布/调度器）正在干活"。此时 WebUI
    不新起实例做登录检测——避免为测登录把正在运行任务的浏览器淘汰掉。

    注意排除自身：本进程串行检测队列刚用完的上一站实例（_status_recent 内、
    95 秒内完成的）不算外部占用，否则整个串行队列会被自己挡停。
    """
    if not _settings.browser_mgr_url:
        return False
    want = key.replace("/", "__")   # 守护侧账号名格式（platform__profile）
    try:
        from .skills.uploader.browser import BrowserManagerClient

        browsers = asyncio.run(BrowserManagerClient(_settings.browser_mgr_url).list_browsers())
    except Exception:  # noqa: BLE001
        return False
    now = time.time()
    with _status_lock:
        recent = dict(_status_recent)
    for b in browsers:
        acc = b.get("account")
        if acc == want or not b.get("running"):
            continue
        if acc in recent and now - recent[acc] < 95:
            continue  # 本进程刚检测过的实例（串行队列上一站）
        idle = b.get("idle_s")
        if idle is None or idle < 90:
            return True
    return False


def _ensure_status_worker() -> None:
    global _status_worker_started
    with _status_lock:
        if _status_worker_started:
            return
        _status_worker_started = True
    threading.Thread(target=_status_worker, name="login-status-worker", daemon=True).start()


def _status_worker() -> None:
    """后台串行登录检测：一次只检测一个账号（对应浏览器守护 MAX_INSTANCES=1 的
    严格串行资源模型），任务/发布/人工接管期间自动跳过让路。"""
    while True:
        key = _status_queue.get()
        try:
            if "/" in key:
                platform, profile = key.split("/", 1)
                profile = None if profile == "默认" else profile
            else:
                platform, profile = key, None
            # 手动「立即检测」→ 放行一次（接管窗口内也可执行），否则任务/发布/接管让路
            force = False
            with _status_lock:
                if key in _force_check:
                    _force_check.discard(key)
                    force = True
            if _automation_busy():
                continue
            if _is_takeover_active(key) and not force:
                continue
            if _mgr_has_active_elsewhere(key):
                continue
            ok = False
            try:
                ok = _sync_check_login(platform, profile)
            finally:
                # 无论成功与否，该账号浏览器都算「本进程使用过」——
                # 让串行队列的下一站允许正常淘汰它，而不是误判为外部占用。
                # 注意落盘用守护侧账号名（platform__profile），与守护返回的
                # account 字段一致，否则命名账号会被误判为「外部占用」而挡停队列。
                with _status_lock:
                    _status_recent[_browser_account(platform, profile)] = time.time()
            with _status_lock:
                _status_cache[key] = (time.time(), ok)
        except Exception as exc:  # noqa: BLE001
            log.warning("后台登录态检测异常 [%s]: %s", key, exc)
        finally:
            with _status_lock:
                _status_pending.discard(key)


def _sync_check_login(platform: str, profile: str | None) -> bool:
    """同步执行一次登录检测（asyncio.run 连接浏览器，结束后断开）。"""
    from .skills.platform_uploader import PlatformUploader

    async def _check() -> bool:
        uploader = PlatformUploader(_settings)
        try:
            return await _check_login_async(uploader, platform, profile)
        finally:
            await uploader.close()

    try:
        return asyncio.run(_check())
    except Exception as exc:  # noqa: BLE001
        log.warning("登录态检测执行失败 [%s/%s]: %s", platform, profile, exc)
        return False


def _account_key(platform: str, profile: str | None) -> str:
    return f"{platform}/{profile}" if profile else platform


def discover_accounts() -> dict[str, list[dict]]:
    """扫描 state/browsers/ 目录，发现各平台账号。

    目录名规则：<platform>（默认账号）或 <platform>__<profile>。
    """
    result: dict[str, list[dict]] = {}
    if not _browsers_dir.exists():
        return result
    for entry in sorted(_browsers_dir.iterdir()):
        if not entry.is_dir():
            continue
        name = entry.name
        if "__" in name:
            platform, profile = name.split("__", 1)
        else:
            platform, profile = name, None
        result.setdefault(platform, []).append(
            {"profile": profile or "默认", "key": _account_key(platform, profile), "dir": str(entry)}
        )
    return result


async def _check_login_async(uploader, platform: str, profile: str | None) -> bool:
    """检测登录态（可被 asyncio 并行调用）。"""
    try:
        return await uploader.check_login(platform, profile)
    except Exception as exc:  # noqa: BLE001
        log.warning("登录态检测失败 [%s/%s]: %s", platform, profile, exc)
        return False


def login_status(platform: str, profile: str | None) -> bool:
    """带缓存的登录态检测（同步入口）。

    统一走后台串行队列（不阻塞接口，也不并发拉起浏览器实例）；首次查询无缓存
    返回 False，真实状态由队列逐账号补齐，前端轮询即可看到。
    """
    key = _account_key(platform, profile)
    now = time.time()
    with _status_lock:
        cached = _status_cache.get(key)
        if cached and now - cached[0] < _STATUS_TTL:
            return cached[1]
        stale = cached[1] if cached else False

    if not _settings.browser_mgr_url:
        log.warning("未配置 BROWSER_MGR_URL：本项目仅支持容器化 Browser-GUI 部署")
        return stale
    # 任务/发布正在占用 → 让路，避免互相淘汰实例
    if _automation_busy():
        return stale
    # 该账号正处于人工接管窗口 → 跳过检测（避免把用户正在登录的页面切走）
    if _is_takeover_active(key):
        return stale
    # 其它账号接管中：其浏览器窗口已被 pin，实例名额被占满，
    # 此时不再新起实例检测（避免临时突破 MAX_INSTANCES 造成双实例内存峰值）
    if any(_is_takeover_active(k) for k in list(_takeover_until)):
        return stale
    _ensure_status_worker()
    _enqueue_status(key)
    return stale


# ---------------- 任务执行 ----------------
def _task_worker(task_name: str, result_holder: dict) -> None:
    """后台线程：执行一次任务（带实时进度回调；支持 tasks.yaml 里任意自定义任务）。"""
    async def _run():
        scheduler = Scheduler(_settings)
        scheduler.register("daily_post", "0 9 * * *", DailyPostTask())
        scheduler.register("maintain_traffic", "*/30 8-23 * * *", MaintainTrafficTask())
        # WebUI/AI 工坊自定义任务：按 tasks.yaml 里的 type 实例化同名任务
        cfg = ((_read_tasks_yaml().get("tasks") or {}).get(task_name) or {})
        if cfg:
            from .scheduler.cron import _build_task_from_type

            built = _build_task_from_type(cfg.get("type"))
            if built is not None and task_name not in scheduler._tasks:
                scheduler.register(task_name, cfg.get("cron") or "0 9 * * *", built)

        def _progress_cb(step_entry: dict) -> None:
            result_holder["steps"].append(step_entry)
            result_holder["current_step"] = step_entry.get("step", "")

        await scheduler.run_once(task_name, progress_cb=_progress_cb)
        result_holder["done"] = True
        result_holder["running"] = False
        result_holder["current_step"] = ""

    try:
        asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        result_holder["done"] = True
        result_holder["running"] = False
        result_holder["error"] = str(exc)


_task_runs: dict[str, dict] = {}


def start_task(task_name: str) -> dict:
    """触发任务（后台线程，防重复）。"""
    existing = _task_runs.get(task_name)
    if existing and existing.get("running"):
        return {"ok": False, "message": "该任务正在执行中"}
    holder = {
        "running": True, "done": False, "error": "",
        "started_at": time.time(), "steps": [], "current_step": "",
    }
    _task_runs[task_name] = holder
    thread = threading.Thread(target=_task_worker, args=(task_name, holder), daemon=True)
    thread.start()
    return {"ok": True}


# ---------------- API ----------------
def _effective_novnc_url() -> str:
    """对外可达的 noVNC 地址（用户浏览器视角）。

    优先用 .env 的 NOVNC_URL；若未配置或配的是 localhost/127.0.0.1（容器默认），
    则按「用户当前访问 WebUI 的 Host」动态推导（端口沿用 6080）——解决从局域网/
    公网 IP 打开面板时 iframe 指向访问者自己机器导致远程桌面连不上的问题。
    """
    conf = (_settings.novnc_url or "").strip()
    hostname = (request.host or "").split(":")[0] if request else ""
    if not conf:
        return f"http://{hostname or 'localhost'}:6080/vnc.html"
    if hostname and ("localhost" in conf or "127.0.0.1" in conf):
        try:
            from urllib.parse import urlparse

            port = urlparse(conf).port or 6080
        except Exception:  # noqa: BLE001
            port = 6080
        return f"http://{hostname}:{port}/vnc.html"
    return conf


@app.get("/")
def index():
    return send_from_directory(Path(__file__).parent / "webui_static", "index.html")


@app.get("/remote-desktop")
def remote_desktop():
    """容器化部署的网页远程桌面（noVNC 全屏入口）：登录卡在滑块/扫码时，
    点这里直接看到 browser-gui 里真实的浏览器桌面，手动完成后回 WebUI 继续。

    地址由 _effective_novnc_url() 给出（NOVNC_URL 或按请求 Host 动态推导）。
    """
    novnc = _effective_novnc_url()
    if not novnc:
        return jsonify({
            "ok": False,
            "message": "未配置 NOVNC_URL（browser-gui 的 noVNC 地址）。"
                       "Docker 部署请设置环境变量 NOVNC_URL=http://<你的域名或IP>:6080/vnc.html",
        }), 404
    if not novnc.startswith(("http://", "https://", "/")):
        novnc = "http://" + novnc
    page = f"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>远程桌面 · 人工接管</title>
<style>
  html,body{{margin:0;height:100%;background:#111;font-family:-apple-system,'Segoe UI','PingFang SC',sans-serif}}
  .bar{{display:flex;align-items:center;gap:12px;padding:8px 14px;background:#1b1b1f;color:#eee;font-size:13px;border-bottom:1px solid #333}}
  .bar b{{color:#6ee7a8}}
  .bar a{{color:#8ab4ff;text-decoration:none;margin-left:auto}}
  iframe{{display:block;width:100vw;height:calc(100vh - 38px);border:0}}
</style>
</head>
<body>
<div class="bar">
  <b>远程桌面（noVNC）</b>
  <span>输入 VNC 密码后进入桌面（密码在 WebUI「设置 → 远程桌面密码」查看/修改）。若桌面有多个窗口，请先点击打开标题含对应平台（如 douyin / xiaohongshu / zhihu）的浏览器窗口，再扫码 / 拖滑块 / 输验证码。完成后关闭本页，回 WebUI 稍候即可看到登录状态刷新。</span>
  <a href="/" target="_top">← 返回运营面板</a>
</div>
<iframe src="{html.escape(novnc, quote=True)}" allow="fullscreen"></iframe>
</body>
</html>"""
    return page


@app.get("/api/status")
def api_status():
    """账号列表 + 登录状态 + 活跃登录/任务状态。"""
    from concurrent.futures import ThreadPoolExecutor

    accounts = discover_accounts()

    def _detect(item: tuple) -> dict:
        platform, acct = item
        profile = None if acct["profile"] == "默认" else acct["profile"]
        ok = login_status(platform, profile)
        return {**acct, "logged_in": ok}

    statuses: dict[str, list] = {}
    items = [(p, a) for p, accts in accounts.items() for a in accts]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_detect, items))
    for (p, a), res in zip(items, results):
        statuses.setdefault(p, []).append(res)
    # 平台能力（始终遍历全部 5 平台，含 0 账号的）
    from .skills.platform_uploader import PlatformUploader

    caps = {}
    try:
        uploader = PlatformUploader(_settings)
        for p in PLATFORM_NAMES:
            adapter = uploader.registry.get(p)
            caps[p] = adapter.capabilities if adapter else {}
    except Exception:  # noqa: BLE001
        pass
    return jsonify({
        "platforms": {p: {"name": PLATFORM_NAMES.get(p, p), "accounts": statuses.get(p, []),
                          "capabilities": caps.get(p, {})} for p in PLATFORM_NAMES},
        "tasks": {k: v for k, v in _task_runs.items()},
    })


@app.post("/api/profile")
def api_profile():
    """新建账号 profile（创建独立浏览器目录）。"""
    data = request.get_json(silent=True) or {}
    platform = data.get("platform", "")
    profile = (data.get("profile") or "").strip()
    if platform not in PLATFORM_NAMES or not profile:
        return jsonify({"ok": False, "message": "参数错误"}), 400
    if profile == "默认":
        return jsonify({"ok": False, "message": "请使用非'默认'的账号名"}), 400
    dir_path = _browsers_dir / f"{platform}__{profile}"
    dir_path.mkdir(parents=True, exist_ok=True)
    return jsonify({"ok": True, "dir": str(dir_path)})


@app.post("/api/logout")
def api_logout():
    """删除账号登录态（删除浏览器目录）。"""
    data = request.get_json(silent=True) or {}
    platform = data.get("platform", "")
    profile = data.get("profile") or None
    if profile == "默认":
        profile = None
    dir_path = _browsers_dir / (f"{platform}__{profile}" if profile else platform)
    key = _account_key(platform, profile)
    _status_cache.pop(key, None)
    _takeover_until.pop(key, None)
    with _status_lock:
        _status_pending.discard(key)
    if dir_path.exists():
        import shutil

        shutil.rmtree(dir_path, ignore_errors=True)
        return jsonify({"ok": True, "message": f"已删除 {platform}/{profile or '默认'} 的登录态"})
    return jsonify({"ok": False, "message": "目录不存在"}), 404


@app.post("/api/accounts/refresh")
def api_accounts_refresh():
    """手动「⟳ 立即检测」：清登录态缓存并强制后台立刻重新探测。

    - 支持单账号 {platform, profile} 或全部账号（都不传）；
    - 与自动检测的区别：接管窗口内也放行（用户登录完主动点刷新最典型）。
    """
    body = request.get_json(silent=True) or {}
    platform = (body.get("platform") or "").strip()
    profile = body.get("profile") or None
    if profile == "默认":
        profile = None
    if platform and platform not in PLATFORM_NAMES:
        return jsonify({"ok": False, "message": f"未知平台: {platform}"}), 400
    accounts = discover_accounts()
    if platform:
        rows = accounts.get(platform) or []
        if not rows:
            return jsonify({"ok": True, "message": f"{platform} 还没有账号，先在账号矩阵新建"})
        if profile and not any(a["profile"] == profile for a in rows):
            return jsonify({"ok": False, "message": f"账号 {platform}/{profile} 不存在"}), 400
        keys = [a["key"] for a in rows if not profile or a["profile"] == profile]
    else:
        keys = [a["key"] for plist in accounts.values() for a in plist]
    if not keys:
        return jsonify({"ok": True, "message": "还没有账号，先去账号矩阵新建吧"})
    if _automation_busy():
        return jsonify({"ok": False, "message": "当前有发布/任务在用浏览器，稍后再点 ⟳ 检测"})
    with _status_lock:
        for k in keys:
            _status_cache.pop(k, None)
            _force_check.add(k)
            _enqueue_status(k)
    _ensure_status_worker()
    return jsonify({"ok": True, "message": f"已对 {len(keys)} 个账号发起重新检测，绿点稍候更新"})
def api_run():
    data = request.get_json(silent=True) or {}
    task = data.get("task", "")
    cfg = ((_read_tasks_yaml().get("tasks") or {}).get(task) or {})
    if not cfg and task not in ("daily_post", "maintain_traffic"):
        return jsonify({"ok": False, "message": f"任务不存在: {task}"}), 400
    return jsonify(start_task(task))


@app.get("/api/reports")
def api_reports():
    reports_dir = (_settings.state_dir.parent / "logs" / "reports")
    out = []
    if reports_dir.exists():
        files = sorted(reports_dir.glob("*.json"), reverse=True)[:20]
        for f in files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                out.append({**data, "file": f.name})
            except Exception:  # noqa: BLE001
                continue
    return jsonify(out)


@app.get("/api/logs")
def api_logs():
    log_file = (_settings.state_dir.parent / "logs" / "agent.log")
    if not log_file.exists():
        return jsonify([])
    lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
    return jsonify(lines)


# ---------------- 自定义定时任务管理（config/tasks.yaml） ----------------
_TASKS_YAML = Path(__file__).resolve().parent.parent / "config" / "tasks.yaml"
_TASK_TYPES = {"daily_post", "maintain_traffic"}


def _read_tasks_yaml() -> dict:
    """读取 tasks.yaml；不存在返回空结构。"""
    if not _TASKS_YAML.exists():
        return {"tasks": {}}
    try:
        import yaml

        with open(_TASKS_YAML, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return data if isinstance(data, dict) else {"tasks": {}}
    except Exception as exc:  # noqa: BLE001
        log.warning("tasks.yaml 读取失败: %s", exc)
        return {"tasks": {}}


def _write_tasks_yaml(data: dict) -> None:
    """写回 tasks.yaml（PyYAML 写回会丢失注释，WebUI 管理任务时接受）。"""
    import yaml

    _TASKS_YAML.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )


@app.get("/api/tasks")
def api_tasks_list():
    """读取 tasks.yaml 的任务列表。"""
    data = _read_tasks_yaml()
    return jsonify({"ok": True, "tasks": data.get("tasks") or {}})


def _merge_task_body(existing: dict, body: dict) -> dict:
    """把 API 请求体合并进任务配置（cron/enabled/type/custom/业务字段）。"""
    existing["enabled"] = body.get("enabled", True)
    existing["cron"] = (body.get("cron") or "").strip()
    existing["type"] = body.get("type") or "daily_post"
    custom = body.get("custom_content")
    if custom:
        existing["custom_content"] = custom  # dict：内联定制内容
    elif "custom_content" in body:
        existing.pop("custom_content", None)  # 显式清空
    # AI 工坊/任务页下发的业务配置（覆盖式同步，显式传空也会覆盖）
    for key in ("platforms", "accounts", "drafts", "visibility", "mode",
                "image_count", "music", "media", "music_dir",
                "notes", "max_replies"):
        if key in body:
            existing[key] = body[key]
    return existing


@app.post("/api/tasks")
def api_tasks_add():
    """添加/更新定时任务（小白模式由前端生成 cron）。"""
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    cron = (body.get("cron") or "").strip()
    task_type = body.get("type") or "daily_post"
    if not name or " " in name or "/" in name:
        return jsonify({"ok": False, "message": "任务名称不能包含空格或 /"}), 400
    if len(cron.split()) != 5:
        return jsonify({"ok": False, "message": "cron 必须是 5 段：分 时 日 月 周"}), 400
    if task_type not in _TASK_TYPES:
        return jsonify({"ok": False, "message": f"未知任务类型: {task_type}"}), 400
    data = _read_tasks_yaml()
    tasks = data.setdefault("tasks", {})
    # 保留该任务已有的业务配置（platforms 等），只更新调度字段
    existing = tasks.get(name) or {}
    tasks[name] = _merge_task_body(existing, body)
    _write_tasks_yaml(data)
    log.info("已保存定时任务 [%s] cron=%s type=%s", name, cron, task_type)
    return jsonify({"ok": True, "message": f"任务 {name} 已保存"}), 200


@app.delete("/api/tasks/<path:name>")
def api_tasks_delete(name: str):
    """删除定时任务。"""
    data = _read_tasks_yaml()
    tasks = data.get("tasks") or {}
    if name not in tasks:
        return jsonify({"ok": False, "message": f"任务不存在: {name}"}), 404
    del tasks[name]
    _write_tasks_yaml(data)
    log.info("已删除定时任务 [%s]", name)
    return jsonify({"ok": True, "message": "已删除"})


# ---------------- AI 运营工坊 ----------------
# 生成任务在后台线程跑（LLM 较长），前端轮询 /api/studio/job/<id>
_studio_jobs: dict[str, dict] = {}


@app.get("/api/studio/presets")
def api_studio_presets():
    from . import studio

    return jsonify({"ok": True, "data": studio.INDUSTRY_PRESETS})


@app.post("/api/studio/generate")
def api_studio_generate():
    """一句话需求 → 异步生成运营定制包。"""
    from . import studio

    body = request.get_json(silent=True) or {}
    brief = (body.get("brief") or "").strip()
    if not brief:
        return jsonify({"ok": False, "message": "请先填写你的行业/店铺与需求"}), 400
    job_id = secrets.token_hex(6)
    holder = {"status": "running", "result": None, "error": "", "started_at": time.time()}
    _studio_jobs[job_id] = holder

    def _run() -> None:
        try:
            pack = studio.generate_pack(
                _settings, brief,
                [str(p) for p in (body.get("platforms") or [])],
                str(body.get("goal") or ""),
            )
            holder["result"] = pack
            holder["status"] = "done"
        except Exception as exc:  # noqa: BLE001
            holder["error"] = str(exc)
            holder["status"] = "error"

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "job": job_id})


@app.get("/api/studio/job/<job_id>")
def api_studio_job(job_id: str):
    holder = _studio_jobs.get(job_id)
    if not holder:
        return jsonify({"ok": False, "message": "任务不存在或已过期"}), 404
    return jsonify({"ok": True, "status": holder["status"], "result": holder["result"],
                    "error": holder["error"]})


@app.get("/api/studio/packs")
def api_studio_packs_list():
    from . import studio

    return jsonify({"ok": True, "data": studio.list_packs(_settings)})


@app.post("/api/studio/packs")
def api_studio_packs_save():
    from . import studio

    body = request.get_json(silent=True) or {}
    entry = {
        "id": str(body.get("id") or ""),
        "name": str(body.get("name") or "").strip(),
        "brief": str(body.get("brief") or "").strip(),
        "goal": str(body.get("goal") or "").strip(),
        "platforms": [str(p) for p in (body.get("platforms") or [])],
        "pack": body.get("pack") or {},
    }
    if not entry["name"] or not entry["pack"]:
        return jsonify({"ok": False, "message": "缺少档案名称或定制包内容"}), 400
    saved = studio.save_pack(_settings, entry)
    return jsonify({"ok": True, "data": saved})


@app.delete("/api/studio/packs/<path:pack_id>")
def api_studio_packs_delete(pack_id: str):
    from . import studio

    return jsonify({"ok": studio.delete_pack(_settings, pack_id), "message": "已删除"})


@app.get("/api/studio/media")
def api_studio_media():
    """素材清单：背景风格 / 自定义背景图 / 用户上传的音乐。"""
    from . import studio

    return jsonify({"ok": True, "data": studio.list_media(_settings)})


@app.post("/api/studio/upload")
def api_studio_upload():
    """上传背景图 / BGM（multipart: kind=music|backgrounds）。"""
    from . import studio

    kind = (request.form.get("kind") or "").strip()
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"ok": False, "message": "请选择文件"}), 400
    try:
        saved = studio.save_upload(_settings, kind, f.filename, f.read())
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "message": str(exc)}), 400
    return jsonify({"ok": True, "data": saved})


@app.delete("/api/studio/file")
def api_studio_file_delete():
    from . import studio

    kind = (request.args.get("kind") or "").strip()
    name = (request.args.get("name") or "").strip()
    try:
        ok = studio.delete_upload(_settings, kind, name)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "message": str(exc)}), 400
    return jsonify({"ok": ok, "message": "已删除" if ok else "文件不存在"})


@app.get("/api/studio/bg-preview")
def api_studio_bg_preview():
    """背景风格小样（PNG，浏览器/轮询前加时间戳防缓存）。"""
    from flask import Response

    from .skills.media_creator import render_background_preview

    style = (request.args.get("style") or "aurora").strip()
    try:
        img = render_background_preview(style, 216, 384)
    except Exception:  # noqa: BLE001
        from .skills.media_creator import CARD_BG_STYLES
        from .skills.media_creator import _card_palette
        from .skills.media_creator import _vertical_gradient

        img = _vertical_gradient(216, 384, [(0, _card_palette(0)[0]), (1, _card_palette(0)[1])])
    import io

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    resp = Response(buf.getvalue(), mimetype="image/png")
    resp.headers["Cache-Control"] = "private, max-age=600"
    resp.headers["Content-Disposition"] = "inline"
    return resp


@app.get("/api/studio/file/<kind>/<name>")
def api_studio_media_file(kind: str, name: str):
    """预览上传的背景图 / 播放 BGM（需登录，支持 Range）。"""
    from flask import send_file

    from .skills.media_creator import media_root

    if kind not in ("music", "backgrounds"):
        return jsonify({"ok": False, "message": "不支持的素材类型"}), 400
    safe = Path(name).name  # 防路径穿越
    target = media_root(_settings, kind) / safe
    if not target.exists():
        return jsonify({"ok": False, "message": "文件不存在"}), 404
    _mimes = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
              ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
              ".wav": "audio/wav", ".flac": "audio/flac"}
    mime = _mimes.get(target.suffix.lower(), "audio/mpeg")
    return send_file(str(target), mimetype=mime, conditional=True, max_age=3600)


@app.post("/api/studio/schedule")
def api_studio_schedule():
    """定制包 → 一键落地为 tasks.yaml 定时任务。"""
    from . import studio

    body = request.get_json(silent=True) or {}
    pack = body.get("pack") or {}
    if not pack:
        return jsonify({"ok": False, "message": "缺少定制包"}), 400
    name = (body.get("name") or "").strip()
    cron = (body.get("cron") or "").strip()
    if not name or " " in name or "/" in name:
        return jsonify({"ok": False, "message": "任务名称不能包含空格或 /"}), 400
    if len(cron.split()) != 5:
        return jsonify({"ok": False, "message": "cron 必须是 5 段：分 时 日 月 周"}), 400
    options = body.get("options") or {}
    payload = studio.task_payload_from_pack(pack, options)
    data = _read_tasks_yaml()
    tasks = data.setdefault("tasks", {})
    task_body = {
        "name": name, "cron": cron, "type": "daily_post", "enabled": body.get("enabled", True),
        **payload,
    }
    tasks[name] = _merge_task_body(tasks.get(name) or {}, task_body)
    _write_tasks_yaml(data)
    log.info("AI 工坊已创建任务 [%s] cron=%s pack=%s", name, cron, pack.get("pack_name"))
    return jsonify({"ok": True, "message": f"任务 {name} 已创建并启用"}), 200


# ---------------- 容器化人工接管（noVNC 远程桌面） ----------------
def _mark_takeover(key: str) -> None:
    """标记账号进入人工接管窗口：期间后台自动登录检测让路，避免导航打断登录。"""
    _takeover_until[key] = time.time() + _TAKEOVER_WINDOW_S


def _mgr_account_of(key: str) -> str:
    """WebUI key（douyin/主号）→ 守护进程账号名（douyin__主号）。"""
    if "/" in key:
        p, pr = key.split("/", 1)
        return _browser_account(p, None if pr == "默认" else pr)
    return _browser_account(key, None)


def _active_takeover_mgr_accounts() -> set[str]:
    """仍在人工接管窗口内（可能正被人工操作）的账号，守护进程命名。"""
    out: set[str] = set()
    now = time.time()
    for k, exp in list(_takeover_until.items()):
        if exp > now:
            out.add(_mgr_account_of(k))
    return out


def _close_idle_browsers_elsewhere(target_account: str) -> None:
    """开新接管前，关掉非目标账号、且已闲置 >60s 的浏览器实例（含历史 pin 残留），
    避免 noVNC 桌面堆积多个窗口、内存占用上升。

    跳过：正在被调度器/发布使用（有近期心跳，idle 小）与仍处接管窗口的账号。
    """
    try:
        from .skills.uploader.browser import BrowserManagerClient

        browsers = asyncio.run(BrowserManagerClient(_settings.browser_mgr_url).list_browsers())
    except Exception as e:  # noqa: BLE001
        log.warning("读取浏览器列表失败，跳过闲置实例清理: %s", e)
        return
    skip = _active_takeover_mgr_accounts() | {target_account}
    now = time.time()
    for b in browsers:
        if not (b or {}).get("running"):
            continue
        acc = (b or {}).get("account") or ""
        if not acc or acc in skip:
            continue
        idle = (b or {}).get("idle_s")
        if idle is None:  # 兼容旧守护进程：无 idle_s 用 last_used 兜底
            idle = now - ((b or {}).get("last_used") or now)
        if idle > 60:
            log.info("接管前关闭闲置浏览器实例 %s（idle %.0fs）", acc, idle)
            try:
                asyncio.run(BrowserManagerClient(_settings.browser_mgr_url).stop_browser(acc))
            except Exception as e:  # noqa: BLE001
                log.warning("关闭 %s 失败: %s", acc, e)


@app.post("/api/remote/prepare")
def api_remote_prepare():
    """人工接管准备：后台把该账号浏览器窗口拉到 noVNC 桌面并打开登录页，
    前端随后打开全屏远程桌面（/remote-desktop）让用户真实操作（拖滑块/扫码/验证码）。
    """
    if not _settings.browser_mgr_url:
        return jsonify({
            "ok": False, "message": "未配置 BROWSER_MGR_URL：本项目仅支持容器化部署，请用 docker compose 启动",
        }), 400
    body = request.get_json(silent=True) or {}
    platform = (body.get("platform") or "").strip()
    profile = body.get("profile") or None
    if profile == "默认":
        profile = None
    if platform not in PLATFORM_NAMES:
        return jsonify({"ok": False, "message": f"未知平台: {platform}"}), 400

    key = _account_key(platform, profile)
    # 60 秒内的重复点击直接放行：避免重复导航把用户正在登录的页面切走
    if _is_takeover_active(key) and _takeover_until.get(key, 0) > time.time() - 60:
        return jsonify({"ok": True, "reused": True, "key": key,
                        "message": "该账号浏览器已在桌面打开，请直接在远程桌面操作"})
    # 新开接管前先关掉早已闲置的其它浏览器实例（含历史 pin 残留）：
    # 既省内存，也不会出现“点一个登录，桌面弹出好几个页面”
    _close_idle_browsers_elsewhere(_browser_account(platform, profile))
    res = remote_mgr.prepare_managed(_settings, platform, profile)
    if res.get("ok"):
        # 不弹掉登录态缓存：接管窗口内轮询返回缓存旧值（已登录保持绿点），
        # 登录态的真实刷新交给「⟳ 立即检测」或窗口过期后的后台检测
        _mark_takeover(key)
    return jsonify({**res, "key": key})


# ---------------- VNC 远程桌面密码（WebUI 设置页可视化查看/修改） ----------------
def _mgr_http(method: str, path: str, body: dict | None = None) -> dict:
    """转发请求到 browser-gui 守护进程（同网络栈，http://127.0.0.1:9100）。"""
    import urllib.request

    mgr = (_settings.browser_mgr_url or "").rstrip("/")
    if not mgr:
        return {"ok": False, "message": "未配置 BROWSER_MGR_URL：本项目仅支持容器化部署"}
    data = json.dumps(body or {}).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"{mgr}{path}", data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    token = os.environ.get("GUARDIAN_TOKEN", "")
    if token:
        req.add_header("X-Guardian-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"浏览器守护请求失败: {exc}"}


@app.get("/api/vnc")
def api_vnc():
    """远程桌面（noVNC）密码信息：当前密码明文 + 来源 + 修改时间（仅容器化部署）。"""
    return jsonify(_mgr_http("GET", "/vnc"))


@app.post("/api/vnc/password")
def api_vnc_password():
    """修改远程桌面（noVNC）密码：立即生效，容器重启后仍保留（不丢）。"""
    body = request.get_json(silent=True) or {}
    password = (body.get("password") or "").strip()
    if not password:
        return jsonify({"ok": False, "message": "请输入新密码"}), 400
    return jsonify(_mgr_http("PUT", "/vnc", {"password": password}))


# ---------------- 模拟内容（无 Key 测试） ----------------
@app.post("/api/mock_draft")
def api_mock_draft():
    """生成一段模拟文案（可一键填入发布表单），不消费任何 token。"""
    from .skills.mock_content import make_mock_dict

    body = request.get_json(silent=True) or {}
    platform = (body.get("platform") or "").strip()
    if platform not in PLATFORM_NAMES:
        return jsonify({"ok": False, "message": f"未知平台: {platform}"}), 400
    return jsonify({"ok": True, **make_mock_dict(platform)})


def _mock_publish_worker(key: str, platform: str, profile: str | None, visibility: str) -> None:
    """后台线程：一键模拟测试发布（无 AI/无 Key：模板文案 + Pillow 卡片/幻灯片视频）。"""
    from .skills.platform_uploader import PlatformUploader
    from .skills.media_creator import MediaCreator
    from .skills.mock_content import make_mock_draft
    from .models import PostPayload

    async def _run():
        uploader = PlatformUploader(_settings)
        task = _publish_runs[key]
        try:
            task["status"] = "generating"
            task["message"] = "正在生成模拟文案…"
            draft = make_mock_draft(platform)
            caps = {}
            adapter = uploader.registry.get(platform)
            if adapter:
                caps = adapter.capabilities or {}

            kwargs: dict = {
                "platform": platform,
                "title": draft.title,
                "content": draft.content,
                "tags": draft.tags or None,
                "visibility": visibility,
            }

            # 平台能力路由：图文平台→卡片图；纯视频平台→卡片合成幻灯片；
            # 纯文章平台（知乎）→ 仅文本。
            if caps.get("image") or caps.get("video"):
                media = MediaCreator(_settings)
                task["message"] = "正在生成测试卡片（Pillow，无需 AI）…"
                cards = await media.generate_cards(draft, image_count=3)
                image_paths = [c.path for c in cards]
                if caps.get("video") and not caps.get("image"):
                    task["message"] = "正在合成幻灯片测试视频（ffmpeg）…"
                    try:
                        video = await media.create_slideshow_video(image_paths)
                        kwargs["video"] = video.path
                    except Exception as exc:  # noqa: BLE001
                        task["status"] = "failed"
                        task["message"] = f"视频合成失败（需 ffmpeg）：{exc}"
                        return
                else:
                    kwargs["images"] = image_paths
            # else: 纯文章平台（知乎）直接文本发布

            task["status"] = "publishing"
            payload = PostPayload(**kwargs)
            result = await uploader.publish(payload, profile=profile)
            task["status"] = "published" if result.success else "failed"
            task["message"] = result.message
            task["url"] = result.url or ""
            task["title"] = draft.title
        except Exception as exc:  # noqa: BLE001
            task["status"] = "error"
            task["message"] = f"{type(exc).__name__}: {exc}"
        finally:
            task["done"] = True
            task["running"] = False
            task["finished_at"] = time.time()
            try:
                _PUBLISH_HISTORY.parent.mkdir(parents=True, exist_ok=True)
                with open(_PUBLISH_HISTORY, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({
                        "key": key, "platform": platform, "profile": profile,
                        "status": task["status"], "message": task["message"],
                        "source": "mock", "title": task.get("title", ""),
                        "started_at": task["started_at"], "finished_at": task["finished_at"],
                    }, ensure_ascii=False) + "\n")
            except Exception:  # noqa: BLE001
                pass
            await uploader.close()

    try:
        asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        task = _publish_runs.get(key)
        if task:
            task["status"] = "error"
            task["message"] = str(exc)
            task["done"] = True
            task["running"] = False


@app.post("/api/mock_publish")
def api_mock_publish():
    """一键模拟测试发布：无 AI/无 Key，走完整"登录→素材→发布"链路。"""
    body = request.get_json(silent=True) or {}
    platform = (body.get("platform") or "").strip()
    profile = body.get("profile") or None
    if profile == "默认":
        profile = None
    visibility = body.get("visibility", "private")
    if visibility not in ("public", "private", "only_friends"):
        visibility = "private"
    if platform not in PLATFORM_NAMES:
        return jsonify({"ok": False, "message": f"未知平台: {platform}"}), 400
    key = f"mock_{platform}__{profile or 'default'}_{int(time.time())}"
    _publish_runs[key] = {
        "key": key, "platform": platform, "profile": profile,
        "status": "generating", "message": "排队中…", "url": "", "title": "",
        "source": "mock", "started_at": time.time(), "finished_at": 0,
        "running": True, "done": False,
    }
    thread = threading.Thread(
        target=_mock_publish_worker,
        args=(key, platform, profile, visibility),
        daemon=True,
    )
    thread.start()
    return jsonify({"ok": True, "key": key, "message": "已开始模拟测试发布"})


# ---------------- 辅助函数 ----------------
def deployment_info() -> dict:
    """从 _settings 提取部署与模型池信息（密钥只回显是否存在，不回明文）。

    v2：仅支持容器化 Browser-GUI（managed），无 local/cdp 模式。
    """
    prov = resolve_llm_provider(_settings)
    return {
        "mode": "managed",
        "browser_mgr_url": _settings.browser_mgr_url or "",
        "novnc_url": _effective_novnc_url(),
        "state_dir": str(_settings.state_dir),
        "llm": {
            "providers": [
                {
                    "id": p["id"],
                    "name": p["name"],
                    "kind": p["kind"],
                    "base_url": p["base_url"],
                    "model": p["model"],
                    "has_key": bool(p.get("api_key")),
                }
                for p in _settings.llm_providers
            ],
            "active": _settings.llm_active,
            "active_name": (prov or {}).get("name", ""),
            "active_model": (prov or {}).get("model", ""),
            "ready": llm_ready(_settings),
        },
        "api_keys": {
            "fal": bool(_settings.fal_key),
            "stability": bool(_settings.stability_api_key),
        },
    }


def _load_reports(limit: int = 5) -> list[dict]:
    """读取最近 N 份任务报告。"""
    reports_dir = (_settings.state_dir.parent / "logs" / "reports")
    out = []
    if reports_dir.exists():
        files = sorted(reports_dir.glob("*.json"), reverse=True)[:limit]
        for f in files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                out.append({**data, "file": f.name})
            except Exception:  # noqa: BLE001
                continue
    return out


# ---------------- 新增 API ----------------
@app.get("/api/dashboard")
def api_dashboard():
    """仪表盘汇总：部署信息 + 全平台概览 + 统计 + 活跃任务/登录 + 最近报告。"""
    from concurrent.futures import ThreadPoolExecutor
    from .skills.platform_uploader import PlatformUploader

    accounts = discover_accounts()

    def _detect(item: tuple) -> dict:
        platform, acct = item
        profile = None if acct["profile"] == "默认" else acct["profile"]
        ok = login_status(platform, profile)
        return {**acct, "logged_in": ok}

    items = [(p, a) for p, accts in accounts.items() for a in accts]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_detect, items))
    statuses: dict[str, list] = {}
    for (p, a), res in zip(items, results):
        statuses.setdefault(p, []).append(res)

    # 全平台能力
    caps = {}
    try:
        uploader = PlatformUploader(_settings)
        for p in PLATFORM_NAMES:
            adapter = uploader.registry.get(p)
            caps[p] = adapter.capabilities if adapter else {}
    except Exception:  # noqa: BLE001
        pass

    total_accounts = len(items)
    total_logged_in = sum(1 for r in results if r.get("logged_in"))
    active_tasks = sum(1 for t in _task_runs.values() if t.get("running"))

    return jsonify({
        "deployment": deployment_info(),
        "platforms": {
            p: {
                "name": PLATFORM_NAMES.get(p, p),
                "accounts": statuses.get(p, []),
                "capabilities": caps.get(p, {}),
                "account_count": len(statuses.get(p, [])),
                "logged_in_count": sum(1 for a in statuses.get(p, []) if a.get("logged_in")),
            }
            for p in PLATFORM_NAMES
        },
        "totals": {
            "accounts": total_accounts,
            "logged_in": total_logged_in,
            "running_tasks": active_tasks,
        },
        "recent_reports": _load_reports(5),
        "active_tasks": {k: {kk: v for kk, v in v.items()} for k, v in _task_runs.items()},
        "ok": True,
    })


@app.get("/api/task_progress/<path:task_name>")
def api_task_progress(task_name: str):
    """实时任务步骤列表。"""
    holder = _task_runs.get(task_name)
    if not holder:
        return jsonify({"ok": False, "message": f"无任务 {task_name}"}), 404
    return jsonify({
        "ok": True,
        "task": task_name,
        "running": holder.get("running", False),
        "done": holder.get("done", False),
        "current_step": holder.get("current_step", ""),
        "steps": holder.get("steps", []),
        "started_at": holder.get("started_at", 0),
        "error": holder.get("error", ""),
    })


@app.get("/api/deployment")
def api_deployment():
    """部署信息 + 平台能力矩阵 + Linux 部署指南。"""
    from .skills.platform_uploader import PlatformUploader

    caps = {}
    try:
        uploader = PlatformUploader(_settings)
        for p in PLATFORM_NAMES:
            adapter = uploader.registry.get(p)
            caps[p] = {
                "name": PLATFORM_NAMES.get(p, p),
                "capabilities": adapter.capabilities if adapter else {},
            }
    except Exception:  # noqa: BLE001
        pass

    return jsonify({
        "ok": True,
        "deployment": deployment_info(),
        "platforms": caps,
        "linux_guide": [
            "1. 启动：cd docker && docker compose up -d --build（agent:8080 + browser-gui:6080/9100），浏览器打开 http://服务器IP:8080",
            "2. 配置模型：WebUI → 设置 → 模型与密钥：添加模型提供商（DeepSeek/通义/Kimi/任何 OpenAI 兼容端点）并填 API Key，选默认模型即生效",
            "3. 登录账号：账号矩阵 → 「远程登录」→ 打开全屏远程桌面（noVNC，密码见设置 → 远程桌面密码）→ 扫码/拖滑块/输验证码",
            "4. 登录态落盘 state/browsers/<平台>__<账号>/，容器重启不丢失；同机浏览器严格单实例（MAX_INSTANCES=1）串行切换，内存省",
            "5. 无 Key 也想先测链路？内容发布 → 「一键模拟测试发布」（模板文案 + Pillow 卡片，零 token）",
            "6. 常驻调度：WebUI → 定时任务（按 cron 自动执行每日发帖/流量维护）",
            "7. 多账号：账号矩阵 → 新建账号（每个 profile 独立浏览器目录/登录态）",
        ],
    })


# ---------------- 设置（运行时配置：LLM 模型池 / 生图密钥） ----------------
def _reload_settings() -> None:
    """保存设置后热重载配置（后续任务/发布/检测均使用新 Key/模型）。"""
    global _settings
    _settings = load_settings()


@app.get("/api/settings")
def api_settings_get():
    """读取设置（LLM 模型池 / 生效模型 / 生图密钥状态）。"""
    return jsonify({"ok": True, "settings": deployment_info()})


@app.post("/api/settings")
def api_settings_save():
    """保存设置（写 state/config_runtime.json 并热生效）。

    约定：API Key 字段为空或以「••••」开头 = 保持不变；生图密钥同理。
    """
    body = request.get_json(silent=True) or {}
    llm = body.get("llm") if isinstance(body.get("llm"), dict) else {}
    providers_in = llm.get("providers")
    if not isinstance(providers_in, list) or not providers_in:
        return jsonify({"ok": False, "message": "至少需要一个模型提供商"}), 400

    runtime = read_runtime_config(_settings.state_dir)
    old_llm = runtime.get("llm") if isinstance(runtime.get("llm"), dict) else {}
    old_by_id = {
        p.get("id"): p
        for p in (old_llm.get("providers") or [])
        if isinstance(p, dict) and p.get("id")
    }

    def _prev_key(pid: str) -> str:
        """改动前的密钥：运行时文件优先，其次 .env 的 DeepSeek 兜底。"""
        old = old_by_id.get(pid)
        if old and old.get("api_key"):
            return old["api_key"]
        if pid == "default":
            return _settings.deepseek_api_key
        return ""

    seen: set[str] = set()
    providers: list[dict] = []
    for p in providers_in:
        if not isinstance(p, dict):
            continue
        pid = str(p.get("id") or "").strip()
        name = str(p.get("name") or pid).strip()
        if not pid or pid in seen:
            continue
        base_url = str(p.get("base_url") or "").strip()
        model = str(p.get("model") or "").strip()
        if not base_url or not model:
            return jsonify({"ok": False, "message": f"模型「{name}」缺少 Base URL 或模型名"}), 400
        key = str(p.get("api_key") or "").strip()
        if key and not key.startswith(KEY_MASK_PREFIX):
            pass  # 新密钥
        else:  # 空 / 掩码 = 保持不变
            key = _prev_key(pid)
        seen.add(pid)
        providers.append({
            "id": pid,
            "name": name,
            "kind": str(p.get("kind") or "openai").strip() or "openai",
            "base_url": base_url,
            "api_key": key,
            "model": model,
        })

    active = str(llm.get("active") or "").strip()
    if active not in seen:
        active = providers[0]["id"]

    new_runtime = dict(runtime)
    new_runtime["llm"] = {"active": active, "providers": providers}
    # 生图密钥：空/掩码 = 保持不变（runtime 文件未写该键则回落 .env）
    for fld in ("fal_key", "stability_api_key"):
        val = body.get(fld)
        if isinstance(val, str) and val.strip() and not val.startswith(KEY_MASK_PREFIX):
            new_runtime[fld] = val.strip()
    write_runtime_config(_settings.state_dir, new_runtime)
    _reload_settings()
    log.info("设置已保存：LLM 池 %d 个提供商，默认模型 = %s", len(providers), active)
    return jsonify({
        "ok": True,
        "message": "设置已保存并生效",
        "settings": deployment_info(),
    })


# ---------------- 手动发布 ----------------
_publish_runs: dict[str, dict] = {}
_PUBLISH_HISTORY = Path(__file__).resolve().parent.parent / "logs" / "publish_history.jsonl"


def _publish_worker(key: str, platform: str, profile: str | None, payload_data: dict) -> None:
    """后台线程：手动发布。"""
    from .skills.platform_uploader import PlatformUploader
    from .models import PostPayload

    async def _run():
        uploader = PlatformUploader(_settings)
        task = _publish_runs[key]
        try:
            payload = PostPayload(
                platform=platform,
                title=payload_data.get("title", ""),
                content=payload_data.get("content", ""),
                tags=[t.strip() for t in payload_data.get("tags", "").split(",") if t.strip()] or None,
                images=payload_data.get("images") or None,
                video=payload_data.get("video") or None,
                visibility=payload_data.get("visibility", "private"),
            )
            task["status"] = "publishing"
            result = await uploader.publish(payload, profile=profile)
            task["status"] = "published" if result.success else "failed"
            task["message"] = result.message
            task["url"] = result.url or ""
        except Exception as exc:  # noqa: BLE001
            task["status"] = "error"
            task["message"] = f"{type(exc).__name__}: {exc}"
        finally:
            task["done"] = True
            task["running"] = False
            task["finished_at"] = time.time()
            # 写入历史
            try:
                _PUBLISH_HISTORY.parent.mkdir(parents=True, exist_ok=True)
                with open(_PUBLISH_HISTORY, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({
                        "key": key, "platform": platform, "profile": profile,
                        "status": task["status"], "message": task["message"],
                        "started_at": task["started_at"], "finished_at": task["finished_at"],
                    }, ensure_ascii=False) + "\n")
            except Exception:  # noqa: BLE001
                pass
            await uploader.close()

    try:
        asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        task = _publish_runs.get(key)
        if task:
            task["status"] = "error"
            task["message"] = str(exc)
            task["done"] = True
            task["running"] = False


@app.post("/api/publish")
def api_publish():
    """手动测试发布（multipart 文件上传）。"""
    platform = (request.form.get("platform") or "").strip()
    profile = request.form.get("profile") or None
    if profile == "默认":
        profile = None
    if platform not in PLATFORM_NAMES:
        return jsonify({"ok": False, "message": f"未知平台: {platform}"}), 400

    title = request.form.get("title", "")
    content = request.form.get("content", "")
    tags = request.form.get("tags", "")
    visibility = request.form.get("visibility", "private")

    # 保存上传文件
    upload_dir = _state_dir / "uploads" / str(int(time.time()))
    upload_dir.mkdir(parents=True, exist_ok=True)
    images = []
    video = None
    for f in request.files.getlist("images"):
        if f and f.filename:
            p = upload_dir / f.filename
            f.save(str(p))
            images.append(str(p))
    vf = request.files.get("video")
    if vf and vf.filename:
        p = upload_dir / vf.filename
        vf.save(str(p))
        video = str(p)

    key = f"publish_{platform}__{profile or 'default'}_{int(time.time())}"
    _publish_runs[key] = {
        "key": key, "platform": platform, "profile": profile,
        "status": "starting", "message": "", "url": "",
        "started_at": time.time(), "finished_at": 0,
        "running": True, "done": False,
    }
    thread = threading.Thread(
        target=_publish_worker,
        args=(key, platform, profile, {
            "title": title, "content": content, "tags": tags,
            "images": images, "video": video, "visibility": visibility,
        }),
        daemon=True,
    )
    thread.start()
    return jsonify({"ok": True, "key": key})


@app.get("/api/publish_status")
def api_publish_status():
    """活跃发布 + 最近 10 条发布历史。"""
    active = {k: {kk: v for kk, v in v.items()} for k, v in _publish_runs.items() if v.get("running")}
    recent = []
    if _PUBLISH_HISTORY.exists():
        lines = _PUBLISH_HISTORY.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in reversed(lines[-10:]):
            try:
                recent.append(json.loads(line))
            except Exception:  # noqa: BLE001
                continue
    return jsonify({"active": active, "recent": recent})


def main(host: str = "127.0.0.1", port: int = 8080) -> None:
    log.info("WebUI 启动: http://%s:%d", host, port)
    if not _settings.browser_mgr_url:
        log.warning("未配置 BROWSER_MGR_URL：本项目仅支持容器化 Browser-GUI 部署（docker compose up -d）")
    log.info("登录态检测走后台串行队列（MAX_INSTANCES=1 严格串行），首次状态补齐约需 1-2 分钟；"
             "人工登录请用「远程桌面接管」（noVNC），完成后回面板刷新状态；"
             "模型池当前 %d 个提供商，默认模型=%s",
             len(_settings.llm_providers),
             (resolve_llm_provider(_settings) or {}).get("model", "未配置"))
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="DeepSeek 社交媒体智能体 WebUI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    main(args.host, args.port)

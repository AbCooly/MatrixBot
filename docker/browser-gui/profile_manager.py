#!/usr/bin/env python3
"""browser-gui 账号浏览器守护进程（容器化部署的"浏览器即服务"）。

职责
  - 为每个「平台×账号」管理一个独立的有头 Chromium 实例（跑在 Xvfb 桌面上，
    noVNC 网页远程桌面实时可见、可人工接管滑块/扫码）
  - 每个实例：独立 user-data-dir（登录态落盘到共享卷）+ 独立 CDP 调试端口
  - 端口映射持久化到 state/.browser-ports.json，容器重启后自动恢复同端口/同登录态
  - 崩溃/被杀后由 agent 再次 ensure 自动拉起
  - 提供 VNC 密码查询与修改（WebUI「设置 → 远程桌面密码」调用，重启不丢）

资源模型（面向 2GB 小内存服务器）
  每个有头 Chromium 常驻约 450-500MB，因此默认**严格串行**：同一时刻最多
  MAX_INSTANCES（默认 1）个实例存活。ensure 新账号会先淘汰最久未用的旧实例，
  登录态已落盘、下次 ensure 自动恢复，不会丢失。

  无人使用超过 IDLE_TTL_S 秒即回收（回收看门狗线程），避免「开过就不关」的内存泄漏。
  人工接管（WebUI 远程桌面 / 驾驶舱）打开的实例通过 pin 保留 PIN_TTL_S 秒内
  不被淘汰、不被回收——直到接管超时自动解除（避免忘关一直驻留）。

HTTP API（agent 的 BrowserManagerClient 调用）
  GET    /health            守护健康检查
  GET    /browsers          列出全部账号浏览器实例（含 pin / 最近使用）
  POST   /browser           确保某账号浏览器运行，body: {"account": "douyin" | "douyin__主号"}
                            → 200 {"account","port","cdp_url","running"}
  POST   /browser           续租（仅刷新空闲计时）：body 加 {"heartbeat": true}
  POST   /browser           人工接管保留/解除：body 加 {"pin": true | false}
  DELETE /browser           关闭某账号浏览器（登录态目录保留）
  GET    /vnc               当前 VNC 密码信息（明文，仅供 WebUI 设置页展示/修改）
  PUT    /vnc               body {"password": "..."} 修改并立即生效（重启 x11vnc）

运行示例
  ADVERTISED_HOST=127.0.0.1 python3 profile_manager.py   # 0.0.0.0:9100
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---------------- 配置 ----------------
STATE_DIR = Path(os.getenv("STATE_DIR", "/app/state"))
DATA_ROOT = Path(os.getenv("BROWSER_DATA_DIR", str(STATE_DIR / "browsers")))
LOG_DIR = Path(os.getenv("BROWSER_LOG_DIR", str(STATE_DIR / "logs")))
PORTS_FILE = Path(os.getenv("PORTS_FILE", str(STATE_DIR / ".browser-ports.json")))
BASE_PORT = int(os.getenv("BASE_PORT", "9222"))
MAX_PORT = int(os.getenv("MAX_PORT", "9800"))
# 默认只绑回环：agent/scheduler 与本服务共享网络栈（docker-compose network_mode:
# service:browser-gui），127.0.0.1 即可互通；不暴露到容器外。如需外部直连可覆盖
# MANAGER_HOST=0.0.0.0（生产不建议，配合 GUARDIAN_TOKEN 使用）。
HOST = os.getenv("MANAGER_HOST", "127.0.0.1")
PORT = int(os.getenv("MANAGER_PORT", "9100"))
# 守护进程访问令牌（可选）：设置后除 /health（容器健康检查）外所有接口需携带
# 请求头 X-Guardian-Token。agent/scheduler 通过环境变量 GUARDIAN_TOKEN 自动附带。
GUARDIAN_TOKEN = os.getenv("GUARDIAN_TOKEN", "")
DISPLAY = os.getenv("DISPLAY", ":99")
ADVERTISED_HOST = os.getenv("ADVERTISED_HOST", "127.0.0.1")
CHROMIUM_EXTRA_ARGS = os.getenv("CHROMIUM_EXTRA_ARGS", "")

# 同时存活的浏览器实例上限（默认严格串行）。有头 Chromium 常驻约 450-500MB，
# 2GB 机器只能同时养 1 个；调大前请确认内存。<=0 表示不限制。
MAX_INSTANCES = int(os.getenv("MAX_INSTANCES", "1"))
# 空闲回收阈值（秒）：超过该时间无人 ensure/heartbeat 即关闭实例。<=0 表示不回收。
IDLE_TTL_S = float(os.getenv("BROWSER_IDLE_TTL", "900"))
# 空闲回收检查周期（秒）
REAP_INTERVAL_S = float(os.getenv("BROWSER_REAP_INTERVAL", "30"))
# 人工接管保留时长（秒）：WebUI「驾驶舱/远程桌面」打开某账号窗口后，
# 该实例在此时间内不被淘汰/回收（避免登录做到一半被切走）；超时自动解除。
PIN_TTL_S = float(os.getenv("BROWSER_PIN_TTL", "7200"))

# VNC 密码（browser-gui 容器内 x11vnc 使用）
VNC_SECRET_FILE = Path(os.getenv("VNC_SECRET_FILE", "/etc/vnc-secret"))
VNC_STATE_FILE = Path(os.getenv("VNC_STATE_FILE", str(STATE_DIR / ".vnc-password.json")))
VNC_MIN_LENGTH = int(os.getenv("VNC_MIN_LENGTH", "6"))
SUPERVISOR_CONF = os.getenv("SUPERVISOR_CONF", "/etc/supervisor/supervisord.conf")

_ACCOUNT_RE = re.compile(r"^[\w\u4e00-\u9fff\-]{1,80}$")  # 含中文账号名

# ---------------- 运行时状态 ----------------
_lock = threading.RLock()
_account_locks: dict[str, threading.Lock] = {}
_capacity_lock = threading.RLock()   # 串行化「淘汰 + 启动」，避免并发 ensure 抢名额互相淘汰
_procs: dict[str, subprocess.Popen] = {}
_started_at: dict[str, float] = {}
_last_used: dict[str, float] = {}   # 最近一次 ensure/heartbeat 时间（空闲回收依据）
_pinned: dict[str, float] = {}      # account -> pin 到期时间戳（人工接管保留）


def log(msg: str, *args) -> None:
    """printf 风格日志：log("x = %s", v)；无额外参数时按原样输出。"""
    text = msg % args if args else msg
    print(f"[browser-mgr] {text}", flush=True)


# ---------------- chromium 发现 ----------------
def find_chromium() -> str:
    env = os.getenv("CHROMIUM_BIN")
    if env and Path(env).exists():
        return env
    base = Path(os.getenv("PLAYWRIGHT_BROWSERS_PATH", "~/.cache/ms-playwright")).expanduser()
    hits = sorted(base.glob("chromium-*/chrome-linux/chrome"))
    if hits:
        return str(hits[-1])
    for c in ("/usr/bin/chromium", "/usr/bin/google-chrome", "/usr/bin/chromium-browser"):
        if Path(c).exists():
            return c
    raise RuntimeError(
        "未找到 Chromium。请安装 playwright 内核：python3 -m playwright install chromium，"
        "或设置 CHROMIUM_BIN 指定可执行文件路径"
    )


# ---------------- 端口分配（跨重启持久化） ----------------
def _load_ports() -> dict[str, int]:
    if PORTS_FILE.exists():
        try:
            data = json.loads(PORTS_FILE.read_text(encoding="utf-8"))
            return {k: int(v) for k, v in data.items() if int(v) <= MAX_PORT}
        except Exception:  # noqa: BLE001
            log("端口映射文件损坏，重建: %s", PORTS_FILE)
    return {}


def _save_ports(mapping: dict[str, int]) -> None:
    PORTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PORTS_FILE.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")


def _port_for(account: str) -> int:
    with _lock:
        ports = _load_ports()
        if account in ports:
            return ports[account]
        used = set(ports.values())
        for p in range(BASE_PORT, MAX_PORT + 1):
            if p not in used:
                ports[account] = p
                _save_ports(ports)
                log("分配端口 %s -> %d", account, p)
                return p
        raise RuntimeError("端口已耗尽（BASE_PORT..MAX_PORT）")


# ---------------- CDP 就绪探测 ----------------
def _cdp_ready(port: int, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=timeout):
            return True
    except Exception:  # noqa: BLE001
        return False


def _wait_ready(port: int, timeout: float = 45.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _cdp_ready(port):
            return True
        time.sleep(0.4)
    return False


# ---------------- 账号浏览器管理 ----------------
def _account_lock(account: str) -> threading.Lock:
    with _lock:
        if account not in _account_locks:
            _account_locks[account] = threading.Lock()
        return _account_locks[account]


def _is_pinned(account: str) -> bool:
    until = _pinned.get(account, 0)
    return until > time.time()


def _pin_until(account: str) -> float:
    return _pinned.get(account, 0)


def _alive(account: str) -> bool:
    proc = _procs.get(account)
    return proc is not None and proc.poll() is None


def ensure_browser(account: str, heartbeat: bool = False, pin: bool | None = None) -> dict:
    """确保账号浏览器运行，返回连接信息。

    heartbeat=True：仅刷新空闲计时（续租），不启动/不回收实例。
    pin=True/False：设置/解除人工接管保留（接管期间不被淘汰与闲置回收）。
    """
    if heartbeat:
        with _account_lock(account):
            port = _port_for(account)
            if not _alive(account):
                return {"account": account, "port": port,
                        "cdp_url": f"http://{ADVERTISED_HOST}:{port}", "running": False}
            _last_used[account] = time.time()
            return _info(account, port)

    if pin is True:
        _pinned[account] = time.time() + PIN_TTL_S
    elif pin is False:
        _pinned.pop(account, None)

    with _account_lock(account):
        port = _port_for(account)
        # 1) 已有存活进程 → 直接用（刷新空闲计时）
        if _alive(account):
            if _wait_ready(port, timeout=10):
                _last_used[account] = time.time()
                return _info(account, port)
            # 进程活着但 CDP 不可用 → 杀掉重启
            _kill(account, port)
        # 2) 进程没了但端口仍有孤儿浏览器在响应（上次崩溃，进程记录丢失）→ 认领
        elif _cdp_ready(port):
            _started_at.setdefault(account, time.time())
            log("认领孤儿浏览器实例 [%s] port=%d", account, port)
            _last_used[account] = time.time()
            return _info(account, port)

    # 3) 需要启动新实例 → 进入全局容量段：先淘汰最久未用（人工接管中的除外）
    with _capacity_lock:
        _evict_if_needed(account)
        with _account_lock(account):
            port = _port_for(account)
            # 等待容量锁期间可能已被其它请求拉起 → 复用
            if _alive(account):
                if _wait_ready(port, timeout=10):
                    _last_used[account] = time.time()
                    return _info(account, port)
                _kill(account, port)
            return _spawn(account, port)


def _evict_if_needed(wanted: str) -> None:
    """容量管理：存活实例达到 MAX_INSTANCES 时，让最久未用的实例让位。

    跳过「人工接管(pin)」中的实例——接管登录做到一半不能被切走。
    若所有实例都在接管中则临时突破上限（内存略超，接管结束自动回收）。
    """
    if MAX_INSTANCES <= 0 or _alive(wanted):
        return
    alive = [a for a in list(_procs) if _alive(a) and a != wanted]
    if len(alive) < MAX_INSTANCES:
        return
    candidates = [a for a in alive if not _is_pinned(a)]
    if not candidates:
        log("实例上限 %d 但现存实例均处于人工接管，临时突破上限启动 [%s]", MAX_INSTANCES, wanted)
        return
    victim = min(candidates, key=lambda a: _last_used.get(a, 0))
    log("实例上限 %d，淘汰最久未用实例 [%s]（登录态已落盘，切换回时自动恢复）让位 [%s]",
        MAX_INSTANCES, victim, wanted)
    stop_browser(victim)


def _info(account: str, port: int) -> dict:
    now = time.time()
    return {
        "account": account,
        "port": port,
        "cdp_url": f"http://{ADVERTISED_HOST}:{port}",
        "running": True,
        "pinned": _is_pinned(account),
        "pin_until": _pin_until(account) or None,
        "last_used": _last_used.get(account),
        "idle_s": round(now - _last_used[account], 1) if account in _last_used else None,
    }


def _clear_singleton_locks(datadir: Path) -> None:
    """清理上次崩溃/容器重建残留的 Chromium 单例锁。

    容器重建后 hostname 变化会让 Chromium 判定 profile 被"另一台机器上的进程"
    占用而拒绝启动（SingletonLock 里记录的是已消亡的 PID + 旧主机名），
    导致该账号永久无法拉起。启动时清掉这些锁文件即可恢复。
    """
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        p = datadir / name
        try:
            if p.is_symlink() or p.exists():
                p.unlink()
        except Exception:  # noqa: BLE001
            pass


def _spawn(account: str, port: int) -> dict:
    chromium = find_chromium()
    datadir = DATA_ROOT / account
    datadir.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _clear_singleton_locks(datadir)

    args = [
        chromium,
        f"--user-data-dir={datadir}",
        f"--remote-debugging-port={port}",
        # 注意：新版 Chromium 已移除 --remote-debugging-address，CDP 固定只监听
        # 127.0.0.1。跨容器访问靠 agent 与本服务共享网络栈（见 docker-compose.yml），
        # 不要试图让 CDP 监听 0.0.0.0（无效）。
        "--remote-allow-origins=*",    # 允许跨进程 WebSocket 连接 DevTools
        "--no-sandbox",                 # 容器内 root 运行必须
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-sync",
        # 抹掉“自动化/无头”特征（脚本运营检测常用），语言切中文减少站点误判
        "--disable-blink-features=AutomationControlled",
        "--lang=zh-CN",
        "--window-size=1280,900",
        "--window-position=40,30",
    ]
    if CHROMIUM_EXTRA_ARGS:
        args.extend(CHROMIUM_EXTRA_ARGS.split())
    args.append("about:blank")

    log("启动账号浏览器 [%s] port=%d datadir=%s", account, port, datadir)
    logf = open(LOG_DIR / f"browser-{account}.log", "ab")
    env = dict(os.environ)
    env["DISPLAY"] = DISPLAY
    try:
        _procs[account] = subprocess.Popen(
            args, stdout=logf, stderr=subprocess.STDOUT, env=env, start_new_session=True,
        )
    finally:
        logf.close()

    if not _wait_ready(port, timeout=60):
        err = _tail_log(account)
        log("浏览器启动失败 [%s]: %s", account, err)
        _kill(account, port)
        raise RuntimeError(
            f"账号浏览器 [%s] 启动失败：CDP 端口 %d 未就绪。日志: %s" % (account, port, err)
        )
    now = time.time()
    _started_at[account] = now
    _last_used[account] = now
    log("账号浏览器就绪 [%s] → http://%s:%d", account, ADVERTISED_HOST, port)
    return _info(account, port)


def _tail_log(account: str, n: int = 30) -> str:
    try:
        lines = (LOG_DIR / f"browser-{account}.log").read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:  # noqa: BLE001
        return "(无日志)"


def _kill(account: str, port: int) -> None:
    proc = _procs.pop(account, None)
    if proc and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass
    # 兜底：按调试端口精确清理（兼容孤儿进程）
    try:
        subprocess.run(
            ["pkill", "-f", f"--remote-debugging-port={port}"],
            capture_output=True, timeout=5,
        )
    except Exception:  # noqa: BLE001
        pass
    time.sleep(0.5)


def stop_browser(account: str) -> bool:
    """关闭账号浏览器（登录态目录保留，下次 ensure 自动恢复）。"""
    with _account_lock(account):
        port = _port_for(account)
        existed = _alive(account) or _cdp_ready(port, timeout=1.0)
        _kill(account, port)
        _started_at.pop(account, None)
        _last_used.pop(account, None)
        _pinned.pop(account, None)
        log("已关闭浏览器实例 [%s]", account)
        return existed


def delete_account(account: str) -> dict:
    """彻底删除账号：关闭实例 → 清内存状态 → 移除持久化端口映射 → 删除数据目录。

    与 stop_browser 的区别：stop_browser 保留登录态目录（下次 ensure 自动恢复）；
    本函数把数据目录与端口映射一并清除——WebUI「删除账号」后不应再被
    list_browsers / 目录扫描发现（修复“删了又被扫出来”的幽灵账号问题）。
    """
    import shutil
    with _account_lock(account):
        port = _port_for(account)
        was_alive = _alive(account) or _cdp_ready(port, timeout=1.0)
        # 1) 终止进程并等待真正退出，避免 Chromium 边删边写把目录“复活”
        proc = _procs.pop(account, None)
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                pass
        try:
            subprocess.run(
                ["pkill", "-f", f"--remote-debugging-port={port}"],
                capture_output=True, timeout=5,
            )
        except Exception:  # noqa: BLE001
            pass
        for _ in range(10):  # 等 CDP 端口完全释放（最长 ~5s）
            if not _cdp_ready(port, timeout=0.5):
                break
            time.sleep(0.5)
        _started_at.pop(account, None)
        _last_used.pop(account, None)
        _pinned.pop(account, None)
    # 2) 移除持久化端口映射（否则 list_browsers 一直列出幽灵账号）
    removed_map = False
    with _lock:
        ports = _load_ports()
        if account in ports:
            ports.pop(account, None)
            _save_ports(ports)
            removed_map = True
            log("删除账号 [%s]：已移除持久化端口映射", account)
    # 3) 删除数据目录（多次重试，确保无进程占用）
    datadir = DATA_ROOT / account
    for attempt in range(5):
        if not datadir.exists():
            break
        try:
            shutil.rmtree(datadir)
            log("删除账号 [%s]：数据目录已清除", account)
            break
        except Exception as exc:  # noqa: BLE001
            log("删除账号 [%s]：目录清除第 %d 次失败: %s", account, attempt + 1, exc)
            time.sleep(1.0)
    log("已彻底删除账号 [%s]（实例曾存活=%s，目录残留=%s）",
        account, was_alive, datadir.exists())
    return {
        "account": account,
        "stopped": bool(was_alive),
        "removed_map": removed_map,
        "dir_exists": datadir.exists(),
    }


def list_browsers() -> list[dict]:
    out = []
    with _lock:
        ports = _load_ports()
    now = time.time()
    for account in sorted(ports):
        port = ports[account]
        alive = _alive(account)
        ready = _cdp_ready(port, timeout=1.5)
        out.append({
            "account": account,
            "port": port,
            "cdp_url": f"http://{ADVERTISED_HOST}:{port}",
            "running": bool(alive or ready),
            "pinned": _is_pinned(account),
            "started_at": _started_at.get(account),
            "last_used": _last_used.get(account),
            "idle_s": round(now - _last_used[account], 1) if account in _last_used and alive else None,
        })
    return out


# ---------------- 闲置回收看门狗 ----------------
def _reaper_loop() -> None:
    """周期回收：超时未用的实例自动关闭（pin 接管中的除外，接管超时自动解除）。

    WebUI/任务每次使用浏览器都会刷新空闲计时（ensure / heartbeat），
    因此本回收不会误伤「正在运行中的发布/登录检测」。
    """
    while True:
        time.sleep(max(1.0, REAP_INTERVAL_S))
        try:
            _reap_once()
        except Exception as exc:  # noqa: BLE001 —— 看门狗不允许退出
            log("回收扫描异常: %s", exc)


def _reap_once() -> None:
    now = time.time()
    for account in list(_procs):
        if not _alive(account):
            _procs.pop(account, None)
            _started_at.pop(account, None)
            _last_used.pop(account, None)
            continue
        # 人工接管超时 → 自动解除 pin（防「开一次接管就永久驻留」）
        if _pinned.get(account, 0) and now >= _pinned[account]:
            log("人工接管超时（%.0f 秒），解除保留 [%s]", PIN_TTL_S, account)
            _pinned.pop(account, None)
        # 空闲超时 → 回收（登录态已落盘，下次 ensure 自动恢复）
        if _is_pinned(account) or IDLE_TTL_S <= 0:
            continue
        idle = now - _last_used.get(account, _started_at.get(account, now))
        if idle > IDLE_TTL_S:
            log("实例 [%s] 闲置 %.0f 秒（阈值 %.0f 秒），自动回收（登录态已落盘）",
                account, idle, IDLE_TTL_S)
            stop_browser(account)


# ---------------- VNC 密码管理 ----------------
def _iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _load_vnc_state() -> dict:
    """读取 WebUI/入口保存的 VNC 密码状态（明文）。"""
    if VNC_STATE_FILE.exists():
        try:
            data = json.loads(VNC_STATE_FILE.read_text(encoding="utf-8"))
            return {
                "password": str(data.get("password") or ""),
                "source": str(data.get("source") or "unknown"),
                "updated_at": str(data.get("updated_at") or ""),
            }
        except Exception:  # noqa: BLE001
            log("VNC 密码状态文件损坏: %s", VNC_STATE_FILE)
    # 兜底：x11vnc 密码文件（storepasswd 加密格式，无法还原明文）
    return {"password": "", "source": "env-or-unknown", "updated_at": ""}


def _save_vnc_state(password: str, source: str) -> None:
    VNC_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {"password": password, "source": source, "updated_at": _iso_now()}
    VNC_STATE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    try:
        os.chmod(VNC_STATE_FILE, 0o600)
    except Exception:  # noqa: BLE001
        pass


def get_vnc_info() -> dict:
    state = _load_vnc_state()
    return {
        "ok": True,
        "password": state["password"],
        "source": state["source"],
        "updated_at": state["updated_at"],
        "min_length": VNC_MIN_LENGTH,
        "secret_file": str(VNC_SECRET_FILE),
    }


def set_vnc_password(password: str) -> dict:
    """修改 VNC 密码并立即生效：写状态文件 → 生成 x11vnc 密码文件 → 重启 x11vnc。"""
    password = (password or "").strip()
    if len(password) < VNC_MIN_LENGTH:
        return {"ok": False, "message": f"密码至少 {VNC_MIN_LENGTH} 位"}
    # 1) 生成 x11vnc 密码文件（vnc 加密格式，供 -rfbauth 使用）
    r = subprocess.run(
        ["x11vnc", "-storepasswd", password, str(VNC_SECRET_FILE)],
        capture_output=True, timeout=15,
    )
    if r.returncode != 0:
        return {"ok": False, "message": f"x11vnc 密码文件生成失败: {r.stderr.decode(errors='replace')[:200]}"}
    try:
        os.chmod(VNC_SECRET_FILE, 0o600)
    except Exception:  # noqa: BLE001
        pass
    # 2) 记录明文状态（供 WebUI 展示/跨容器重启保留，entrypoint 优先读取它）
    _save_vnc_state(password, "webui")
    # 3) 重启 x11vnc 让新密码生效（supervisord autorestart，失败则 fallback 杀掉进程）
    restarted = False
    try:
        r = subprocess.run(
            ["supervisorctl", "-c", SUPERVISOR_CONF, "restart", "x11vnc"],
            capture_output=True, timeout=20,
        )
        restarted = r.returncode == 0
    except Exception:  # noqa: BLE001
        pass
    if not restarted:
        try:
            subprocess.run(["pkill", "-x", "x11vnc"], capture_output=True, timeout=5)
            restarted = True  # supervisord autorestart 会立刻拉起
        except Exception:  # noqa: BLE001
            pass
    log("VNC 密码已更新（来源 webui），x11vnc 重启: %s", "ok" if restarted else "手动生效")
    return {"ok": True, "message": "已更新并生效，noVNC 下次连接请使用新密码"}


# ---------------- HTTP 服务 ----------------
def _body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    if not length:
        return {}
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _respond(handler: BaseHTTPRequestHandler, code: int, payload: dict) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _valid_account(account: str) -> bool:
    if not account or not _ACCOUNT_RE.match(account):
        return False
    if ".." in account or account.startswith(".") or account in {"browsers", "logs"}:
        return False
    return True


def _as_flag(value, default: bool | None = None) -> bool | None:
    """把 JSON 里的 bool/字符串宽松转成三态（True/False/None）。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    return default


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # 精简访问日志
        log("HTTP %s" % (fmt % args))

    def _authorized(self) -> bool:
        """有 GUARDIAN_TOKEN 时校验 X-Guardian-Token（常量时间比较，防时序探测）。"""
        if not GUARDIAN_TOKEN:
            return True
        import hmac

        got = self.headers.get("X-Guardian-Token") or ""
        return hmac.compare_digest(got, GUARDIAN_TOKEN)

    def do_GET(self):
        path = self.path.rstrip("/")
        # /health 放行给容器健康检查；其余接口要求 token
        if path != "/health" and not self._authorized():
            _respond(self, 401, {"ok": False, "message": "guardian token 缺失或不正确"})
            return
        if path == "/health":
            _respond(self, 200, {"ok": True, "chromium": find_chromium()})
        elif path == "/browsers":
            _respond(self, 200, {"browsers": list_browsers()})
        elif path == "/vnc":
            _respond(self, 200, get_vnc_info())
        else:
            _respond(self, 404, {"error": "not found"})

    def do_POST(self):
        path = self.path.rstrip("/")
        if not self._authorized():
            _respond(self, 401, {"ok": False, "message": "guardian token 缺失或不正确"})
            return
        if path != "/browser":
            _respond(self, 404, {"error": "not found"})
            return
        data = _body(self)
        account = str(data.get("account", "")).strip()
        if not _valid_account(account):
            _respond(self, 400, {"error": f"非法账号名: {account!r}"})
            return
        try:
            heartbeat = _as_flag(data.get("heartbeat"), False) or False
            pin = _as_flag(data.get("pin"))  # None = 未指定，保持现状
            _respond(self, 200, ensure_browser(account, heartbeat=heartbeat, pin=pin))
        except Exception as exc:  # noqa: BLE001
            log("ensure 失败 [%s]: %s", account, exc)
            _respond(self, 500, {"error": str(exc)})

    def do_PUT(self):
        path = self.path.rstrip("/")
        if not self._authorized():
            _respond(self, 401, {"ok": False, "message": "guardian token 缺失或不正确"})
            return
        if path != "/vnc":
            _respond(self, 404, {"error": "not found"})
            return
        data = _body(self)
        try:
            _respond(self, 200, set_vnc_password(str(data.get("password") or "")))
        except Exception as exc:  # noqa: BLE001
            log("VNC 密码更新失败: %s", exc)
            _respond(self, 500, {"error": str(exc)})

    def do_DELETE(self):
        path = self.path.rstrip("/")
        if not self._authorized():
            _respond(self, 401, {"ok": False, "message": "guardian token 缺失或不正确"})
            return
        if path == "/browser":
            data = _body(self)
            account = str(data.get("account", "")).strip()
            if not _valid_account(account):
                _respond(self, 400, {"error": f"非法账号名: {account!r}"})
                return
            _respond(self, 200, {"account": account, "stopped": stop_browser(account)})
        elif path == "/profile":
            # 彻底删除账号（停实例 + 清端口映射 + 删数据目录），WebUI「删除账号」调用
            data = _body(self)
            account = str(data.get("account", "")).strip()
            if not _valid_account(account):
                _respond(self, 400, {"error": f"非法账号名: {account!r}"})
                return
            try:
                _respond(self, 200, delete_account(account))
            except Exception as exc:  # noqa: BLE001
                log("删除账号失败 [%s]: %s", account, exc)
                _respond(self, 500, {"error": str(exc)})
        else:
            _respond(self, 404, {"error": "not found"})


def main() -> int:
    for d in (DATA_ROOT, LOG_DIR, PORTS_FILE.parent):
        d.mkdir(parents=True, exist_ok=True)
    if not PORTS_FILE.exists():
        _save_ports({})
    log("chromium = %s", find_chromium())
    log("DATA_ROOT = %s | DISPLAY = %s | ADVERTISED_HOST = %s", DATA_ROOT, DISPLAY, ADVERTISED_HOST)
    log("资源模型: MAX_INSTANCES=%s（<=0 不限制） IDLE_TTL=%ss PIN_TTL=%ss",
        MAX_INSTANCES if MAX_INSTANCES > 0 else "∞", IDLE_TTL_S, PIN_TTL_S)
    # 启动闲置回收看门狗（daemon，不阻塞主服务）
    reaper = threading.Thread(target=_reaper_loop, name="browser-reaper", daemon=True)
    reaper.start()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    httpd.daemon_threads = True
    log("账号浏览器守护已启动: http://%s:%d", HOST, PORT)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

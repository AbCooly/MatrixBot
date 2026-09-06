#!/usr/bin/env bash
# browser-gui 容器入口：准备共享卷 + VNC 密码（WebUI 可改、容器重启后保留），再拉起 supervisord
set -euo pipefail

# 共享状态目录（与 agent 容器同一卷：登录态 / 浏览器端口映射 / VNC 密码 都落盘于此）
mkdir -p /app/state/browsers /app/state/logs
[ -f /app/state/.browser-ports.json ] || echo '{}' > /app/state/.browser-ports.json

VNC_STATE_FILE=/app/state/.vnc-password.json

# 解析最终 VNC 密码，并保证 /app/state/.vnc-password.json 存在（WebUI 设置页可读/改）。
# 优先级：WebUI 已保存（文件存在） > .env 的 VNC_PASSWORD > 本次随机。
# 容器重启时若文件存在则以文件为准——用户在 WebUI 改过的密码不会被 .env 覆盖。
resolve_vnc_password() {
  local pw
  if [ -f "$VNC_STATE_FILE" ]; then
    pw="$(python3 -c 'import json;print(json.load(open("/app/state/.vnc-password.json")).get("password",""))' 2>/dev/null || true)"
    if [ -n "$pw" ]; then
      VNC_PASSWORD="$pw"
      echo "[browser-gui] 使用 WebUI 保存的 VNC 密码（可在 WebUI「设置 → 远程桌面密码」修改）"
      return
    fi
  fi
  local src="env"
  VNC_PASSWORD="${VNC_PASSWORD:-}"
  if [ -z "$VNC_PASSWORD" ]; then
    VNC_PASSWORD="$(head -c 12 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 10)"
    src="auto"
    echo "[browser-gui] ⚠ VNC_PASSWORD 未设置，本次随机生成，请到容器日志查看：" >&2
  fi
  # 首次落盘（来源 env/auto），让 WebUI 能读到当前密码
  python3 - "$VNC_PASSWORD" "$src" <<'PY'
import json, os, sys, datetime
pw, src = sys.argv[1], sys.argv[2]
data = {"password": pw, "source": src,
        "updated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds")}
path = "/app/state/.vnc-password.json"
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
os.replace(tmp, path)
os.chmod(path, 0o600)
PY
  echo "[browser-gui] VNC 密码来源：${src}（登录 WebUI 后可在「设置 → 远程桌面密码」查看/修改）" >&2
}
resolve_vnc_password

# 生成 x11vnc 密码文件（供 -rfbauth 使用）
if ! x11vnc -storepasswd "$VNC_PASSWORD" /etc/vnc-secret 2>/dev/null; then
  echo "[browser-gui] 无法生成 VNC 密码文件，将以明文文件兜底（安全性低）" >&2
  printf '%s\n' "$VNC_PASSWORD" > /etc/vnc-secret
fi
chmod 600 /etc/vnc-secret

exec supervisord -n -c /etc/supervisor/supervisord.conf

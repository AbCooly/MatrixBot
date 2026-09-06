#!/usr/bin/env bash
# =============================================================================
# MatrixBot 一键部署脚本
#
# 用法（任选其一）：
#   1) 一行命令（推荐，可管道执行，不需要先 clone）：
#        bash <(curl -fsSL https://raw.githubusercontent.com/AbCooly/MatrixBot/main/install.sh)
#   2) 先 clone 再执行（喜欢看完整仓库的人）：
#        git clone https://github.com/AbCooly/MatrixBot.git
#        cd MatrixBot && bash install.sh
#   3) 仓库已有本地代码：
#        bash install.sh
#
# 自动完成：检测/安装 Docker → 拉取代码 → 生成随机密码的 .env →
#          准备 config/tasks.yaml → docker compose 构建并启动
#          （默认同时启动常驻 scheduler，任务页新建任务 30 秒内自动生效）
#
# 可调环境变量：
#   INSTALL_DIR=/srv/MatrixBot   代码安装目录（默认 $HOME/MatrixBot，仅在用一行命令时生效）
#   REPO_URL=...                 仓库地址（默认 GitHub；国内可指到加速镜像）
#   NO_SCHEDULER=1               不启动常驻调度器（只跑 WebUI）
#   SKIP_UPDATE=1                重跑时不 git pull
#   WEBUI_PORT=8080 NOVNC_PORT=6080 VNC_PORT=5900   覆盖端口（与 docker/.env 保持一致）
#
# 系统要求：Debian/Ubuntu/CentOS 的 x86_64 Linux（云端服务器最佳），内存建议 >= 2GB，
#           磁盘 >= 5GB；WSL2/macOS 亦可运行（macOS 需自行先装好 Docker Desktop）。
# =============================================================================
set -euo pipefail

log()  { printf '\033[1;32m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[install]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[install]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------- 参数与环境 ----------------
REPO_URL="${REPO_URL:-https://github.com/AbCooly/MatrixBot.git}"
INSTALL_DIR="${INSTALL_DIR:-$HOME/MatrixBot}"
SCHEDULER="${NO_SCHEDULER:-0}"
SKIP_UPDATE="${SKIP_UPDATE:-0}"
WEBUI_PORT="${WEBUI_PORT:-8080}"
NOVNC_PORT="${NOVNC_PORT:-6080}"
VNC_PORT="${VNC_PORT:-5900}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || echo "$PWD")"

have() { command -v "$1" >/dev/null 2>&1; }

random_hex() {
  if have openssl; then openssl rand -hex 12; else head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n'; fi
}

# ---------------- 0) 基础依赖：curl / git / docker ----------------
ensure_docker() {
  if have docker && docker info >/dev/null 2>&1; then
    if docker compose version >/dev/null 2>&1; then
      log "Docker 与 compose 已就绪：$(docker --version)，compose $(docker compose version --short)"
      return 0
    fi
    die "已检测到 docker 但缺少 docker compose 插件，请安装 docker-compose-plugin 后重试"
  fi
  warn "未检测到 Docker，尝试自动安装（官方脚本）…"
  local sudoer=""
  if [ "$(id -u)" != "0" ]; then
    have sudo || die "当前用户非 root 且无 sudo，无法自动安装 Docker。请手动安装后重跑。"
    sudoer="sudo"
  fi
  $sudoer sh -c "$(curl -fsSL https://get.docker.com)" \
    || die "Docker 自动安装失败。请手动安装：https://docs.docker.com/engine/install/"
  $sudoer systemctl enable --now docker >/dev/null 2>&1 || true
  docker compose version >/dev/null 2>&1 || die "Docker compose 插件未随 Docker 装好，请手动安装 docker-compose-plugin"
  log "Docker 安装完成：$(docker --version)"
}

# ---------------- 1) 代码：clone 或复用当前目录 ----------------
ensure_code() {
  local repo_root
  # 已在仓库 checkout（脚本与 docker/ 同目录）→ 就地使用
  if [ -f "$SCRIPT_DIR/docker/docker-compose.yml" ] && [ -f "$SCRIPT_DIR/config/tasks.example.yaml" ]; then
    repo_root="$SCRIPT_DIR"
    log "复用当前代码目录：$repo_root"
  else
    have git || die "未检测到 git，请先安装 git（apt install -y git / yum install -y git）"
    if [ -d "$INSTALL_DIR/.git" ]; then
      repo_root="$INSTALL_DIR"
      if [ "$SKIP_UPDATE" != "1" ]; then
        log "检测到已有代码，尝试更新…"
        git -C "$repo_root" pull --ff-only >/dev/null 2>&1 || warn "更新失败（忽略，继续使用现有代码）"
      fi
    else
      log "克隆代码到 $INSTALL_DIR …"
      mkdir -p "$(dirname "$INSTALL_DIR")"
      git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
      repo_root="$INSTALL_DIR"
    fi
  fi
  [ -f "$repo_root/docker/docker-compose.yml" ] || die "未找到 docker/docker-compose.yml，代码不完整"
  REPO_ROOT="$repo_root"
}

# ---------------- 2) 配置文件准备 ----------------
prepare_env() {
  local env_file="$REPO_ROOT/docker/.env"
  if [ ! -f "$env_file" ]; then
    cp "$REPO_ROOT/docker/.env.example" "$env_file"
    log "已生成 $env_file"
  fi

  # 自动补强密码：只在值为空或占位 change-me 时替换，重复执行不覆盖已填值
  local web_pass="" token="" vnc_pass=""
  if ! grep -Eq '^WEBUI_PASSWORD=.+$' "$env_file"; then web_pass="$(random_hex)"; fi
  if ! grep -Eq '^GUARDIAN_TOKEN=.+$' "$env_file"; then token="$(random_hex)"; fi
  if ! grep -Eq '^VNC_PASSWORD=.+$' "$env_file" \
     || grep -Eq '^VNC_PASSWORD=change-me' "$env_file"; then vnc_pass="$(random_hex)"; fi
  [ -z "$web_pass" ] || sed -i "s|^WEBUI_PASSWORD=.*|WEBUI_PASSWORD=$web_pass|" "$env_file"
  [ -z "$token" ]    || sed -i "s|^GUARDIAN_TOKEN=.*|GUARDIAN_TOKEN=$token|"   "$env_file"
  [ -z "$vnc_pass" ] || sed -i "s|^VNC_PASSWORD=.*|VNC_PASSWORD=$vnc_pass|"     "$env_file"
  WEBUI_PASSWORD_VALUE="$(grep -E '^WEBUI_PASSWORD=' "$env_file" | head -1 | cut -d= -f2- || true)"
  VNC_PASSWORD_VALUE="$(grep -E '^VNC_PASSWORD='     "$env_file" | head -1 | cut -d= -f2- || true)"

  # 任务清单：tasks.yaml 不存在则由示例生成（默认所有任务停用，安全）
  local tasks_yaml="$REPO_ROOT/config/tasks.yaml"
  if [ ! -f "$tasks_yaml" ]; then
    cp "$REPO_ROOT/config/tasks.example.yaml" "$tasks_yaml"
    log "已生成 $tasks_yaml（示例任务默认停用，需在 WebUI 配置后启用）"
  fi
}

# ---------------- 3) 拉起容器 ----------------
start_compose() {
  cd "$REPO_ROOT/docker"
  local compose_args=()
  if [ "$SCHEDULER" != "1" ]; then
    log "NO_SCHEDULER 未设置 → 默认同时启用常驻调度器（scheduler profile）"
    compose_args+=(--profile scheduler)
  fi
  log "构建并启动容器（首次构建需下载 Chromium/依赖，可能要几分钟）…"
  if [ "${NO_BUILD:-0}" = "1" ]; then
    docker compose "${compose_args[@]}" up -d
  else
    docker compose "${compose_args[@]}" up -d --build
  fi
}

wait_webui() {
  if ! have curl; then return 0; fi
  log "等待 WebUI 就绪（最多 3 分钟）…"
  local i
  for ((i = 0; i < 90; i++)); do
    if curl -fsS -o /dev/null "http://127.0.0.1:${WEBUI_PORT}/" 2>/dev/null; then
      log "WebUI 已就绪 ✅"
      return 0
    fi
    sleep 2
  done
  warn "WebUI 暂时没响应，请稍后刷新或 docker compose logs 查看"
}

# ---------------- 入口 ----------------
main() {
  log "MatrixBot 一键部署"
  log "仓库：$REPO_URL"
  [ -n "$(uname -s | tr 'A-Z' 'a-z' | grep -E 'linux|darwin')" ] || die "暂不支持的系统：$(uname -s)"
  ensure_docker
  ensure_code
  prepare_env
  start_compose
  wait_webui

  cat <<'EOF'

╔══════════════════════════════════════════════════════════════════════╗
║                     部署完成！接下来做 3 件事                          ║
╚══════════════════════════════════════════════════════════════════════╝

 ① 打开面板（建议用本机浏览器 + SSH 隧道访问，或放行端口后访问公网 IP）
      WebUI 面板 :8080 →  http://<服务器IP>:8080
      远程桌面   :6080 →  http://<服务器IP>:6080/vnc.html
EOF
  printf '  登录账号    : %s\n' "$(grep -E '^WEBUI_USERNAME=' "$REPO_ROOT/docker/.env" | head -1 | cut -d= -f2- || echo admin)"
  printf '  登录密码    : %s   ← 也可看 docker compose logs agent 里的随机密码\n' "${WEBUI_PASSWORD_VALUE:-（自动随机，见容器日志）}"
  printf '  VNC/远程桌面密码: %s   ← WebUI「设置 → 远程桌面密码」可随时改\n' "${VNC_PASSWORD_VALUE:-（自动随机，见 WebUI 设置）}"
  cat <<'EOF'

 ② 云服务器请放行端口并改回回环更安全：
      ufw allow 8080/tcp && ufw allow 6080/tcp
      （长期公网使用强烈建议 Nginx/Caddy 反代 HTTPS，见 README「安全」）
      .env 配置文件：docker/.env（已含随机密码，勿外传）

 ③ 首次使用：登录 WebUI →「设置」配 DeepSeek 等模型 Key →
   「账号矩阵」新建账号并用「去远程登录」扫码登录 →
   发布测试跑通后再到「任务调度 / AI 工坊」新建定时任务。

 定时任务说明：任务页/工坊创建的任务写入 config/tasks.yaml，scheduler 每 30 秒
 热加载，无需重启容器；仓库里自带的示例任务默认停用（安全）。

 日常命令（在 docker/ 目录）：docker compose logs -f    /   docker compose down
 卸载重装：docker compose down -v （会删除登录态，谨慎）
EOF
}

main "$@"

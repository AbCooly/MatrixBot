# browser-gui：容器化「浏览器即服务」
#   - Xvfb 虚拟桌面 + openbox 窗口管理（Chromium 以有头窗口运行，登录态可持久化）
#   - x11vnc + noVNC：把桌面推到网页（WebUI「远程桌面 · 人工接管」入口）
#   - profile_manager.py：账号浏览器守护（每个「平台×账号」一个独立 Chromium + CDP 端口）
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    DISPLAY=:99 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
# Chromium 默认走官方 cdn.playwright.dev（npmmirror 镜像已缺 1.49 构建文件）

# 国内网络构建优化：apt/pip/浏览器走国内镜像（避免直连/阿里云被限速断流）
RUN set -eux; \
    rm -f /etc/apt/sources.list.d/debian.sources; \
    printf 'deb http://mirrors.tuna.tsinghua.edu.cn/debian/ bookworm main contrib non-free non-free-firmware\ndeb http://mirrors.tuna.tsinghua.edu.cn/debian/ bookworm-updates main contrib non-free non-free-firmware\ndeb http://mirrors.tuna.tsinghua.edu.cn/debian-security bookworm-security main contrib non-free non-free-firmware\n' > /etc/apt/sources.list; \
    printf 'Acquire::Retries "6";\nAcquire::http::Timeout "120";\nAcquire::https::Timeout "120";\n' > /etc/apt/apt.conf.d/90network-retry; \
    apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip ca-certificates curl \
        xvfb x11vnc xauth openbox \
        novnc websockify supervisor procps tzdata \
        fonts-noto-cjk fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m pip install --no-cache-dir --retries 6 --break-system-packages -i https://pypi.tuna.tsinghua.edu.cn/simple/ "playwright==1.49.1"

# Chromium 内核（供守护进程拉起有头实例）
RUN python3 -m playwright install chromium \
    && python3 -m playwright install-deps chromium

WORKDIR /opt/browser-gui
COPY profile_manager.py ./profile_manager.py
COPY supervisord.conf /etc/supervisor/conf.d/browser-gui.conf
COPY entrypoint.sh ./entrypoint.sh
# Windows 下 git checkout 常把文件写成 CRLF，会破坏 shebang/supervisord 解析，
# 构建时统一转 LF（幂等，无副作用）
RUN sed -i 's/\r$//' ./profile_manager.py ./entrypoint.sh /etc/supervisor/conf.d/browser-gui.conf \
    && chmod +x ./entrypoint.sh

# 5900=VNC(可不开) 6080=noVNC(网页远程桌面) 9100=账号浏览器守护 API(agent 内部调用)
EXPOSE 5900 6080 9100

ENTRYPOINT ["./entrypoint.sh"]

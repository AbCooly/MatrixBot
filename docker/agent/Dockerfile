# agent：运营智能体（WebUI 多账号矩阵 + 设置 + 定时调度）
#   浏览器不装在本容器 —— 通过 BROWSER_MGR_URL 连接 browser-gui 容器里的
#   每账号独立 Chromium（noVNC 远程桌面可人工接管登录验证）。
#   v2：仅支持容器化 Browser-GUI 一种部署方式（已移除 local/cdp 模式）。
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

# 国内网络构建优化：apt/pip 走清华镜像（避免直连/阿里云被限速断流）
RUN set -eux; \
    rm -f /etc/apt/sources.list.d/debian.sources; \
    printf 'deb http://mirrors.tuna.tsinghua.edu.cn/debian/ bookworm main contrib non-free non-free-firmware\ndeb http://mirrors.tuna.tsinghua.edu.cn/debian/ bookworm-updates main contrib non-free non-free-firmware\ndeb http://mirrors.tuna.tsinghua.edu.cn/debian-security bookworm-security main contrib non-free non-free-firmware\n' > /etc/apt/sources.list; \
    apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl tzdata ffmpeg fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -i https://pypi.tuna.tsinghua.edu.cn/simple/ -r requirements.txt

COPY . .
RUN mkdir -p state logs

EXPOSE 8080

# WebUI 为默认启动；如需常驻定时调度，用 compose 里 scheduler 服务或改 command
CMD ["python3", "-m", "agent.webui", "--host", "0.0.0.0", "--port", "8080"]

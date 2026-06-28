# ============================================================
# Ombre Brain Docker Build
# Docker 构建文件
#
# Build:
#   docker build -t ombre-brain .
# 本地运行（最小必填项）:
#   docker run \
#     -e OMBRE_COMPRESS_API_KEY=your-llm-key \
#     -e OMBRE_EMBED_API_KEY=your-gemini-key \
#     -e OMBRE_DASHBOARD_PASSWORD=xxx \
#     -p 8000:8000 ombre-brain
# 推荐用 deploy/docker-compose.yml（开发）或 deploy/docker-compose.user.yml（用户）启动。
# ============================================================

FROM python:3.12-slim

WORKDIR /app

# Install cloudflared + curl (for downloading cloudflared)
# 安装 cloudflared（用于 Tunnel 一键管理功能）
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && ARCH=$(dpkg --print-architecture) \
    && curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH}" \
       -o /usr/local/bin/cloudflared \
    && chmod +x /usr/local/bin/cloudflared \
    && apt-get remove -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first (leverage Docker cache)
# 先装依赖（利用 Docker 缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files / 复制项目文件
COPY src/ ./src/
COPY frontend/ ./frontend/
COPY VERSION ./VERSION
COPY config.example.yaml ./config.default.yaml
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

# Persistent mount point: bucket data
# 持久化挂载点：记忆数据
VOLUME ["/app/buckets"]

# Default to streamable-http for container (remote access)
# 容器场景默认用 streamable-http
ENV OMBRE_TRANSPORT=streamable-http
ENV OMBRE_BUCKETS_DIR=/app/buckets
# config 默认落在持久卷 /app/buckets 里，而不是镜像可写层 /app/config.yaml。
# 关键：很多 PaaS（Zeabur / 部分 Render 配置等）用**只读根文件系统**，只有挂载的卷可写——
# 这时 entrypoint 往 /app/config.yaml 写默认配置会 "Read-only file system" 失败 → FATAL →
# 无限崩溃重启（本地 root + 可写 /app 复现不出，平台上才炸）。放到 /app/buckets 既避开只读根，
# 又让 Dashboard 改的 key 落在卷上、重启/重部署不丢。VPS（deploy/docker-compose.yml）显式覆盖回
# /app/config.yaml 保持原有文件挂载不变。
ENV OMBRE_CONFIG_PATH=/app/buckets/config.yaml
# Embedding 使用 API 后端（Gemini）
# 必须通过运行时 -e 或 docker-compose environment 传入 OMBRE_EMBED_API_KEY
ENV OMBRE_EMBED_BACKEND=api

EXPOSE 8000

ENTRYPOINT ["./entrypoint.sh"]

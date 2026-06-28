#!/bin/sh
# entrypoint.sh — 容器启动入口
#
# 职责：确保 config 文件是一个可用的**普通文件**再启动服务，否则宁可 FATAL 退出，
# 也不让应用带着坏配置进入无限崩溃重启。不做其他事（不改业务逻辑）。
#
# 问题背景（Windows/WSL2 fresh install 崩溃重启）：
#   旧 compose 用单文件 bind mount `./config.yaml:/app/config.yaml`。若宿主
#   ./config.yaml 不存在，Docker（尤其 Windows/WSL2）会把它当成目录创建并挂进来，
#   /app/config.yaml 于是是个**目录**而非文件，应用读它直接 IsADirectoryError 崩溃。
#   更糟的是：bind mount 的挂载点在容器内**删不掉**（rm 报 "Device or resource busy"）。
#   根治办法是不再单文件挂载 config，改用 $OMBRE_CONFIG_PATH 把配置放进已经是目录挂载
#   的数据卷里（见 docker-compose.user.yml）。本脚本是最后一道防线。
#
# 处理逻辑：
#   1. 配置路径取 $OMBRE_CONFIG_PATH，未设则退回 /app/config.yaml（老行为，兼容现有部署）。
#   2. 确保父目录存在。
#   3. 若该路径是目录（Docker 副作用）：rmdir / rm -rf 常规删除；删不掉就用
#      `find -mindepth 1 -delete` 清空内容兜底（即便目录本身是挂载点删不掉），再试 rmdir。
#   4. 删成功（路径已不存在）→ 从内置默认模板初始化一份。
#   5. 最终校验：路径必须是普通文件，否则打印清晰指引并 FATAL 退出（不带病启动）。

CONFIG="${OMBRE_CONFIG_PATH:-/app/config.yaml}"
DEFAULT=/app/config.default.yaml

mkdir -p "$(dirname "$CONFIG")" 2>/dev/null || true

# --- 3. 若是目录，尽全力把它清掉 ---
if [ -d "$CONFIG" ]; then
    echo "[entrypoint] '$CONFIG' is a directory (Docker created it because the host file was missing)."
    echo "[entrypoint] Trying to remove it and re-initialize from defaults..."
    rmdir "$CONFIG" 2>/dev/null || rm -rf "$CONFIG" 2>/dev/null || true
    if [ -d "$CONFIG" ]; then
        # 直接删除失败（多半是活动 bind mount，挂载点自身删不掉）。
        # 兜底：清空目录内容（mindepth 1 = 不碰目录自身），再试着删掉空目录。
        echo "[entrypoint] Direct removal failed; clearing its contents as a fallback..."
        find "$CONFIG" -mindepth 1 -delete 2>/dev/null || true
        rmdir "$CONFIG" 2>/dev/null || true
    fi
fi

# --- 4. 不存在则从默认模板初始化（上面删成功后会走到这；纯缺文件也走这）---
if [ ! -e "$CONFIG" ]; then
    echo "[entrypoint] Initializing config from defaults at '$CONFIG'..."
    cp "$DEFAULT" "$CONFIG"
fi

# --- 5. 最终校验：必须是普通文件，否则别启动去无限崩溃刷屏 ---
if [ ! -f "$CONFIG" ]; then
    echo "[entrypoint] FATAL: could not prepare a usable config file at '$CONFIG'."
    echo "[entrypoint] Two known causes:"
    echo "[entrypoint]   (a) compose single-file-mounts a missing config (Docker makes it a directory):"
    echo "[entrypoint]         volumes:  - ./config.yaml:/app/config.yaml   <-- remove this line"
    echo "[entrypoint]   (b) the path sits on a read-only / non-writable filesystem (many PaaS, e.g."
    echo "[entrypoint]         Zeabur, use a read-only rootfs — only the mounted volume is writable)."
    echo "[entrypoint] Fix: point config at the writable data volume:"
    echo "[entrypoint]     environment:  - OMBRE_CONFIG_PATH=/app/buckets/config.yaml"
    echo "[entrypoint]     volumes:      - ./buckets:/app/buckets   (PaaS: mount the volume at /app/buckets)"
    echo "[entrypoint] The image already defaults OMBRE_CONFIG_PATH to /app/buckets/config.yaml;"
    echo "[entrypoint] this FATAL means it was overridden to an unwritable location."
    exit 1
fi

echo "[entrypoint] config ready at '$CONFIG'."
exec python src/server.py

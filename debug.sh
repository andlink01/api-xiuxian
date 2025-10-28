#!/bin/bash
# 这是一个调试脚本，用于清理、重建和启动 game_assistant 开发环境容器。

# 确保在脚本出错时立即退出
set -e
set -o pipefail # 如果管道中任何命令失败，则使整个管道失败

# 定义 Compose 文件和服务名称
COMPOSE_FILES="-f docker-compose.yml -f docker-compose.dev.yml"
SERVICE_NAME="game_assistant"

echo "--- 正在停止并清理旧的开发容器和网络 ---"
# 使用 -f 指定文件，确保停止正确的服务
docker compose $COMPOSE_FILES down --remove-orphans

echo "--- 正在强制重新构建开发镜像 (docker compose build --no-cache) ---"
# 使用 -f 指定文件进行构建
docker compose $COMPOSE_FILES build --no-cache $SERVICE_NAME

echo "--- 正在启动新的开发容器 (docker compose up -d) ---"
# 使用 -f 指定文件启动
docker compose $COMPOSE_FILES up -d $SERVICE_NAME

echo "--- 正在进入实时日志 (无前缀) ---"
echo "--- 按 Ctrl+C 停止跟踪日志 (不会停止容器) ---"
# 使用 -f 指定文件查看日志，并添加 --no-log-prefix 选项
docker compose $COMPOSE_FILES logs -f --no-log-prefix $SERVICE_NAME


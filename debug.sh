#!/bin/bash
# 这是一个调试脚本，用于清理、重建和启动 game_assistant 容器。

# 确保在脚本出错时立即退出
set -e

# 1. 清理：停止并删除旧的容器、网络和本地构建的镜像
echo "--- 正在停止并清理旧的容器和镜像 (docker-compose down --rmi 'local') ---"
# 使用 --rmi 'local' 只删除 docker-compose build 构建的镜像
docker-compose down --rmi 'local' --remove-orphans # 添加 --remove-orphans 以清理可能残留的旧容器

# 2. 重建：强制重新构建镜像，不使用缓存
echo "--- 正在重新构建镜像 (docker-compose build --no-cache) ---"
docker-compose build --no-cache

# 3. 部署：在后台启动容器
echo "--- 正在启动新容器 (docker-compose up -d) ---"
docker-compose up -d

# 4. 进入日志：实时跟踪容器日志 (去除前缀)
echo "--- 正在进入实时日志 (docker-compose logs -f --no-log-prefix) ---" # 修改处
echo "--- 按 Ctrl+C 停止跟踪日志 (不会停止容器) ---"
docker-compose logs -f --no-log-prefix # 修改处


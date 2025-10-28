#!/bin/bash
# 入口脚本，用于在启动前检查并初始化 config.yaml

CONFIG_FILE="/app/config.yaml"
# 获取挂载点上的文件所有者和组（如果存在）
uid=$(stat -c %u /app 2>/dev/null || echo 1000) # 默认为 1000
gid=$(stat -c %g /app 2>/dev/null || echo 1000) # 默认为 1000

# 检查配置文件是否存在于容器内的挂载点
if [ ! -f "$CONFIG_FILE" ]; then
    echo "配置文件 $CONFIG_FILE 未找到，正在创建初始设置文件..." >&2
    # 在容器内创建文件，尝试设置所有者（可能失败，但无妨），然后设置权限
    touch "$CONFIG_FILE"
    chown $uid:$gid "$CONFIG_FILE" 2>/dev/null || true # 尝试修改所有者，忽略错误
    echo "setup_needed: true" > "$CONFIG_FILE"
    chmod 666 "$CONFIG_FILE" # 关键：确保容器可写，进而影响宿主机
    echo "初始配置文件已创建并设置权限。" >&2
else
    echo "配置文件 $CONFIG_FILE 已存在。" >&2
    # 如果文件存在但为空，也写入初始内容并确保权限
    if [ ! -s "$CONFIG_FILE" ]; then
        echo "配置文件 $CONFIG_FILE 为空，正在写入初始设置内容..." >&2
        echo "setup_needed: true" > "$CONFIG_FILE"
        chmod 666 "$CONFIG_FILE"
        echo "初始配置文件内容已写入并确保权限。" >&2
    else
        # 对于已存在的文件，尝试确保容器内有写权限（现在挂载是 rw，应该能成功）
        chmod u+w "$CONFIG_FILE" 2>/dev/null || true
        echo "尝试确保容器对现有 $CONFIG_FILE 具有写权限。" >&2
    fi
fi

echo "启动主应用程序..." >&2
# 执行 Dockerfile 中定义的 CMD 或传递给 docker run/compose run 的命令
exec "$@"

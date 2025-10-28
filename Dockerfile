# ---- 构建阶段 (Builder Stage) ----
# 使用 slim 镜像作为基础，包含 Python 环境
FROM python:3.11-slim AS builder

# 设置工作目录
WORKDIR /app

# 安装编译 Python 包所需的系统依赖
# --no-install-recommends 避免安装不必要的推荐包
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      build-essential \
      python3-dev \
    && rm -rf /var/lib/apt/lists/* # 清理 apt 缓存

# 复制依赖文件
COPY requirements.txt .

# 安装 Python 依赖
# 使用 wheel 方式先构建，然后安装，可以稍微提高效率并方便后续复制
# --no-cache-dir 避免 pip 缓存增大镜像体积
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt \
    && pip install --no-cache-dir --no-deps --no-index --find-links=/wheels /wheels/* \
    && rm -rf /wheels # 安装完成后清理 wheel 文件

# ---- 运行阶段 (Final Stage) ----
# 再次使用干净的 slim 镜像作为最终运行环境
FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# (可选) 如果运行时确实需要某些 C 库（比如 hiredis 可能需要 libc，但 slim 镜像通常自带），可以在这里安装
# RUN apt-get update && apt-get install -y --no-install-recommends <runtime-lib> && rm -rf /var/lib/apt/lists/*

# 从构建阶段复制已安装好的 Python 包到最终镜像的相应位置
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
# 复制可能由 pip 安装的可执行文件（虽然本项目可能没有）
COPY --from=builder /usr/local/bin /usr/local/bin

# 复制项目代码（.dockerignore 文件会确保不复制无关内容）
COPY . .

# 创建 /app/logs 和 /app/data 目录，作为挂载点或容器内存储
# （即使挂载了卷，创建目录也是好的实践）
RUN mkdir -p /app/logs /app/data

# 容器启动时运行的默认命令
CMD ["python", "main.py"]

# 修仙助手 (xiuxian-bot)

一个基于 Python 和 Kurigram (Pyrogram) 的异步 Telegram 机器人，旨在自动化修仙主题游戏的部分操作。它利用插件化架构、事件驱动模型、Redis 缓存/状态管理和 Docker 进行部署。

## 功能特性
* 模块化插件系统，易于扩展
* 事件驱动架构，低耦合
* 异步处理，高性能
* 使用 Redis 进行数据缓存和状态管理
* 通过 Docker Compose 实现便捷部署（区分开发与生产）
* 支持交互式首次配置 (自动处理文件创建和权限)
* 自动化任务调度 (基于 APScheduler)
* 部分自动化插件（闭关、药园、引道、点卯、传功、闯塔、学习配方、元婴出窍、魔君降临等）
* 数据查询与手动同步功能
* 智能炼制（自动检查配方、材料，支持材料收集）
* 多账号资源转移（基于 Redis Pub/Sub）
* 游戏事件通知
* 配方、问答库管理
* ... 以及更多插件功能

## 先决条件
* **Git:** 用于克隆代码库。
* **Docker:** 用于构建和运行容器。
* **Docker Compose:** 用于编排容器服务。
* **Telegram API 凭证:** `api_id` 和 `api_hash` (从 [my.telegram.org](https://my.telegram.org) 获取)。
* **Redis 服务器:** 需要一个可访问的 Redis 实例。
* **(可选) Google Gemini API 密钥:** 如需 AI 功能。
* **(可选) Docker Hub 账号:** 如需推送或拉取镜像。

---

## 开发环境部署与调试

此方法适用于本地开发，支持代码热更新。

**步骤:**

1.  **克隆代码库:**
    ```bash
    git clone <您的仓库 URL>
    cd xiuxian-bot
    ```
2.  **准备环境文件:** 确保项目包含 `Dockerfile`, `docker-compose.yml`, `docker-compose.dev.yml`, `entrypoint.sh`, `requirements.txt` 及源代码。
3.  **赋予脚本权限:** `chmod +x entrypoint.sh debug.sh`
4.  **启动开发环境:**
    ```bash
    ./debug.sh
    ```
5.  **首次运行设置:**
    * 如果 `./config.yaml` 文件不存在或内容为 `setup_needed: true`，容器启动后会自动进入**交互式设置**流程。
    * 按照终端提示完成设置。设置脚本会直接将配置写入 `./config.yaml` 文件。
    * 设置完成后，容器可能会自动重启或需要您手动重启 (`Ctrl+C` 停止日志跟踪，然后 `docker compose -f docker-compose.yml -f docker-compose.dev.yml restart game_assistant`) 以加载新配置。
6.  **开发:** 容器运行后，本地代码修改会实时同步到容器内。根据 Python 应用特性，可能需要重启容器使修改生效。
7.  **停止:** `docker compose -f docker-compose.yml -f docker-compose.dev.yml down`

---

## 生产环境部署 (新机器)

此方法使用预构建的 Docker Hub 镜像进行部署。

**步骤:**

1.  **准备宿主机环境:**
    * 安装 Docker 和 Docker Compose。
    * 创建部署目录 (例如 `~/xiuxian_qiu`) 并进入。
    * 创建 `logs` 和 `data` 子目录: `mkdir logs data`。
2.  **创建 `docker-compose.yml` 文件:** (内容见下方或项目内文件)
    * 确保 `image:` 指向您正确的 Docker Hub 镜像。
    * 确保 `config.yaml` 挂载为**读写 (`rw`)** 或不指定模式（默认为 `rw`）。
    ```yaml
    # version: '3.8'
    services:
      game_assistant:
        image: lostme01/api-xiuxian:latest # 您的镜像
        container_name: game_assistant
        restart: unless-stopped
        volumes:
          - ./config.yaml:/app/config.yaml # 默认为 rw
          - ./logs:/app/logs
          - ./data:/app/data
          - /etc/localtime:/etc/localtime:ro
        tty: true
        stdin_open: true
        networks:
          - default
    networks:
      default:
    ```
3.  **首次启动与设置:**
    * 直接运行：
        ```bash
        docker compose up -d
        ```
    * 容器启动时，`entrypoint.sh` 会检查 `./config.yaml`。由于是新机器，文件不存在，脚本会自动创建包含 `setup_needed: true` 的初始文件并设置好权限。
    * `main.py` 检测到需要设置，会**暂停正常启动**并等待用户进入交互设置。
    * **进入交互设置:** 使用 `docker attach` 命令连接到正在运行的容器的标准输入/输出：
        ```bash
        docker attach game_assistant
        ```
        *(如果 `docker attach` 后没有反应，尝试按一下回车键)*
    * 您现在应该能看到设置脚本的提示信息了。按照提示完成所有配置。
    * 设置脚本会将最终配置**直接写入** `./config.yaml` 文件。
    * 设置完成后，脚本通常会退出。您可能需要按 `Ctrl+P` 然后 `Ctrl+Q` 来**分离 (detach)** `attach` 会话而不停止容器（如果设置脚本没有自动退出容器）。
4.  **重启服务以加载最终配置:**
    * 为了确保应用以最终配置运行，建议重启容器：
        ```bash
        docker compose restart game_assistant
        ```
    * 现在应用会读取完整的 `config.yaml` 并正常运行。

5.  **后续操作:**
    * **查看日志:** `docker compose logs -f`
    * **停止服务:** `docker compose down`
    * **更新镜像并重启:**
        ```bash
        docker compose pull game_assistant
        docker compose up -d
        ```

---

## 配置 (`config.yaml`)

* 应用的配置中心。
* **首次部署**时，通过 `docker compose up -d` 启动，然后 `docker attach game_assistant` 进入交互式设置自动生成。
* 文件以**读写**方式挂载在容器中。可以通过应用内的 `,配置` 指令修改部分运行时配置，修改会直接写入文件。
* **请勿将包含敏感信息（API密钥、密码、Cookie）的 `config.yaml` 文件提交到 Git 仓库！** (`.gitignore` 文件已包含忽略规则)。

---
## 主要指令 (管理员)
*(保持不变)*

---
## 故障排除
*(保持不变，但关于首次设置权限错误的部分可以移除或淡化)*


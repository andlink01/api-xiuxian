# 修仙助手 (xiuxian-bot)

一个基于 Python 和 Kurigram (Pyrogram) 的异步 Telegram 机器人，旨在自动化修仙主题游戏的部分操作。它利用插件化架构、事件驱动模型、Redis 缓存/状态管理和 Docker 进行部署。

## 功能特性

* 模块化插件系统，易于扩展
* 事件驱动架构，低耦合
* 异步处理，高性能
* 使用 Redis 进行数据缓存和状态管理
* 通过 Docker Compose 实现便捷部署（区分开发与生产）
* 支持交互式首次配置 (自动处理文件创建和权限)
* 自动化任务调度 (基于 APScheduler, 支持持久化)
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
* **Docker Compose:** 用于编排容器服务 (通常随 Docker 安装)。
* **Telegram API 凭证:** 您需要从 [my.telegram.org](https://my.telegram.org) 获取 `api_id` 和 `api_hash`。
* **Redis 服务器:** 需要一个可访问的 Redis 实例用于数据存储和状态管理。
* **(可选) Google Gemini API 密钥:** 如果需要使用 AI 功能。
* **(可选) Docker Hub 账号:** 如果您需要推送或拉取镜像。

---

## 开发环境部署与调试

此方法适用于在本地机器上进行代码开发和调试，支持代码热更新。

**步骤:**

1.  **克隆代码库:**
    ```bash
    git clone <您的仓库 URL>
    cd xiuxian-bot
    ```

2.  **准备环境文件:**
    * 确保项目根目录下包含 Dockerfile, docker-compose.yml, docker-compose.dev.yml, entrypoint.sh, requirements.txt 及源代码等。

3.  **赋予脚本权限:**
    ```bash
    chmod +x entrypoint.sh debug.sh push_updates.sh
    ```

4.  **启动开发环境:**
    * 运行调试脚本：
        ```bash
        ./debug.sh
        ```
    * 该脚本会执行以下操作：
        * 停止并清理旧的开发容器。
        * 强制重新构建 Docker 镜像 (使用 `docker-compose.dev.yml` 的 `build: .` 设置)。
        * 在后台启动新的开发容器。
        * 实时跟踪容器日志（无服务前缀）。

5.  **首次运行设置:**
    * 如果 `./config.yaml` 文件不存在或内容仅为 `setup_needed: true`，容器启动时会自动进入**交互式设置**流程。
    * 入口脚本 `entrypoint.sh` 会自动创建初始的 `./config.yaml` 文件并设置权限。
    * 按照终端日志下方的提示完成设置 (`core/setup.py`)。设置脚本会直接将配置写入 `./config.yaml` 文件，因为开发环境挂载是读写的 (`rw`)。
    * 设置完成后，容器可能会自动基于新配置继续运行，或者您可能需要手动重启容器以加载完整配置：先按 `Ctrl+C` 停止日志跟踪，然后运行 `docker compose -f docker-compose.yml -f docker-compose.dev.yml restart game_assistant`。

6.  **开发与调试:**
    * 容器运行后，您在宿主机上对项目代码（`.py` 文件等）的修改会**实时同步**到容器内（因为 `docker-compose.dev.yml` 挂载了 `./:/app`）。通常需要重启容器 (`docker compose ... restart game_assistant`) 才能让 Python 应用加载修改后的代码。
    * 使用 `Ctrl+C` 停止跟踪日志（容器仍在后台运行）。
    * 如需完全停止开发环境，请在项目根目录运行：
        ```bash
        docker compose -f docker-compose.yml -f docker-compose.dev.yml down
        ```

---

## 生产环境部署 (新机器)

此方法使用预构建的 Docker Hub 镜像进行部署，并简化了首次配置流程。

**步骤:**

1.  **准备宿主机环境:**
    * 确保已安装 Docker 和 Docker Compose。
    * 创建部署目录，例如：
        ```bash
        mkdir -p ~/xiuxian_qiu
        cd ~/xiuxian_qiu
        ```
    * **重要:** 在部署目录中创建 `logs` 和 `data` 子目录：
        ```bash
        mkdir logs data
        ```

2.  **创建 `docker-compose.yml` 文件:**
    * 在部署目录 (`~/xiuxian_qiu`) 下创建 `docker-compose.yml` 文件，内容如下 (确保 `image:` 指向您正确的 Docker Hub 镜像，并将 `config.yaml` 挂载为**读写 `rw`** 或不指定模式)：
        ```yaml
        # version: '3.8' # 版本号在新版 Docker Compose 中不是必需的

        services:
          game_assistant:
            # 拉取您在 Docker Hub 上的镜像
            image: lostme01/api-xiuxian:latest # 您的镜像名称和标签
            container_name: game_assistant
            restart: unless-stopped

            volumes:
              # 配置文件挂载为读写 (rw)，允许入口脚本创建和设置脚本写入
              - ./config.yaml:/app/config.yaml # 默认为 rw
              # 日志目录挂载
              - ./logs:/app/logs
              # 数据目录挂载 (包含数据库和 session)
              - ./data:/app/data
              # 宿主机时区文件挂载
              - /etc/localtime:/etc/localtime:ro

            # 使容器保持运行并允许交互式命令 (如 run, attach)
            tty: true
            stdin_open: true

            networks:
              - default

        networks:
          default:
        ```

3.  **首次启动与设置:**
    * **启动服务:** 直接在后台启动服务：
        ```bash
        docker compose up -d
        ```
    * **检查是否需要设置:** 查看容器日志，确认是否进入设置模式。
        ```bash
        docker compose logs game_assistant
        ```
        如果日志提示进入设置模式 (类似 "WARNING:GameAssistant.Config:处于设置模式...")，则进行下一步。如果直接正常启动，则说明可能之前有残留的 `config.yaml` 或镜像内已包含配置（不推荐）。
    * **进入交互设置:** 使用 `docker attach` 命令连接到正在运行的容器的标准输入/输出：
        ```bash
        docker attach game_assistant
        ```
        *(如果 `docker attach` 后没有反应，尝试按一下回车键)*
    * **完成设置:** 您现在应该能看到设置脚本的提示信息了。按照提示完成所有配置。设置脚本会将最终配置**直接写入**宿主机的 `./config.yaml` 文件。
    * **分离会话:** 设置完成后，脚本通常会退出，`attach` 会话也会随之结束。如果脚本没有退出，您可能需要按 `Ctrl+P` 然后 `Ctrl+Q` 来**分离 (detach)** `attach` 会话而不停止容器。
    * **重启服务加载最终配置:** 为了确保应用以最终配置运行，**强烈建议**在设置完成后重启容器：
        ```bash
        docker compose restart game_assistant
        ```

4.  **后续操作:**
    * **查看日志:** `docker compose logs -f`
    * **停止服务:** `docker compose down`
    * **更新镜像并重启:** (确保先停止旧容器)
        ```bash
        docker compose pull game_assistant # 拉取最新镜像
        docker compose up -d              # 使用新镜像重新创建并启动容器
        ```

---

## 配置 (`config.yaml`)

* 应用的配置中心，包含 API 密钥、ID、功能开关、时间间隔等。
* **首次部署**时，通过 `docker compose up -d` 启动，然后 `docker attach game_assistant` 进入交互式设置自动生成。
* 文件以**读写 (`rw`)** 方式挂载在容器中。可以通过应用内的 `,配置` 指令修改部分运行时配置，修改会直接写入文件。
* **请勿将包含敏感信息（API密钥、密码、Cookie）的 `config.yaml` 文件提交到 Git 仓库！** (`.gitignore` 文件已包含忽略规则)。

---

## 主要指令 (管理员)

通过与机器人在私聊或指定的**控制群**中交互来使用（在控制群中可能需要 `@机器人用户名`）：

* `,菜单`: 显示可用指令列表。
* `,帮助 <指令名>`: 查看具体指令的说明。
* `,查询角色`/`背包`/`商店`: 查看缓存数据。
* `,同步角色`/`背包`/`商店`/`物品`: 手动强制更新缓存。
* `,发送 <游戏指令>`: 让机器人发送指定的游戏指令。
* `,收货 ...`: 触发多账号资源转移流程（接收方使用）。
* `,智能炼制 <物品名>[*数量]`: 自动执行炼制任务。
* `,任务列表`: 查看当前计划的任务。
* `,插件`: 查看插件加载状态。
* `,配置 [<配置项>] [<新值>]`: 查看或修改配置。
* `,日志 [main|chat] [行数]`: 查看日志。
* `,清除状态 <类型>`: 手动清理 Redis 中的某些状态锁或标记。
* ... 更多指令请参考 `,菜单` 和 `,帮助`。

---

## 故障排除

* **首次启动未进入设置:** 检查宿主机部署目录下是否意外存在 `config.yaml` 文件且内容不是 `setup_needed: true`。删除或清空该文件后重试 `docker compose up -d`。
* **`docker attach` 后无反应:** 尝试按一下回车键。如果仍然没有设置提示，检查 `docker compose logs game_assistant` 确认应用是否已正常启动（可能 `config.yaml` 已存在且有效）或在启动过程中报错。
* **设置时权限错误:** 理论上新方案已解决此问题。如果仍出现，请检查 `entrypoint.sh` 是否正确包含在镜像中且有执行权限，以及宿主机部署目录本身是否具有基本的读写权限。
* **Redis 连接失败:** 检查 `config.yaml` 中的 Redis 配置，确认 Redis 服务可用且网络通畅。
* **Telegram 登录失败:** 检查 `config.yaml` 中的 `api_id`/`api_hash`。如果 `data/my_game_assistant.session` 文件损坏，可尝试删除后重新运行 `docker attach` 进入设置重新登录。
* **插件加载失败/功能不工作:** 查看应用启动日志 (`docker compose logs game_assistant`) 检查插件加载错误。确认相关插件在 `config.yaml` 中已启用。


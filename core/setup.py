import asyncio
import sys
import yaml
import getpass
from pyrogram import Client
from pyrogram.enums import ChatType
from pyrogram.errors import SessionPasswordNeeded, PhoneCodeInvalid, PasswordHashInvalid

async def input_safe(prompt: str) -> str:
    """安全地获取用户输入，处理 EOFError (在 docker attach 中常见)"""
    while True:
        try:
            val = input(prompt).strip()
            return val
        except EOFError:
            print("输入流已断开 (EOF)。请重新 attach 并按 Enter 键继续...")
            await asyncio.sleep(2) # 等待用户重新 attach
        except KeyboardInterrupt:
            print("\n检测到中断。正在退出设置。")
            sys.exit(1)

async def input_secure(prompt: str) -> str:
    """安全地获取密码输入"""
    while True:
        try:
            val = getpass.getpass(prompt).strip()
            return val
        except EOFError:
            print("输入流已断开 (EOF)。请重新 attach 并按 Enter 键继续...")
            await asyncio.sleep(2)
        except KeyboardInterrupt:
            print("\n检测到中断。正在退出设置。")
            sys.exit(1)

async def select_group_dialog(client: Client, purpose: str) -> int:
    """
    (修正) 专门用于选择群组/超级群组 (包含 FORUM 类型)
    """
    print(f"\n--- 正在为您拉取 *所有* 群组列表 ({purpose}) ---")
    print("这可能需要一点时间... (将拉取所有对话)")
    
    dialogs = []
    try:
        async for dialog in client.get_dialogs(): 
            
            # --- (修正点: 包含 ChatType.FORUM) ---
            if dialog.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP, ChatType.FORUM]:
                dialogs.append(dialog)
                # 打印一个动态提示，让您知道它在工作
                print(f"  > 正在查找... 已找到 {len(dialogs)} 个群组: {dialog.chat.title[:30]}...", end="\r")
            # --- (修正结束) ---

        print("\n拉取完成！正在显示所有找到的群组。") # 清理动态提示
        
    except Exception as e:
        print(f"\n拉取对话列表失败: {e}")
        return 0

    print(f"\n--- 请为 '{purpose}' 选择一个目标 ---")
    
    if not dialogs:
        print("未找到任何符合条件 (GROUP, SUPERGROUP 或 FORUM) 的群组。")
        return 0

    indexed_groups = []
    dialogs.sort(key=lambda d: d.chat.title.lower() if d.chat.title else "") 
    
    for i, dialog in enumerate(dialogs):
        title = dialog.chat.title
        chat_id = dialog.chat.id
        # (新) 标记话题模式群组 (Forum)
        is_forum_marker = "[话题]" if dialog.chat.is_forum else "" 
        entry = f"  [{i+1}] {title} {is_forum_marker} (ID: {chat_id})"
        print(entry)
        indexed_groups.append(chat_id)

    while True:
        try:
            choice_str = await input_safe(f"\n请输入序号 (1-{len(indexed_groups)}) 或 手动输入ID: ")
            
            if choice_str.isdigit() and 1 <= int(choice_str) <= len(indexed_groups):
                selected_id = indexed_groups[int(choice_str) - 1]
                print(f"已选择: {selected_id}")
                return selected_id
            
            elif choice_str.lstrip('-').isdigit():
                print(f"已手动输入: {int(choice_str)}")
                return int(choice_str)
            else:
                print("无效输入。")
        except ValueError:
            print("无效输入。")

async def select_game_bots_from_chat(client: Client, chat_id: int) -> list[int]:
    """
    拉取指定群组的最后 50 条消息，筛选出频道发言者，并让用户多选。
    """
    print(f"\n--- 正在拉取游戏群 (ID: {chat_id}) 的最后 50 条消息以筛选 Game Bot... ---")
    channels = {} # 使用字典去重: {channel_id: title}
    
    try:
        async for message in client.get_chat_history(chat_id, limit=50):
            if message.sender_chat:
                channels[message.sender_chat.id] = message.sender_chat.title
    except Exception as e:
        print(f"\n拉取群组历史消息失败: {e}")
        print("这可能是因为您的账户不在该群组，或者群组禁止查看历史记录。")
        print("将跳过此步骤。")
        return []

    if not channels:
        print("在最近 50 条消息中未检测到任何以频道身份的发言。")
        return []

    print("\n--- (多选) 请选择您希望监控的 Game Bot 频道 ---")
    
    channel_list = list(channels.items()) # [(id, title), ...]
    
    for i, (channel_id, title) in enumerate(channel_list):
        print(f"  [{i+1}] {title} (ID: {channel_id})")

    print("\n请输入序号，用逗号 ',' 分隔 (例如: 1,3,4)。")
    print("如果全选，请输入 'all'。")
    print("如果全不选，请直接按 Enter 键。")

    selected_ids = []
    input_str = await input_safe("您的选择: ")

    if input_str.lower().strip() == 'all':
        selected_ids = [cid for cid, title in channel_list]
    elif input_str.strip():
        try:
            indices = [int(x.strip()) - 1 for x in input_str.split(',') if x.strip()]
            for idx in indices:
                if 0 <= idx < len(channel_list):
                    selected_ids.append(channel_list[idx][0])
                else:
                    print(f"警告: 序号 {idx+1} 无效，已忽略。")
        except ValueError:
            print("输入格式错误，跳过频道选择。")
    
    if selected_ids:
        print(f"已选择 {len(selected_ids)} 个频道作为 Game Bot 进行监控。")
    else:
        print("未选择任何 Game Bot 频道。")
        
    return selected_ids


async def run_setup(config_path: str):
    """
    运行交互式设置的主函数
    """
    print("--- 欢迎使用 xiuxian-bot 交互式设置 ---")
    
    config_data = {}
    
    print("您已连接到设置向导。")
    await input_safe("\n>>> 请按 Enter 键开始设置... <<<")

    # 1. Telegram API
    print("\n--- 1. Telegram API 设置 ---")
    print("请访问 https://my.telegram.org 获取您的 API 信息。")
    
    api_id_str = ""
    while not api_id_str.isdigit():
        api_id_str = await input_safe("请输入 api_id: ")
        if not api_id_str.isdigit():
            print("输入无效。api_id 必须是纯数字，请重新输入。")
            
    config_data['telegram'] = {
        'api_id': int(api_id_str),
        'api_hash': await input_safe("请输入 api_hash: ")
    }

    # 2. 登录 Telegram
    print("\n--- 2. 登录 Telegram ---")
    print("Pyrogram (Kurigram) 将尝试登录。请准备接收验证码。")
    client = Client(
        "data/my_game_assistant",
        api_id=config_data['telegram']['api_id'],
        api_hash=config_data['telegram']['api_hash']
    )
    
    try:
        await client.start()
        me = await client.get_me()
        print(f"\n登录成功！欢迎, {me.first_name} (ID: {me.id})")
        
        print(f"已自动将您的账户 (ID: {me.id}) 设为管理员。")
        config_data['telegram']['admin_id'] = me.id
        
    except (PhoneCodeInvalid, PasswordHashInvalid) as e:
        print(f"\n登录失败: {e}")
        print("请检查您的输入。退出设置。")
        await client.stop()
        sys.exit(1)
    except SessionPasswordNeeded:
        print("检测到两步验证 (2FA)。")
        try:
            await client.check_password(await input_secure("请输入您的两步验证密码: "))
            me = await client.get_me()
            print(f"\n登录成功！欢迎, {me.first_name} (ID: {me.id})")
            print(f"已自动将您的账户 (ID: {me.id}) 设为管理员。")
            config_data['telegram']['admin_id'] = me.id
        except Exception as e:
            print(f"\n2FA 密码错误或登录失败: {e}")
            await client.stop()
            sys.exit(1)
    except Exception as e:
        print(f"\n发生未知登录错误: {e}")
        await client.stop()
        sys.exit(1)

    # 3. 选择群组和 Game Bots
    print("\n--- 3. 选择群组和 Game Bots ---")
    
    target_chat_id = await select_group_dialog(client, "游戏群 (Game Group)")
    config_data['telegram']['target_chat_id'] = target_chat_id
    
    control_chat_id = await select_group_dialog(client, "控制群 (Control Group)")
    config_data['telegram']['control_chat_id'] = control_chat_id
    
    game_bot_ids = []
    if target_chat_id:
        game_bot_ids = await select_game_bots_from_chat(client, target_chat_id)
    else:
        print("未选择游戏群，跳过 Game Bot 频道筛选。")
        
    config_data['telegram']['game_bot_ids'] = game_bot_ids 
    
    await client.stop()
    print("Telegram 客户端会话已保存并断开。")

    # 4. Redis
    print("\n--- 4. Redis 数据库设置 ---")
    redis_port_str = ""
    while not redis_port_str.isdigit():
        redis_port_str = await input_safe("请输入 Redis 端口 (默认 6379): ") or "6379"
        if not redis_port_str.isdigit():
            print("输入无效。端口必须是纯数字，请重新输入。")

    redis_db_str = ""
    while not redis_db_str.isdigit():
        redis_db_str = await input_safe("请输入 Redis 数据库 (默认 0): ") or "0"
        if not redis_db_str.isdigit():
            print("输入无效。数据库必须是纯数字，请重新输入。")
            
    config_data['redis'] = {
        'host': await input_safe("请输入 Redis 主机 (e.g., your.redis-server.com): "),
        'port': int(redis_port_str),
        'db': int(redis_db_str),
        'password': await input_secure("请输入 Redis 密码 (如果没有请留空): ")
    }

    # 5. API Services (Cookie)
    print("\n--- 5. API 服务设置 (可选) ---")
    config_data['api_services'] = {
        'shared_cookie': await input_safe("请输入共享 Cookie (如果不需要请留空): ")
    }

    # 6. Gemini
    print("\n--- 6. Gemini API 密钥 (可选) ---")
    keys_str = await input_safe("请输入您的 Gemini API 密钥 (多个请用逗号 ',' 分隔): ")
    config_data['gemini'] = {
        'api_keys': [key.strip() for key in keys_str.split(',') if key.strip()]
    }

    # 7. Database
    config_data['database'] = {
        'sqlite_url': "sqlite:///session_data/local_data.db"
    }

    # 8. 写入配置
    print("\n--- 正在保存配置 ---")
    try:
        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.dump(config_data, f, indent=2, allow_unicode=True)
        print(f"配置已成功写入 {config_path}")
        print("\n设置完成！")
        print("正在退出... 容器将自动重启并应用新配置。")
        sys.exit(0)
    except Exception as e:
        print(f"写入配置文件失败: {e}")
        print("请检查文件权限。")
        sys.exit(1)

if __name__ == "__main__":
    # 此脚本不应被直接运行
    print("这是一个设置模块，请通过 main.py 启动。")

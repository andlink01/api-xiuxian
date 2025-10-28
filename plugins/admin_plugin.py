import logging
import re
import json
from datetime import datetime
import pytz
import yaml
from plugins.base_plugin import BasePlugin, AppContext
from pyrogram.types import Message, ReplyParameters, LinkPreviewOptions
from pyrogram.enums import ChatType
import asyncio
import os
from typing import Optional # 引入 Optional
# 移除 string 导入
# import string

# 导入功能
try: from plugins.character_sync_plugin import trigger_character_sync
except ImportError: trigger_character_sync = None
try: from plugins.item_sync_plugin import trigger_item_sync_update
except ImportError: trigger_item_sync_update = None
try: from plugins.shop_sync_plugin import trigger_shop_sync_update; SHOP_SYNC_ENABLED = True
except ImportError: trigger_shop_sync_update = None; SHOP_SYNC_ENABLED = False

# --- 导入包含 user_id 格式的 Key 常量 ---
from plugins.constants import REDIS_CHAR_KEY_PREFIX, REDIS_INV_KEY_PREFIX, REDIS_ITEM_MASTER_KEY
try: from plugins.cultivation_plugin import REDIS_WAITING_KEY_PREFIX # 已经是格式化字符串
except ImportError: REDIS_WAITING_KEY_PREFIX = "cultivation_waiting_msg_id:{}" # 提供后备
try: from plugins.herb_garden_plugin import HERB_GARDEN_ACTION_LOCK_KEY_FORMAT # 导入药园锁格式
except ImportError: HERB_GARDEN_ACTION_LOCK_KEY_FORMAT = "herb_garden:action_lock:{}" # 提供后备
try: from plugins.marketplace_transfer_plugin import REDIS_ORDER_EXEC_LOCK_PREFIX # 交易锁前缀
except ImportError: REDIS_ORDER_EXEC_LOCK_PREFIX = "marketplace_order_exec:lock:"
try: from plugins.sect_teach_plugin import REDIS_PENDING_PLACEHOLDER_KEY_PREFIX, REDIS_TEACH_LOCK_KEY_FORMAT # 导入传功锁格式和占位符前缀
except ImportError: REDIS_PENDING_PLACEHOLDER_KEY_PREFIX = "sect_teach:pending_placeholder"; REDIS_TEACH_LOCK_KEY_FORMAT = "sect_teach:action_lock:{}" # 提供后备
# --- 导入结束 ---

# --- COMMAND_MENU_TEXT 格式 ---
COMMAND_MENU_TEXT = """
🎮 **修仙助手 - 指令菜单**

🔍 **查询功能**
  👤`,查询角色` 🎒`,查询背包` 🏦`,查询商店`
  📜`,已学配方` 🧪`,查询配方` 📊`,缓存状态`

🔄 **同步功能**
  👤`,同步角色` 🎒`,同步背包` 💎`,同步物品`
  🏦`,同步商店`

👉 **手动操作**
  🛠️`,智能炼制`
  ➡️`,发送` 📥`,收货`

💾 **数据管理**
  📚`,查询题库` ➕`,添加题库` 🗑️`,删除题库`
  📝`,更新配方`

⚙️ **系统管理**
  📅`,任务列表` 📈`,日志级别` 🧹`,清除状态`
  🧩`,插件` 🔧`,配置` 📄`,日志`

ℹ️ **帮助**
  🧭`,菜单` ❓`,帮助`
"""
# --- 格式结束 ---

HELP_DETAILS = {
    # ... (帮助信息保持不变) ...
    "菜单": "显示指令菜单。",
    "查询角色": "查询您当前角色的基本信息 (仅读取缓存)。",
    "查询背包": "查询您当前储物袋的内容 (仅读取缓存)。",
    "查询商店": "查询当前缓存的宗门宝库物品列表 (仅读取缓存)。",
    "已学配方": "查询当前助手已学习的所有配方名称。",
    "查询配方": "查询指定物品的炼制配方所需材料。\n用法: `,查询配方 <物品名>`",
    "缓存状态": "显示主要数据缓存的上次更新时间和剩余有效期。",
    "同步角色": "手动强制从 API 同步一次角色和背包信息到缓存。",
    "同步背包": "手动强制从 API 同步一次角色和背包信息到缓存。",
    "同步商店": f"手动强制从 API 同步一次商店物品信息到缓存。({'已启用' if SHOP_SYNC_ENABLED else '未启用'})",
    "同步物品": "手动强制从 API 同步一次物品主数据到缓存 (忽略每日限制)。",
    "发送": "让助手向游戏群发送指定的游戏指令。\n用法: `,发送 <游戏指令>`",
    "收货": "【接收方用】让机器人发布求购单，触发多账号资源转移流程。\n用法1: `,收货 <物品> <数量>`\n用法2: `,收货 <需求物品> <需求数量> <支付物品> <支付数量>`",
    "智能炼制": "自动检查配方学习状态和材料并执行炼制，材料不足时尝试收集。\n用法: `,智能炼制 <物品名>[*数量]` 或 `,智能炼制 <物品名> [数量]`",
    "更新配方": "【限收藏夹】将消息内容作为配方文本更新到 Redis。\n用法: `,更新配方 [--overwrite]` (消息体包含配方)",
    "查询题库": "搜索或列出玄骨/天机题库。\n用法: `,查询题库 [玄骨|天机] [关键词]` (不带关键词则列出全部)",
    "添加题库": "添加或更新玄骨/天机问答对。\n用法: `,添加题库 [玄骨|天机] 问题文本::答案文本`",
    "删除题库": "根据 `,查询题库` 返回的编号删除问答对。\n用法: `,删除题库 <编号>`",
    "任务列表": "查询当前正在运行或计划中的定时任务列表。",
    "插件": "查看插件列表。\n用法: `,插件`",
    "配置": "查看或设置功能模块。\n用法: `,配置` 或 `,配置 <配置项> <新值>`",
    "日志": "查看最近的日志信息。\n用法: `,日志 [类型] [行数]`",
    "日志级别": "查看或设置日志级别。\n用法: `,日志级别` 或 `,日志级别 <级别>`",
    "清除状态": "手动清除 Redis 锁或标记。\n用法: `,清除状态 <类型>` (可选类型: 药园锁, 闭关等待, 传功锁, 传功占位符, 交易订单锁)",
    "帮助": "查看指令的详细说明和用法。\n用法: `,帮助 <指令名>`",
}

DIRECT_REPLY_COMMANDS = {
    "菜单", "帮助",
    "查询角色", "查询背包", "查询商店",
    "已学配方", "缓存状态", "任务列表",
    "插件", "配置", "日志级别", "清除状态",
}

class Plugin(BasePlugin):
    """处理管理员指令的入口和分发器插件。"""
    def __init__(self, context: AppContext, plugin_name: str, cn_name: str | None = None):
        super().__init__(context, plugin_name, cn_name)
        self.control_chat_id = self.config.get("telegram.control_chat_id", 0)
        self.admin_id = self.config.get("telegram.admin_id")
        self.telegram_client_instance = getattr(context, 'telegram_client', None)
        if not self.telegram_client_instance:
             self.error("初始化时无法获取 TelegramClient 实例！编辑/回复/发送功能可能受限。")
        self.info("管理插件 (入口) 已加载。")

    def register(self):
        self.event_bus.on("admin_command_received", self.handle_admin_command)
        self.info("已注册 admin_command_received 事件监听器。")

    async def handle_admin_command(self, message: Message, my_username: str | None):
        raw_text = message.text or message.caption
        if not raw_text: return
        if not self.admin_id: self.warning("管理员 ID 未配置，无法处理指令。"); return
        if not message.from_user or message.from_user.id != self.admin_id: return

        command_text = raw_text.strip(); command: Optional[str] = None; args: Optional[str] = None
        is_private = message.chat.type == ChatType.PRIVATE
        is_saved_message = is_private and message.chat.id == self.admin_id
        is_control_group = message.chat.id == self.control_chat_id
        should_process = False

        # --- 修改: 处理控制群提及 ---
        if is_control_group:
            mention = f"@{my_username}" if my_username else None
            if mention and mention in command_text:
                # 只有当提及的是当前机器人时才处理
                should_process = True
                # 从文本中移除提及，无论在开头还是中间
                command_text = command_text.replace(mention, "").strip()
            # 如果没有提及，则不处理 (除非后续有特殊指令判断)
        elif is_private:
            should_process = True
        # --- 修改结束 ---

        if not should_process: return

        # --- 修改: 恢复严格检查逗号前缀 ---
        detected_prefix = None
        if command_text and command_text.startswith(','): # 只检查逗号
            detected_prefix = ','
            command_parts = command_text[1:].split(maxsplit=1)
            if not command_parts: return # 只有前缀，没有命令
            command = command_parts[0].lower() # 命令转小写
            args = command_parts[1].strip() if len(command_parts) > 1 else None
        # --- 修改结束 ---
        else: return # 如果没有逗号前缀，则忽略 (提及已被移除)

        if command is None: return
        self.info(f"处理管理员指令 (前缀: '{detected_prefix}'): '{command}' (来自收藏夹: {is_saved_message}) (参数: {args})")

        edit_target_id = None
        # ... (后续处理逻辑保持不变) ...
        fast_view_commands_no_args = ["帮助", "配置", "日志级别", "插件", "清除状态"]
        always_direct_reply_commands = ["菜单", "查询角色", "查询背包", "查询商店", "已学配方", "缓存状态", "任务列表"]
        should_send_processing = True
        if command in always_direct_reply_commands: should_send_processing = False
        elif command in fast_view_commands_no_args and args is None: should_send_processing = False
        elif command == "发送": should_send_processing = False

        if should_send_processing:
            if is_control_group or (is_private and not is_saved_message):
                 status_msg = await self._send_status_message(message, f"⏳ 正在处理 `{command}`...")
                 edit_target_id = status_msg.id if status_msg else None
            elif command == "更新配方" and is_saved_message:
                 status_msg = await self._send_status_message(message, f"⏳ 正在处理配方更新...")
                 edit_target_id = status_msg.id if status_msg else None

        # --- 指令分发 ---
        if command == "清除状态":
             if not args:
                 clear_help = HELP_DETAILS.get("清除状态", "用法: ,清除状态 <类型>")
                 if "可选类型:" not in clear_help:
                      clear_help += "\n(可选类型: 药园锁, 闭关等待, 传功锁, 传功占位符, 交易订单锁)"
                 await self._edit_or_reply(message.chat.id, edit_target_id, clear_help, original_message=message)
                 return
             await self._command_clear_state(message, args, edit_target_id)
        # ... (其他指令处理保持不变) ...
        elif command == "菜单": await self._command_menu(message, edit_target_id=edit_target_id)
        elif command == "帮助":
            if not args: await self._edit_or_reply(message.chat.id, edit_target_id, HELP_DETAILS.get("帮助", "用法: ,帮助 <指令名>"), original_message=message); return
            await self._command_help(message, args, edit_target_id=edit_target_id)
        elif command == "查询角色": await self.event_bus.emit("query_character_command", message, edit_target_id)
        elif command == "查询背包": await self.event_bus.emit("query_inventory_command", message, edit_target_id)
        elif command == "查询商店": await self.event_bus.emit("query_shop_command", message, edit_target_id)
        elif command == "已学配方": await self.event_bus.emit("query_learned_recipes_command", message, edit_target_id)
        elif command == "查询配方":
            if not args: await self._edit_or_reply(message.chat.id, edit_target_id, HELP_DETAILS.get("查询配方", "用法: ,查询配方 <物品名>"), original_message=message); return
            await self.event_bus.emit("query_recipe_detail_command", message, args.strip(), edit_target_id)
        elif command == "缓存状态": await self.event_bus.emit("query_cache_status_command", message, edit_target_id)
        elif command == "同步角色": await self.event_bus.emit("sync_character_command", message, edit_target_id)
        elif command == "同步背包": await self.event_bus.emit("sync_inventory_command", message, edit_target_id)
        elif command == "同步商店": await self.event_bus.emit("sync_shop_command", message, edit_target_id)
        elif command == "同步物品": await self.event_bus.emit("sync_items_command", message, edit_target_id)
        # --- 修改: 将发送指令的处理移交给 _command_send_game_cmd ---
        elif command == "发送":
             # 直接使用已经移除提及并解析好的 args
             if not args: await self._edit_or_reply(message.chat.id, edit_target_id, HELP_DETAILS.get("发送", "用法: ,发送 <游戏指令>"), original_message=message); return
             await self._command_send_game_cmd(message, args) # 传递 args 而不是重新解析
        # --- 修改结束 ---
        elif command == "智能炼制":
            if not args: await self._edit_or_reply(message.chat.id, edit_target_id, HELP_DETAILS.get("智能炼制"), original_message=message); return
            item_name = args.strip(); quantity = 1
            match_star = re.match(r"(.+?)\s*\*\s*(\d+)$", item_name)
            match_space = re.match(r"(.+?)\s+(\d+)$", item_name)
            if match_star:
                item_name = match_star.group(1).strip()
                try: quantity = int(match_star.group(2)); quantity = max(1, quantity)
                except ValueError: quantity = 1
            elif match_space:
                item_name_candidate = match_space.group(1).strip()
                quantity_candidate_str = match_space.group(2)
                try:
                     quantity_test = int(quantity_candidate_str)
                     if quantity_test > 0: item_name = item_name_candidate; quantity = quantity_test
                except ValueError: pass
            quantity = max(1, quantity)
            self.info(f"解析智能炼制指令: 物品='{item_name}', 数量={quantity}")
            await self.event_bus.emit("smart_crafting_command", message, item_name, quantity, edit_target_id)
        elif command == "更新配方":
             if is_saved_message:
                 recipe_text_to_pass = ""; overwrite_flag = False
                 overwrite_match = re.search(r"(--overwrite)\s*$", args or "", re.IGNORECASE)
                 if overwrite_match: overwrite_flag = True; args_cleaned = (args or "")[:overwrite_match.start()].strip()
                 else: args_cleaned = args or ""
                 cmd_prefix_len = 0; prefix = ',' + command # 只认逗号
                 if raw_text and raw_text.startswith(prefix): cmd_prefix_len = len(prefix)

                 if cmd_prefix_len > 0:
                      recipe_text_raw = raw_text[cmd_prefix_len:].strip()
                      if overwrite_flag: recipe_text_to_pass = re.sub(r"\s*--overwrite\s*$", "", recipe_text_raw, flags=re.IGNORECASE).strip()
                      else: recipe_text_to_pass = recipe_text_raw
                 else: self.warning("无法从消息中提取配方文本前缀长度。")
                 if not recipe_text_to_pass:
                      if message.reply_to_message and (message.reply_to_message.text or message.reply_to_message.caption):
                           recipe_text_to_pass = message.reply_to_message.text or message.reply_to_message.caption; self.info("从回复的消息中获取配方文本。")
                      else:
                           reply_text = "❌ 请将配方文本直接跟在 `,更新配方` 指令后面，或回复包含配方文本的消息。\n" + HELP_DETAILS.get("更新配方", "")
                           await self._edit_or_reply(message.chat.id, edit_target_id, reply_text, original_message=message); return
                 self.info(f"检测到来自收藏夹的更新配方指令 (overwrite={overwrite_flag})，发送事件...")
                 await self.event_bus.emit("update_recipes_command", message, recipe_text_to_pass, overwrite_flag, edit_target_id)
             else: await message.reply_text("❌ `,更新配方` 指令只能在您的“收藏夹”(Saved Messages)中使用。", quote=True)
        elif command == "查询题库":
            qa_type = "玄骨"; keyword = None
            if args:
                parts = args.split(maxsplit=1)
                first_part_lower = parts[0].lower()
                if first_part_lower in ["玄骨", "xuangu"]: qa_type = "玄骨"; keyword = parts[1].strip() if len(parts) > 1 else None
                elif first_part_lower in ["天机", "tianji"]: qa_type = "天机"; keyword = parts[1].strip() if len(parts) > 1 else None
                else: keyword = args.strip()
            await self.event_bus.emit("query_qa_command", message, qa_type, keyword, edit_target_id)
        elif command == "添加题库":
            if not args: await self._edit_or_reply(message.chat.id, edit_target_id, HELP_DETAILS.get("添加题库", "用法: ,添加题库 [玄骨|天机] 问题::答案"), original_message=message); return
            qa_type = "玄骨"; qa_pair = args.strip()
            parts = args.split(maxsplit=1)
            first_part_lower = parts[0].lower()
            if first_part_lower in ["玄骨", "xuangu"] and len(parts) > 1: qa_type = "玄骨"; qa_pair = parts[1].strip()
            elif first_part_lower in ["天机", "tianji"] and len(parts) > 1: qa_type = "天机"; qa_pair = parts[1].strip()
            await self.event_bus.emit("add_update_qa_command", message, qa_type, qa_pair, edit_target_id)
        elif command == "删除题库":
            if not args: await self._edit_or_reply(message.chat.id, edit_target_id, HELP_DETAILS.get("删除题库", "用法: ,删除题库 <编号>"), original_message=message); return
            await self.event_bus.emit("delete_qa_command", message, args.strip(), edit_target_id)
        elif command == "任务列表":
            await self.event_bus.emit("system_show_tasks_command", message, edit_target_id)
        elif command == "插件": await self.event_bus.emit("system_plugins_command", message, args, edit_target_id)
        elif command == "配置": await self.event_bus.emit("system_config_command", message, args, edit_target_id)
        elif command == "日志": await self.event_bus.emit("system_log_command", message, args, edit_target_id)
        elif command == "日志级别": await self.event_bus.emit("system_loglevel_command", message, args, edit_target_id)
        else:
             # 对于其他所有指令 (包括 `,收货`)，事件总线会分发给相应的插件
             # 如果没有插件处理，就不做任何事
             self.debug(f"指令 '{command}' 由 AdminPlugin 分发，等待其他插件处理...")
             # 移除未知指令的回复逻辑


    async def _command_menu(self, message: Message, edit_target_id: int | None = None):
        await self._edit_or_reply(message.chat.id, edit_target_id, COMMAND_MENU_TEXT, original_message=message)

    async def _command_help(self, message: Message, args: str | None, edit_target_id: int | None = None):
         # ... (此函数逻辑保持不变) ...
         if not args:
              reply = HELP_DETAILS.get("帮助", "用法: ,帮助 <指令名>") + "\n\n可查询帮助的指令:\n`" + "`, `".join(sorted(HELP_DETAILS.keys())) + "`"
         else:
              command_name = args.strip().lower(); cleaned_name = command_name.lstrip(',/')
              detail = HELP_DETAILS.get(cleaned_name)
              reply = f"❓ **指令帮助: `,`{cleaned_name}**\n\n{detail}" if detail else f"❌ 找不到指令 `{cleaned_name}` 的帮助信息。\n请发送 `,菜单` 查看可用指令。"
         await self._edit_or_reply(message.chat.id, edit_target_id, reply, original_message=message)

    # --- 修改: _command_send_game_cmd 使用传入的 args ---
    async def _command_send_game_cmd(self, message: Message, game_command_args: str | None):
         """处理 ,发送 指令，直接使用解析好的参数，并移除提及"""
         if not game_command_args: # 检查传入的参数
             reply_text = HELP_DETAILS.get("发送", "❌ 用法: ,发送 <游戏指令>")
             await self._edit_or_reply(message.chat.id, None, reply_text, original_message=message)
             return

         # 移除参数中的 @username 提及
         game_command_cleaned = re.sub(r'@\w+', '', game_command_args).strip()

         if not game_command_cleaned: # 如果移除提及后参数为空
              reply_text = "❌ 发送的指令内容不能为空（移除提及后）。"
              await self._edit_or_reply(message.chat.id, None, reply_text, original_message=message)
              return

         if not self.telegram_client_instance:
             reply_text = "❌ 错误: Telegram 客户端不可用。"
             self.error("无法发送 ,发送 指令: TelegramClient 不可用。")
             await self._edit_or_reply(message.chat.id, None, reply_text, original_message=message)
             return

         try:
             self.info(f"准备通过 ,发送 指令将 '{game_command_cleaned[:50]}...' 加入队列...");
             success = await self.telegram_client_instance.send_game_command(game_command_cleaned) # 发送清理后的指令
             if success:
                 reply_text = f"✅ 指令 `{game_command_cleaned[:50]}{'...' if len(game_command_cleaned) > 50 else ''}` 已加入队列。"
                 self.info(f"指令 '{game_command_cleaned[:50]}...' 已加入队列。")
             else:
                 reply_text = f"❌ 将指令 `{game_command_cleaned[:50]}{'...' if len(game_command_cleaned) > 50 else ''}` 加入队列失败。"
                 self.error(f"通过 ,发送 指令将 '{game_command_cleaned[:50]}...' 加入队列失败。")
         except Exception as e:
             reply_text = f"❌ 发送指令时发生错误: {e}"
             self.error(f"处理 ,发送 指令 '{game_command_cleaned[:50]}...' 时出错: {e}", exc_info=True)

         # 决定在哪里回复 (私聊或控制群)
         if message.chat.type == ChatType.PRIVATE or self.control_chat_id == message.chat.id:
              await self._edit_or_reply(message.chat.id, None, reply_text, original_message=message)
         else: # 如果是在其他群组（理论上不应该，但作为 fallback）
              if self.control_chat_id:
                  await self._send_to_control_chat(f"(指令 '{game_command_cleaned[:20]}...' 执行结果)\n{reply_text}")
    # --- 修改结束 ---

    async def _command_clear_state(self, message: Message, args: str | None, edit_target_id: int | None):
         # ... (此函数逻辑保持不变，已包含 user_id 隔离) ...
         self.info(f"处理 ,清除状态 指令 (参数: {args})")
         if not self.context.redis: await self._edit_or_reply(message.chat.id, edit_target_id, "❌ 错误: Redis 未初始化。", original_message=message); return
         redis_client = self.context.redis.get_client(); my_id = self.telegram_client_instance._my_id if self.telegram_client_instance else None
         if not redis_client: await self._edit_or_reply(message.chat.id, edit_target_id, "❌ 错误: 无法连接到 Redis。", original_message=message); return
         if not my_id: self.warning("清除状态时无法获取 my_id"); await self._edit_or_reply(message.chat.id, edit_target_id, "❌ 错误: 无法获取助手 User ID。", original_message=message); return

         key_to_clear = None; key_name = ""; deleted_count = 0; reply = ""
         args_lower = args.strip().lower() if args else ""

         if args_lower == "药园锁":
             key_to_clear = HERB_GARDEN_ACTION_LOCK_KEY_FORMAT.format(my_id)
             key_name = "药园操作锁"
         elif args_lower == "闭关等待":
             key_to_clear = REDIS_WAITING_KEY_PREFIX.format(my_id) # 闭关等待 Key
             key_name = "闭关等待状态"
         elif args_lower == "传功锁":
             key_to_clear = REDIS_TEACH_LOCK_KEY_FORMAT.format(my_id) # 传功检查锁 Key
             key_name = "传功检查锁"
         elif args_lower == "传功占位符":
             key_to_clear = f"{REDIS_PENDING_PLACEHOLDER_KEY_PREFIX}{my_id}"
             key_name = "传功占位符等待标记"
         elif args_lower == "交易订单锁":
             key_to_clear = f"{REDIS_ORDER_EXEC_LOCK_PREFIX}*:{my_id}" # 使用通配符 *
             key_name = f"当前账号({my_id})的所有交易执行锁"
         else:
             reply = HELP_DETAILS.get("清除状态", "❌ 参数错误。用法: ,清除状态 <类型>")
             await self._edit_or_reply(message.chat.id, edit_target_id, reply, original_message=message); return

         try:
             if '*' in key_to_clear:
                 self.info(f"准备使用 SCAN 删除匹配 '{key_to_clear}' 的键...")
                 keys_found = []
                 async for key in redis_client.scan_iter(match=key_to_clear):
                     keys_found.append(key)
                 deleted_count = 0
                 if keys_found:
                      self.info(f"找到 {len(keys_found)} 个匹配的键，正在删除...")
                      deleted_count = await redis_client.delete(*keys_found)
                 if deleted_count > 0: reply = f"✅ 已成功清除 Redis 中匹配 **{key_name}** 的 {deleted_count} 个键。"
                 else: reply = f"ℹ️ Redis 中未找到匹配 **{key_name}** 的键。"
             else:
                 deleted_count = await redis_client.delete(key_to_clear)
                 if deleted_count > 0: reply = f"✅ 已成功清除 **{key_name}** (Key: `{key_to_clear}`)。"; self.info(f"已清除 Key: {key_to_clear}")
                 else: reply = f"ℹ️ 未找到 **{key_name}** (Key: `{key_to_clear}`)。"; self.info(f"尝试清除 Key 时未找到: {key_to_clear}")
         except Exception as e: reply = f"❌ 清除 Redis 状态时发生错误: {e}"; self.error(f"清除 Key '{key_to_clear}' 时出错: {e}", exc_info=True)
         await self._edit_or_reply(message.chat.id, edit_target_id, reply, original_message=message)

    # --- 辅助函数 (_edit_or_reply, _send_status_message, _send_to_control_chat) 保持不变 ---
    async def _edit_or_reply(self, chat_id: int, message_id: int | None, text: str, original_message: Message):
        tg_client = self.telegram_client_instance
        if not tg_client or not tg_client.app.is_connected: self.error("无法编辑/回复：TG 客户端不可用。"); return
        edited = False; link_preview_options = LinkPreviewOptions(is_disabled=True); MAX_LEN = 4096
        if len(text) > MAX_LEN:
            self.warning(f"即将发送/编辑的消息过长 ({len(text)} > {MAX_LEN})，将被截断。")
            text = text[:MAX_LEN - 15] + "\n...(消息过长截断)"

        if message_id:
            try:
                await tg_client.app.edit_message_text(chat_id, message_id, text, link_preview_options=link_preview_options)
                edited = True
            except Exception as e:
                if "MESSAGE_NOT_MODIFIED" not in str(e) and "MESSAGE_ID_INVALID" not in str(e):
                    self.warning(f"编辑消息 {message_id} 失败 ({e})，将尝试回复...")
                    edited = False
                elif "MESSAGE_ID_INVALID" in str(e):
                     self.warning(f"编辑消息 {message_id} 失败 (MESSAGE_ID_INVALID)，将尝试回复...")
                     edited = False
                else: # MESSAGE_NOT_MODIFIED
                    self.debug(f"消息 {message_id} 未修改。")
                    edited = True

        if not edited:
            if not original_message:
                 self.error("无法回复：缺少原始消息对象。")
                 await self._send_to_control_chat(f"(回复原始消息失败)\n{text[:1000]}...")
                 return
            try:
                reply_params = ReplyParameters(message_id=original_message.id)
                await tg_client.app.send_message(chat_id, text, reply_parameters=reply_params, link_preview_options=link_preview_options)
            except Exception as e2:
                self.error(f"直接回复原始消息 {original_message.id} 失败: {e2}，尝试不引用回复...")
                try: await tg_client.app.send_message(chat_id, text, link_preview_options=link_preview_options)
                except Exception as e3:
                     self.error(f"编辑、回复和直接发送均失败: {e3}")
                     await self._send_to_control_chat(f"(回复失败)\n{text[:1000]}...")

    async def _send_status_message(self, original_message: Message, status_text: str) -> Message | None:
        tg_client = self.telegram_client_instance
        if not tg_client or not tg_client.app.is_connected: self.warning("无法发送状态消息：TG 客户端不可用。"); return None
        link_preview_options = LinkPreviewOptions(is_disabled=True)
        try:
             reply_params = ReplyParameters(message_id=original_message.id)
             return await tg_client.app.send_message(original_message.chat.id, status_text, reply_parameters=reply_params, link_preview_options=link_preview_options)
        except Exception as e:
            self.warning(f"回复状态消息失败 ({e})，尝试直接发送...")
            try: return await tg_client.app.send_message(original_message.chat.id, status_text, link_preview_options=link_preview_options)
            except Exception as e2: self.error(f"直接发送状态消息也失败: {e2}"); return None

    async def _send_to_control_chat(self, text: str):
         tg_client = self.telegram_client_instance
         fallback_chat_id = self.control_chat_id or self.admin_id
         if not tg_client or not tg_client.app.is_connected or not fallback_chat_id:
              self.error(f"无法发送到控制群/管理员：TG 客户端不可用或未配置 ID。消息: {text[:100]}...")
              return
         try:
              link_preview_options = LinkPreviewOptions(is_disabled=True)
              await tg_client.app.send_message(fallback_chat_id, text, link_preview_options=link_preview_options)
         except Exception as final_err:
              self.critical(f"最终 fallback 发送失败: {final_err}")


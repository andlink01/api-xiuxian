import logging
from pyrogram.types import Message, ReplyParameters, LinkPreviewOptions
from pyrogram.enums import ChatType
import asyncio
from datetime import datetime
from plugins.character_sync_plugin import format_local_time # 需要保留

from plugins.base_plugin import BasePlugin, AppContext
# 导入新的触发函数 (用于类型提示和检查)
try: from plugins.character_sync_plugin import trigger_character_sync
except ImportError: trigger_character_sync = None
try: from plugins.item_sync_plugin import trigger_item_sync_update
except ImportError: trigger_item_sync_update = None
try: from plugins.shop_sync_plugin import trigger_shop_sync_update
except ImportError: trigger_shop_sync_update = None

# 导入辅助函数
from plugins.utils import edit_or_reply, get_my_id # <-- 导入 get_my_id

class Plugin(BasePlugin):
    """
    处理所有手动同步指令 (,同步角色, ,同步背包 等)。
    (Phase 1: 触发 DataManager 更新缓存并报告结果)
    """
    def __init__(self, context: AppContext, plugin_name: str, cn_name: str | None = None):
        super().__init__(context, plugin_name, cn_name)
        self.info("插件已加载。")

    def register(self):
        """注册 sync command 事件监听器"""
        self.event_bus.on("sync_character_command", self.handle_sync_character)
        self.event_bus.on("sync_inventory_command", self.handle_sync_inventory)
        self.event_bus.on("sync_shop_command", self.handle_sync_shop)
        self.event_bus.on("sync_items_command", self.handle_sync_items)
        self.info("已注册 sync command 事件监听器。")

    async def handle_sync_character(self, message: Message, edit_target_id: int | None):
        """处理 ,同步角色 指令 (触发 DataManager 更新)"""
        self.info("处理 ,同步角色 指令...")

        sync_start_time = datetime.now()
        success = False
        result_msg = "角色同步插件未加载或触发函数不可用。" # 默认错误

        # --- 新增: 获取 user_id 和 username ---
        user_id = await get_my_id(self, message, edit_target_id)
        username = self.context.telegram_client._my_username if self.context.telegram_client else None

        if not user_id or not username:
             self.error("无法获取 User ID 或 Username，无法执行同步。")
             result_msg = "❌ 无法获取助手用户信息"
             success = False
        # --- 新增结束 ---
        elif trigger_character_sync: # 检查函数是否成功导入
            try:
                # --- 修改: 传递 user_id 和 username ---
                success, result_msg = await trigger_character_sync(self.context, user_id, username)
                # --- 修改结束 ---
                if success:
                     self.info("手动同步角色/背包：DataManager 更新成功。")
                     result_msg = "✅ 角色与背包缓存更新成功。"
                else:
                     self.error(f"手动同步角色/背包：DataManager 更新失败: {result_msg}")
                     # result_msg 已经是 trigger_character_sync 返回的错误信息，无需修改
            except Exception as e:
                 success = False
                 result_msg = f"触发缓存更新时发生意外错误: {e}"
                 self.error(f"手动同步角色/背包：触发更新时出错: {e}", exc_info=True)
        else:
             self.error("无法执行同步：trigger_character_sync 函数未找到。")


        sync_end_time = datetime.now()
        now_aware = sync_end_time.astimezone()
        time_str = format_local_time(now_aware)

        if success:
            reply_text = f"{result_msg}\n\n🕒 *完成时间: {time_str}*"
        else:
            # 使用已有的 result_msg (包含错误信息)
            reply_text = f"❌ **触发角色/背包缓存更新失败**\n原因: {result_msg}"

        await edit_or_reply(self, message.chat.id, edit_target_id, reply_text, original_message=message)

    async def handle_sync_inventory(self, message: Message, edit_target_id: int | None):
        """处理 ,同步背包 指令 (逻辑同同步角色)"""
        self.info("处理 ,同步背包 指令...")
        await self.handle_sync_character(message, edit_target_id) # 复用

    async def handle_sync_shop(self, message: Message, edit_target_id: int | None):
        """处理 ,同步商店 指令 (触发 DataManager 更新)"""
        self.info("处理 ,同步商店 指令...")
        if not trigger_shop_sync_update:
            self.warning("商店同步插件未加载或无法导入触发函数。")
            await edit_or_reply(self, message.chat.id, edit_target_id, "❌ 商店同步插件 (shop_sync_plugin) 未加载或版本不兼容。", original_message=message)
            return

        self.info("开始手动触发商店缓存更新...")
        sync_start_time = datetime.now()
        success = False
        result_msg = "触发商店缓存更新时发生错误。" # 默认错误
        try:
            # trigger_shop_sync_update 不需要 user_id/username 参数
            success, _, result_msg_internal = await trigger_shop_sync_update(self.context, force=True)
            if success:
                 self.info("手动触发商店缓存更新成功。")
                 result_msg = "✅ 商店缓存更新成功。"
            else:
                 self.error(f"手动触发商店缓存更新失败: {result_msg_internal}")
                 result_msg = f"❌ DataManager 更新失败: {result_msg_internal}"
        except Exception as e:
             success = False
             result_msg = f"触发缓存更新时发生意外错误: {e}"
             self.error(f"手动触发商店缓存更新时出错: {e}", exc_info=True)

        sync_end_time = datetime.now()
        now_aware = sync_end_time.astimezone()
        time_str = format_local_time(now_aware)

        if success:
            reply_text = f"{result_msg}\n\n🕒 *完成时间: {time_str}*"
        else:
            reply_text = f"❌ **触发商店缓存更新失败**\n原因: {result_msg}"

        await edit_or_reply(self, message.chat.id, edit_target_id, reply_text, original_message=message)


    async def handle_sync_items(self, message: Message, edit_target_id: int | None):
        """处理 ,同步物品 指令 (触发 DataManager 更新)"""
        self.info("处理 ,同步物品 指令...")
        if not trigger_item_sync_update:
            self.warning("物品同步插件未加载或无法导入触发函数。")
            await edit_or_reply(self, message.chat.id, edit_target_id, "❌ 物品同步插件 (item_sync_plugin) 未加载或版本不兼容。", original_message=message)
            return

        self.info("开始手动触发物品主数据缓存更新...")
        sync_start_time = datetime.now()
        success = False
        result_msg = "触发物品缓存更新时发生错误。" # 默认错误
        try:
            # trigger_item_sync_update 不需要 user_id/username 参数
            success, count, result_msg_internal = await trigger_item_sync_update(self.context, force=True)
            if success and count != -1: # count == -1 表示跳过，强制模式不应跳过
                 self.info("手动触发物品主数据缓存更新成功。")
                 result_msg = "✅ 物品主数据缓存更新成功。"
            elif success and count == -1: # 理论上不会发生
                 self.warning("强制物品同步被跳过？")
                 result_msg = f"ℹ️ 同步被跳过: {result_msg_internal}"
                 success = False # 算作未完全成功
            else:
                 self.error(f"手动触发物品主数据缓存更新失败: {result_msg_internal}")
                 result_msg = f"❌ DataManager 更新失败: {result_msg_internal}"
        except Exception as e:
             success = False
             result_msg = f"触发缓存更新时发生意外错误: {e}"
             self.error(f"手动触发物品缓存更新时出错: {e}", exc_info=True)

        sync_end_time = datetime.now()
        now_aware = sync_end_time.astimezone()
        time_str = format_local_time(now_aware)

        if success:
            reply_text = f"{result_msg}\n\n🕒 *完成时间: {time_str}*"
        else:
            reply_text = f"❌ **触发物品主数据缓存更新失败**\n原因: {result_msg}"

        await edit_or_reply(self, message.chat.id, edit_target_id, reply_text, original_message=message)


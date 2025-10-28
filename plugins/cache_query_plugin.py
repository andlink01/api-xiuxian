import logging
import asyncio
from datetime import datetime
from typing import Optional, Dict, List, Tuple, Any
from plugins.base_plugin import BasePlugin, AppContext
from pyrogram.types import Message
# 导入 GDM Key 定义 和 格式化 TTL 的函数
from modules.game_data_manager import (
    CHAR_STATUS_KEY, CHAR_INVENTORY_KEY, CHAR_SECT_KEY, CHAR_GARDEN_KEY,
    CHAR_PAGODA_KEY, CHAR_RECIPES_KEY, GAME_ITEMS_MASTER_KEY, GAME_SHOP_KEY,
    format_ttl_internal
)
from plugins.utils import edit_or_reply, get_my_id

logger = logging.getLogger(__name__)

# 定义要查询状态的缓存键类型及其描述
CACHE_KEYS_TO_QUERY = {
    "status": ("角色状态", CHAR_STATUS_KEY),
    "inventory": ("背包", CHAR_INVENTORY_KEY),
    "sect": ("宗门信息", CHAR_SECT_KEY),
    "garden": ("药园", CHAR_GARDEN_KEY),
    "pagoda": ("闯塔进度", CHAR_PAGODA_KEY),
    "recipes": ("已学配方", CHAR_RECIPES_KEY),
    "shop": ("商店", GAME_SHOP_KEY),
    "item_master": ("物品主数据", GAME_ITEMS_MASTER_KEY),
}

# --- 插件类 ---
class Plugin(BasePlugin):
    """处理缓存状态查询指令"""
    def __init__(self, context: AppContext, plugin_name: str, cn_name: str | None = None):
        super().__init__(context, plugin_name, cn_name or "缓存查询")
        self.info("插件已加载。")

    def register(self):
        """注册指令事件监听器"""
        self.event_bus.on("query_cache_status_command", self.handle_query_cache_status)
        self.info("已注册 query_cache_status_command 事件监听器。")

    async def handle_query_cache_status(self, message: Message, edit_target_id: int | None):
        """处理 ,缓存状态 指令"""
        self.info("处理 ,缓存状态 指令...")
        my_id = await get_my_id(self, message, edit_target_id)
        
        if not self.data_manager:
            await edit_or_reply(self, message.chat.id, edit_target_id, "❌ 错误: GameDataManager 未初始化。", original_message=message); return
        
        # --- 修复: 移除初始 "处理中" 消息 ---
        # await edit_or_reply(self, message.chat.id, edit_target_id, "⏳ 正在查询主要数据缓存状态...", original_message=message)
        # --- 修复结束 ---

        reply_lines = ["📊 **主要数据缓存状态** 📊\n"]
        has_error = False

        for key_type, (desc, key_template) in CACHE_KEYS_TO_QUERY.items():
            user_specific = "{}" in key_template
            key_to_check = None
            if user_specific:
                if my_id: key_to_check = key_template.format(my_id)
                else: reply_lines.append(f"❓ {desc}: 无法查询 (缺少 User ID)"); continue
            else: key_to_check = key_template

            if not key_to_check: continue

            try:
                # 调用 GDM 获取详细信息
                data, ttl, last_updated = await self.data_manager.get_cached_data_with_details(key_type, my_id or 0)
                status_icon = "❓"; time_info = "未知"; ttl_info = "未知"

                if data is not None: # 缓存存在且有数据
                    status_icon = "✅"
                    time_info = f"更新于: {last_updated}" if last_updated else "更新时间未知"
                    ttl_info = f"剩余: {format_ttl_internal(ttl)}" if ttl is not None else "无 TTL 或已过期"
                elif ttl == -2: # Key 不存在 (GDM 的 _get_cache_data 返回 (None, -2, None))
                     status_icon = "❌"
                     time_info = "从未缓存"
                     ttl_info = "不存在"
                elif ttl is not None and ttl >= -1: # 键存在但数据为 None (例如刚过期) (TTL >= 0 或 -1 表示无过期)
                     status_icon = "⚠️"
                     time_info = f"上次更新: {last_updated}" if last_updated else "更新时间未知"
                     ttl_info = f"剩余: {format_ttl_internal(ttl)}" if ttl != -1 else "永不自动过期"
                else: # 获取缓存出错 (GDM 返回 (None, None, None))
                     status_icon = "❓"
                     time_info = "查询出错"
                     ttl_info = "查询出错"
                reply_lines.append(f"{status_icon} **{desc}**: {time_info} ({ttl_info})")
            except Exception as e:
                self.error(f"查询缓存 '{key_to_check}' 状态时出错: {e}", exc_info=True)
                reply_lines.append(f"❓ **{desc}**: 查询时发生错误"); has_error = True

        if has_error: reply_lines.append("\n(部分缓存状态查询出错，请检查日志)")
        final_reply = "\n".join(reply_lines)
        # edit_target_id 为 None，将直接回复
        await edit_or_reply(self, message.chat.id, edit_target_id, final_reply, original_message=message)


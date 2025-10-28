import logging
import json
import asyncio
from datetime import datetime
from collections import defaultdict
from pyrogram.types import Message
from plugins.base_plugin import BasePlugin, AppContext
from plugins.character_sync_plugin import format_local_time # 保留

# 移除 get_redis_ttl_and_value
from plugins.utils import (
    get_my_id,
    # get_redis_ttl_and_value,
    edit_or_reply,
)
# 移除 REDIS_INV_KEY_PREFIX
from plugins.constants import SHOP_ITEM_TYPE_TRANSLATION

# 辅助函数：格式化 TTL
def format_ttl(ttl_seconds: int | None) -> str:
    if ttl_seconds is None or ttl_seconds < 0: return "未知或已过期"
    if ttl_seconds < 60: return f"{ttl_seconds} 秒"
    elif ttl_seconds < 3600: return f"{round(ttl_seconds / 60)} 分钟"
    else: return f"{round(ttl_seconds / 3600, 1)} 小时"

class Plugin(BasePlugin):
    """
    处理 ,查询背包 指令的插件 (仅查询缓存，通过 DataManager)。
    """
    def __init__(self, context: AppContext, plugin_name: str, cn_name: str | None = None):
        super().__init__(context, plugin_name, cn_name)
        self.info("插件已加载。")

    def register(self):
        """注册查询指令事件监听器"""
        self.event_bus.on("query_inventory_command", self.handle_query_inventory)
        self.info("已注册 query_inventory_command 事件监听器。")

    async def handle_query_inventory(self, message: Message, edit_target_id: int | None):
        """处理 ,查询背包 指令 (仅查询缓存，通过 DataManager)"""
        self.info("处理 ,查询背包 指令 (通过 DataManager)...")
        my_id = await get_my_id(self, message, edit_target_id)
        if not my_id: return
        if not self.data_manager:
            await edit_or_reply(self, message.chat.id, edit_target_id, "❌ 错误: GameDataManager 未初始化。", original_message=message); return

        # --- 调用 DataManager 获取数据 ---
        inv_data, cache_ttl, last_updated_str = await self.data_manager.get_cached_data_with_details('inventory', my_id)
        # --- 获取结束 ---

        source = "缓存"

        if inv_data is None:
            self.info(f"背包缓存 (用户: {my_id}) 为空或读取失败。")
            await edit_or_reply(self, message.chat.id, edit_target_id, f"ℹ️ 储物袋信息缓存为空或读取失败。\n请使用 `,同步背包` 指令获取最新数据。", original_message=message)
            return

        try:
            summary = inv_data.get("summary", {})
            items_by_type = inv_data.get("items_by_type", {})
            reply = f"🎒 **储物袋** ({source})\n\n"
            reply += f"📦 总类: {summary.get('total_types', 'N/A')} | "
            reply += f"🌿 材料: {summary.get('material_types', 'N/A')} 种\n"

            type_map = SHOP_ITEM_TYPE_TRANSLATION
            output_order = ["treasure", "elixir", "material", "recipe", "talisman", "seed", "quest_item", "formation", "badge", "special_item", "loot_box", "special_tool"]
            output_lines = []
            found_items = False

            for type_key in output_order:
                display_name = type_map.get(type_key, f"{type_key.capitalize()}❓")
                items = items_by_type.get(type_key)
                if items and isinstance(items, list):
                    found_items = True
                    output_lines.append(f"\n**{display_name} ({len(items)}):**")
                    max_items_to_show = 15 ; count = 0
                    items.sort(key=lambda x: x.get('quantity', 0) if isinstance(x, dict) else 0, reverse=True)
                    for item in items:
                        if isinstance(item, dict):
                            if count < max_items_to_show:
                                output_lines.append(f"  • `{item.get('name', '?')}` x{item.get('quantity', 0):,}")
                                count += 1
                            else:
                                output_lines.append(f"  • ...等 {len(items) - count} 种"); break
                        else: self.warning(f"背包数据中类型 '{type_key}' 下的 item 不是字典: {item}")
                elif items: self.warning(f"背包数据中 items_by_type['{type_key}'] 不是列表: {items}")

            other_types = set(items_by_type.keys()) - set(output_order)
            if other_types:
                has_other_content = False; other_lines = []
                for t in sorted(list(other_types)):
                     other_items = items_by_type.get(t, [])
                     item_count = len(other_items) if isinstance(other_items, list) else 0
                     if item_count > 0: other_lines.append(f"`{t}`({item_count})"); has_other_content = True
                if has_other_content:
                    found_items = True; output_lines.append("\n**❓ 其他:**"); output_lines.append("  • " + ", ".join(other_lines))

            if not found_items: output_lines.append("\n_(储物袋为空)_")
            reply += "\n".join(output_lines)

            # --- 统一显示更新时间和过期时间 ---
            reply += "\n"
            if last_updated_str: reply += f"\n🕒 数据更新于: {last_updated_str}"
            else: reply += f"\n🕒 数据更新时间: 未知"
            ttl_formatted = format_ttl(cache_ttl)
            reply += f"\n⏳ 缓存将在约 {ttl_formatted} 后过期"
            # --- 统一显示结束 ---

            await edit_or_reply(self, message.chat.id, edit_target_id, reply, original_message=message)
            self.info("成功查询并回复背包缓存信息 (通过 DataManager)。")

        except Exception as e:
             self.error(f"格式化背包信息出错: {e}", exc_info=True)
             await edit_or_reply(self, message.chat.id, edit_target_id, "❌ 格式化背包缓存信息时发生错误。", original_message=message)


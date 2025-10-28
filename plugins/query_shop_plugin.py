import logging
import json
import asyncio
from datetime import datetime
from collections import defaultdict
from pyrogram.types import Message
from plugins.base_plugin import BasePlugin, AppContext

# 移除 get_redis_ttl_and_value
from plugins.utils import (
    get_my_id,
    # get_redis_ttl_and_value,
    edit_or_reply,
)

# 移除 REDIS_SHOP_KEY_PREFIX
from plugins.constants import SHOP_ITEM_TYPE_TRANSLATION

# 辅助函数：格式化 TTL
def format_ttl(ttl_seconds: int | None) -> str:
    if ttl_seconds is None or ttl_seconds < 0: return "未知或已过期"
    if ttl_seconds < 60: return f"{ttl_seconds} 秒"
    elif ttl_seconds < 3600: return f"{round(ttl_seconds / 60)} 分钟"
    else: return f"{round(ttl_seconds / 3600, 1)} 小时"

class Plugin(BasePlugin):
    """
    处理 ,查询商店 指令的插件 (仅查询缓存，通过 DataManager)。
    """
    def __init__(self, context: AppContext, plugin_name: str, cn_name: str | None = None):
        super().__init__(context, plugin_name, cn_name)
        self.info("插件已加载。")

    def register(self):
        """注册查询指令事件监听器"""
        self.event_bus.on("query_shop_command", self.handle_query_shop)
        self.info("已注册 query_shop_command 事件监听器。")

    async def handle_query_shop(self, message: Message, edit_target_id: int | None):
        """处理 ,查询商店 指令 (仅查询缓存，通过 DataManager)"""
        self.info("处理 ,查询商店 指令 (通过 DataManager)...")
        my_id = await get_my_id(self, message, edit_target_id)
        if not my_id: return
        if not self.data_manager:
            await edit_or_reply(self, message.chat.id, edit_target_id, "❌ 错误: GameDataManager 未初始化。", original_message=message); return

        shop_sync_plugin_status = self.context.plugin_statuses.get("shop_sync_plugin", 'not_loaded')
        if shop_sync_plugin_status != 'enabled':
             self.warning(f"商店同步插件状态为 '{shop_sync_plugin_status}'，商店缓存可能不存在或已过期。")

        # --- 调用 DataManager 获取数据 ---
        # get_cached_data_with_details 返回的是包含时间戳和 'items' 的外层字典
        shop_data_wrapped, cache_ttl, last_updated_str = await self.data_manager.get_cached_data_with_details('shop', my_id)
        shop_items_dict = None # 商店物品字典
        if isinstance(shop_data_wrapped, dict):
             shop_items_dict = shop_data_wrapped.get("items")
             if not isinstance(shop_items_dict, dict):
                 self.error("DataManager 返回的商店数据内部 'items' 格式错误。")
                 shop_items_dict = None # 置空以触发未命中逻辑
        # --- 获取结束 ---

        source = "缓存"

        if shop_items_dict is None: # 缓存未命中、错误或内部格式错误
             self.info(f"商店缓存 (用户: {my_id}) 为空或读取/解析失败。")
             if shop_sync_plugin_status != 'enabled':
                 await edit_or_reply(self, message.chat.id, edit_target_id, f"ℹ️ 商店信息缓存为空，且商店同步插件状态为 '{shop_sync_plugin_status}'。\n请先启用插件并等待同步。", original_message=message)
             else:
                 await edit_or_reply(self, message.chat.id, edit_target_id, f"ℹ️ 商店信息缓存为空或读取失败。\n请使用 `,同步商店` 指令获取最新数据。", original_message=message)
             return

        try:
            reply = f"🏦 **宗门宝库** ({source})\n\n"
            items = list(shop_items_dict.values()) # 从物品字典获取列表

            if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
                 self.error(f"商店缓存数据格式错误 ('items' 值不是字典列表)，实际: {type(items)}")
                 await edit_or_reply(self, message.chat.id, edit_target_id, "❌ 商店缓存数据格式错误。", original_message=message)
                 return

            items.sort(key=lambda x: (
                list(SHOP_ITEM_TYPE_TRANSLATION.keys()).index(x.get('type', 'zzz')) if x.get('type') in SHOP_ITEM_TYPE_TRANSLATION else 999,
                x.get('price', 999999) if isinstance(x.get('price'), (int, float)) else 999999
            ))

            output_lines = []
            current_type = None
            MAX_ITEMS_TO_SHOW = 50
            count = 0
            for item in items:
                if count >= MAX_ITEMS_TO_SHOW:
                    output_lines.append(f"\n...等 {len(items) - count} 件商品（已省略）")
                    break
                item_type_en = item.get('type', 'unknown')
                item_type_cn = SHOP_ITEM_TYPE_TRANSLATION.get(item_type_en, f"{item_type_en.capitalize()}❓")
                if item_type_cn != current_type:
                    if current_type is not None and output_lines: output_lines.append("")
                    output_lines.append(f"**{item_type_cn}:**")
                    current_type = item_type_cn
                item_name = item.get('name', '未知物品')
                item_price = item.get('price', '?')
                sect_exclusive = item.get('sect_exclusive')
                sect_str = f" ({sect_exclusive}专属)" if sect_exclusive else ""
                price_str = f"{item_price:,}" if isinstance(item_price, (int, float)) else str(item_price)
                output_lines.append(f"  • `{item_name}` - {price_str} 贡献{sect_str}")
                count += 1

            if not output_lines: reply += "_(宝库为空)_"
            else: reply += "\n".join(output_lines)

            # --- 统一显示更新时间和过期时间 ---
            reply += "\n"
            if last_updated_str: reply += f"\n🕒 数据更新于: {last_updated_str}"
            else: reply += f"\n🕒 数据更新时间: 未知"
            ttl_formatted = format_ttl(cache_ttl)
            reply += f"\n⏳ 缓存将在约 {ttl_formatted} 后过期"
            # --- 统一显示结束 ---

            await edit_or_reply(self, message.chat.id, edit_target_id, reply, original_message=message)
            self.info("成功查询并回复商店缓存信息 (通过 DataManager)。")

        except Exception as e:
             self.error(f"格式化商店信息出错: {e}", exc_info=True)
             await edit_or_reply(self, message.chat.id, edit_target_id, "❌ 格式化商店缓存信息时发生错误。", original_message=message)


import logging
import json
import asyncio
from datetime import datetime
from pyrogram.types import Message
from plugins.base_plugin import BasePlugin, AppContext
from plugins.character_sync_plugin import format_local_time # 需要保留

# 移除 get_redis_ttl_and_value
from plugins.utils import (
    get_my_id,
    # get_redis_ttl_and_value,
    edit_or_reply,
)
# 移除 REDIS_CHAR_KEY_PREFIX
from plugins.constants import STATUS_TRANSLATION

# 辅助函数：格式化 TTL
def format_ttl(ttl_seconds: int | None) -> str:
    if ttl_seconds is None or ttl_seconds < 0:
        return "未知或已过期"
    if ttl_seconds < 60:
        return f"{ttl_seconds} 秒"
    elif ttl_seconds < 3600:
        return f"{round(ttl_seconds / 60)} 分钟"
    else:
        return f"{round(ttl_seconds / 3600, 1)} 小时"

class Plugin(BasePlugin):
    """
    处理 ,查询角色 指令的插件 (仅查询缓存，通过 DataManager)。
    """
    def __init__(self, context: AppContext, plugin_name: str, cn_name: str | None = None):
        super().__init__(context, plugin_name, cn_name)
        self.info("插件已加载。")

    def register(self):
        """注册查询指令事件监听器"""
        self.event_bus.on("query_character_command", self.handle_query_character)
        self.info("已注册 query_character_command 事件监听器。")

    async def handle_query_character(self, message: Message, edit_target_id: int | None):
        """处理 ,查询角色 指令 (仅查询缓存，通过 DataManager)"""
        self.info("处理 ,查询角色 指令 (通过 DataManager)...")
        my_id = await get_my_id(self, message, edit_target_id)
        if not my_id: return
        if not self.data_manager:
            await edit_or_reply(self, message.chat.id, edit_target_id, "❌ 错误: GameDataManager 未初始化。", original_message=message); return

        # --- 调用 DataManager 获取数据 ---
        char_data, cache_ttl, last_updated_str = await self.data_manager.get_cached_data_with_details('status', my_id)
        sect_data, _, _ = await self.data_manager.get_cached_data_with_details('sect', my_id)
        # --- 获取结束 ---

        source = "缓存"

        if char_data is None:
            self.info(f"角色状态缓存 (用户: {my_id}) 为空或读取失败。")
            await edit_or_reply(self, message.chat.id, edit_target_id, f"ℹ️ 角色信息缓存为空或读取失败。\n请使用 `,同步角色` 指令获取最新数据。", original_message=message)
            return

        # 合并数据用于显示
        display_data = {**(char_data or {}), **(sect_data or {})}

        try:
            status_en = display_data.get('status', 'N/A')
            status_cn = STATUS_TRANSLATION.get(status_en, status_en)
            reply = f"👤 **角色信息** ({source})\n\n"
            reply += f"🏷 道号: `{display_data.get('dao_name', 'N/A')}`\n"
            reply += f"⚡ 境界: {display_data.get('cultivation_level', 'N/A')}\n"
            reply += f"📈 修为: {display_data.get('cultivation_points', 0):,}\n"
            reply += f"🌟 灵根: {display_data.get('spirit_root', 'N/A')}\n"
            reply += f"🏛 门派: {display_data.get('sect_name', 'N/A')}\n"
            reply += f"💎 贡献: {display_data.get('sect_contribution', 0):,}\n"
            reply += f"🧠 神识: {display_data.get('shenshi_points', 0):,}\n"
            reply += f"☠️ 丹毒: {display_data.get('drug_poison_points', 0)}\n"
            reply += f"⚔️ 战绩: {display_data.get('kill_count', 'N/A')}杀 / {display_data.get('death_count', 'N/A')}死\n"
            reply += f"🚦 状态: {status_cn} {'(瓶颈!)' if display_data.get('is_bottleneck') else ''}\n"

            cult_cd_formatted = display_data.get('cultivation_cooldown_until_formatted')
            deep_cd_formatted = display_data.get('deep_seclusion_end_time_formatted')
            if cult_cd_formatted: reply += f"⏳ 闭关冷却: {cult_cd_formatted}\n"
            if deep_cd_formatted: reply += f"⏳ 深度闭关结束: {deep_cd_formatted}\n"

            # 阵法和 Buff (从 status 数据中获取)
            form_exp_formatted = None; active_formation_data = display_data.get('active_formation'); form_id = None
            if isinstance(active_formation_data, dict): form_exp_formatted = active_formation_data.get('expiry_time_formatted'); form_id = active_formation_data.get('id')
            buff_exp_formatted = None; active_buff_data = display_data.get('active_yindao_buff'); buff_name = None
            if isinstance(active_buff_data, dict): buff_exp_formatted = active_buff_data.get('expiry_time_formatted'); buff_name = active_buff_data.get('name')
            if form_id: reply += f"✨ 阵法: {form_id} (至: {form_exp_formatted or '未知'})\n"
            if buff_name: reply += f"🌿 Buff: {buff_name} (至: {buff_exp_formatted or '未知'})\n"

            badge = display_data.get('active_badge');
            cons_days = display_data.get('consecutive_check_in_days');
            div_count = display_data.get('divination_count_today')
            if badge: reply += f"🏅 徽章: {badge}\n"
            if cons_days is not None: reply += f"🗓️ 连续签到: {cons_days} 天\n"
            if div_count is not None: reply += f"☯️ 今日卜卦: {div_count} 次\n"

            # --- 统一显示更新时间和过期时间 ---
            reply += "\n"
            if last_updated_str: reply += f"🕒 数据更新于: {last_updated_str}\n"
            else: reply += f"🕒 数据更新时间: 未知\n"
            ttl_formatted = format_ttl(cache_ttl)
            reply += f"⏳ 缓存将在约 {ttl_formatted} 后过期"
            # --- 统一显示结束 ---

            await edit_or_reply(self, message.chat.id, edit_target_id, reply, original_message=message)
            self.info("成功查询并回复角色缓存信息 (通过 DataManager)。")

        except Exception as e:
             self.error(f"格式化角色信息出错: {e}", exc_info=True)
             await edit_or_reply(self, message.chat.id, edit_target_id, "❌ 格式化角色缓存信息时发生错误。", original_message=message)


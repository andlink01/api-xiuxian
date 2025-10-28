import logging
import asyncio
import json
from collections import defaultdict
import pytz
from datetime import datetime, timedelta
from plugins.base_plugin import BasePlugin, AppContext
# --- 修改: 导入 trigger_item_sync_update ---
from plugins.item_sync_plugin import trigger_item_sync_update as trigger_item_sync
# --- 修改结束 ---
from core.context import get_global_context
# --- 修改: 导入 trigger_character_sync ---
from plugins.character_sync_plugin import trigger_character_sync as trigger_combined_sync
# --- 修改结束 ---

_inv_sync_fetch_logger = logging.getLogger("InvSync.Fetch")
async def fetch_and_store_inventory_data(context: AppContext) -> tuple[bool, str]:
    """
    (已废弃) 触发统一的角色/背包同步。
    """
    logger = _inv_sync_fetch_logger
    logger.warning("【背包同步】fetch_and_store_inventory_data 被调用，此函数已废弃，请检查调用来源。") # 添加警告

    if not context:
        msg = "内部错误: 无 AppContext"
        logger.error(f"【背包同步】获取背包失败：{msg}")
        return False, msg

    logger.info("【背包同步】(手动/查询) 触发... 调用统一的同步触发函数...")
    try:
        # 调用新的触发函数
        success, result_msg = await trigger_combined_sync(context)
        if not success:
            logger.error(f"【背包同步】统一同步触发函数调用失败: {result_msg}")
            return False, result_msg
        else:
            # 成功触发，但无法直接返回背包摘要
            logger.info(f"【背包同步】统一同步触发成功: {result_msg}")
            # 返回一个通用的成功消息
            return True, "✅ 角色/背包缓存更新已触发。"

    except Exception as e:
        msg = f"调用统一同步触发函数时发生意外错误: {e}"
        logger.error(f"【背包同步】{msg}", exc_info=True)
        return False, msg

# --- Plugin Class ---
class Plugin(BasePlugin):
    def __init__(self, context: AppContext, plugin_name: str, cn_name: str | None = None):
        super().__init__(context, plugin_name, cn_name)
        # --- 修改: 更新日志说明 ---
        self.info("插件已加载 (主要功能已移至 DataManager 和 character_sync_plugin)。")
        # --- 修改结束 ---

    def register(self):
        """(此插件不再注册任何任务或监听器)"""
        self.debug("此插件不再注册后台定时任务或监听器。")
        pass


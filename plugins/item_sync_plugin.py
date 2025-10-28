import logging
import asyncio
import random
import json
from datetime import datetime, time
from plugins.base_plugin import BasePlugin, AppContext
from typing import Optional, Tuple
from core.context import get_global_context
# 移除常量导入

SYNC_FLAG_KEY_PREFIX = "item_sync_flag" # 防止每日重复同步的标志

_item_sync_task_logger = logging.getLogger("ItemSyncPlugin.Task")
# --- trigger_item_sync_update 函数负责触发更新 ---
async def trigger_item_sync_update(context: AppContext, force: bool = False) -> Tuple[bool, int, str]:
    """触发 GameDataManager 更新物品主数据缓存"""
    logger = _item_sync_task_logger
    # 检查核心服务
    if not context or not context.data_manager or not context.redis or not context.telegram_client:
        msg = "内部错误: DataManager, Redis 或 TGClient 未准备就绪"
        logger.error(f"【物品同步触发器】失败：{msg}")
        return False, 0, msg

    redis_client = context.redis.get_client()
    my_id = context.telegram_client._my_id # 用于检查标志

    # 检查每日同步标志
    sync_flag_key = None
    if my_id and not force:
        today_str = datetime.now().strftime("%Y-%m-%d")
        sync_flag_key = f"{SYNC_FLAG_KEY_PREFIX}:{my_id}:{today_str}"
        try:
            if redis_client:
                already_synced = await redis_client.exists(sync_flag_key)
                if already_synced:
                    msg = f"用户 {my_id} 今天 ({today_str}) 已同步过物品信息，跳过本次触发。"
                    logger.info(f"【物品同步触发器】{msg}")
                    return True, -1, msg # 跳过
            else: logger.warning("【物品同步触发器】无法检查同步标志：Redis 未连接。继续尝试触发。")
        except Exception as e: logger.error(f"【物品同步触发器】检查同步标志时出错: {e}", exc_info=True)
    elif force: logger.info("【物品同步触发器】强制同步模式，跳过每日同步检查。")
    elif not my_id and not force:
        msg = "自动同步任务跳过：缺少用户 ID 无法检查或设置同步标志。"
        logger.warning(f"【物品同步触发器】{msg}"); return True, -1, msg

    logger.info("【物品同步触发器】请求 DataManager 更新全局物品主数据缓存...")
    try:
        # 调用 DataManager 的更新方法
        success = await context.data_manager.update_item_master_cache()
        if success:
            logger.info("【物品同步触发器】DataManager 缓存更新成功。")
            # 设置成功标志
            if sync_flag_key and redis_client:
                 await redis_client.set(sync_flag_key, "1", ex=25 * 3600)
                 logger.info(f"【物品同步触发器】设置用户 {my_id} 今日同步成功标志: {sync_flag_key}")
            return True, 0, "✅ DataManager 物品主数据缓存更新已触发。" # 数量意义不大
        else:
            logger.error("【物品同步触发器】DataManager 缓存更新失败。")
            return False, 0, "❌ DataManager 物品主数据缓存更新失败。"
    except Exception as e:
        msg = f"调用 DataManager 更新物品缓存时发生意外错误: {e}"
        logger.error(f"【物品同步触发器】{msg}", exc_info=True)
        return False, 0, msg

# --- 定时任务函数 ---
_item_sync_scheduled_logger = logging.getLogger("ItemSyncPlugin.Scheduled")
async def _scheduled_sync_items_task():
    """定时任务，触发物品主数据缓存更新"""
    logger = _item_sync_scheduled_logger
    logger.info("【物品同步】[定时任务] 触发: 开始触发 DataManager 更新缓存...")
    try:
        context = get_global_context()
        if not context:
            logger.error("【物品同步】[定时任务] 失败：全局 AppContext 未初始化。")
            return
        success, count, msg = await trigger_item_sync_update(context=context, force=False) # 定时任务非强制
        if count == -1: logger.info(f"【物品同步】[定时任务] 跳过: {msg}")
        elif success: logger.info(f"【物品同步】[定时任务] 触发成功。")
        else: logger.error(f"【物品同步】[定时任务] 触发失败: {msg}")
    except Exception as e: logger.error(f"【物品同步】[定时任务] 执行时发生意外错误: {e}", exc_info=True)

# --- Plugin Class ---
class Plugin(BasePlugin):
    def __init__(self, context: AppContext, plugin_name: str, cn_name: str | None = None):
        super().__init__(context, plugin_name, cn_name)
        self.info("插件已加载。")

    def register(self):
        try:
            if self.scheduler:
                self.scheduler.add_job(
                    _scheduled_sync_items_task, # 调度目标不变
                    trigger='cron', hour=0, minute=0, second=0,
                    jitter=3600, id='sync_items_job', replace_existing=True, misfire_grace_time=600
                )
                self.info("已注册每日随机时间 (本地时间 00:00-01:00) 的物品同步任务。")
            else:
                 self.error("无法注册定时任务：Scheduler 不可用。")
        except Exception as e:
             self.error(f"注册 'sync_items_job' 任务失败: {e}", exc_info=True)

# 导出新的触发函数名
__all__ = ['trigger_item_sync_update']


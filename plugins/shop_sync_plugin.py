import logging
import asyncio
import json
from datetime import datetime
from plugins.base_plugin import BasePlugin, AppContext
from typing import Optional, Tuple
from core.context import get_global_context
# 移除常量导入

_shop_sync_task_logger = logging.getLogger("ShopSyncPlugin.Task")
# --- trigger_shop_sync_update 函数负责触发更新 ---
async def trigger_shop_sync_update(context: Optional[AppContext] = None, force: bool = False) -> Tuple[bool, int, str]:
    """触发 GameDataManager 更新指定用户的商店缓存"""
    logger = _shop_sync_task_logger
    local_context = context
    # 检查核心服务
    if not local_context or not local_context.data_manager or not local_context.telegram_client:
        msg = "内部错误: DataManager 或 TGClient 未准备就绪"
        logger.error(f"【商店同步触发器】失败：{msg}")
        return False, 0, msg

    # 获取用户 ID
    my_id = None
    try:
        my_id = await local_context.telegram_client.get_my_id()
        if not my_id: raise ValueError("无法获取 User ID")
    except Exception as e:
        msg = f"无法获取助手 User ID: {e}"
        logger.error(f"【商店同步触发器】失败：{msg}")
        return False, 0, msg

    # force 参数在此处意义不大，因为商店通常基于用户 ID，每日同步一次即可
    # 但保留参数以防未来需要

    logger.info(f"【商店同步触发器】请求 DataManager 更新用户 {my_id} 的商店缓存...")
    try:
        # 调用 DataManager 的更新方法
        success = await local_context.data_manager.update_shop_cache(my_id)
        if success:
            logger.info(f"【商店同步触发器】DataManager 商店缓存更新成功 (用户: {my_id})。")
            return True, 0, f"✅ DataManager 商店缓存更新已触发 (用户: {my_id})。" # 数量无意义
        else:
            logger.error(f"【商店同步触发器】DataManager 商店缓存更新失败 (用户: {my_id})。")
            return False, 0, f"❌ DataManager 商店缓存更新失败 (用户: {my_id})。"
    except Exception as e:
        msg = f"调用 DataManager 更新商店缓存时发生意外错误: {e}"
        logger.error(f"【商店同步触发器】{msg}", exc_info=True)
        return False, 0, msg

# --- 定时任务函数 ---
_shop_sync_scheduled_logger = logging.getLogger("ShopSyncPlugin.Scheduled")
async def _scheduled_sync_shop_task():
    """定时任务，触发商店缓存更新"""
    logger = _shop_sync_scheduled_logger
    logger.info("【商店同步】[定时任务] 触发: 开始触发 DataManager 更新缓存...")
    context = get_global_context()
    if not context:
        logger.error("【商店同步】[定时任务] 失败：全局 AppContext 未初始化。")
        return
    try:
        success, _, msg = await trigger_shop_sync_update(context=context, force=False) # 定时非强制
        if success: logger.info(f"【商店同步】[定时任务] 触发成功。")
        else: logger.error(f"【商店同步】[定时任务] 触发失败: {msg}")
    except Exception as e:
        logger.error(f"【商店同步】[定时任务] 执行时发生意外错误: {e}", exc_info=True)

# --- Plugin Class ---
class Plugin(BasePlugin):
    def __init__(self, context: AppContext, plugin_name: str, cn_name: str | None = None):
        super().__init__(context, plugin_name, cn_name)
        self.info("插件已加载。")

    def register(self):
        try:
            if self.scheduler: # 确保 scheduler 可用
                self.scheduler.add_job(
                    _scheduled_sync_shop_task, # 调度目标不变
                    trigger='cron', hour=1, minute=30,
                    jitter=1800, id='sync_shop_items_job', replace_existing=True, misfire_grace_time=600
                )
                self.info("已注册每日随机时间 (本地时间 01:30-02:00) 的商店同步任务。")
            else:
                self.error("无法注册定时任务：Scheduler 不可用。")

            run_on_startup = self.config.get("sync_on_startup.shop", True)
            if run_on_startup:
                self.info("将在 TG 客户端启动后触发一次商店信息同步。")
                self.event_bus.on("telegram_client_started", self.run_startup_sync)
        except Exception as e:
             self.error(f"注册 'sync_shop_items_job' 任务失败: {e}", exc_info=True)

    async def run_startup_sync(self):
        self.info("TG 客户端已启动，触发启动时商店同步...")
        await asyncio.sleep(10) # 延迟执行
        success, _, msg = await trigger_shop_sync_update(context=self.context, force=False)
        if not success:
             self.warning(f"启动时商店同步触发失败: {msg}")

# 导出新的触发函数名
__all__ = ['trigger_shop_sync_update']


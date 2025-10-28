import logging
import asyncio
import json
from datetime import datetime, timedelta, timezone
import pytz
from collections import defaultdict
# 移除导入 Redis Key 常量
# 移除导入 item_sync 触发器
from plugins.base_plugin import BasePlugin, AppContext
from core.context import get_global_context

_char_sync_time_logger = logging.getLogger("CharSync.TimeParse")
# parse_iso_datetime 和 format_local_time 函数保持不变
def parse_iso_datetime(dt_str: str | None) -> datetime | None:
    if not dt_str: return None
    try:
        dt_naive_str = dt_str.split('+')[0].split('Z')[0]
        parts = dt_naive_str.split('.')
        if len(parts) > 1:
             time_part = parts[0]; micro_part = parts[1][:6]
             if 'T' not in time_part: raise ValueError("Invalid ISO format without T separator")
             dt_naive_str = time_part + '.' + micro_part
             dt_naive = datetime.fromisoformat(dt_naive_str)
        else:
             if 'T' not in dt_naive_str: raise ValueError("Invalid ISO format without T separator")
             dt_naive = datetime.fromisoformat(dt_naive_str)
        return pytz.utc.localize(dt_naive)
    except Exception as e:
        _char_sync_time_logger.warning(f"无法解析时间字符串 '{dt_str}': {e}")
        return None

def format_local_time(dt: datetime | None) -> str | None:
    if dt is None: return None
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None: dt = pytz.utc.localize(dt)
    local_dt = dt.astimezone()
    return local_dt.strftime("%Y-%m-%d %H:%M:%S %Z%z")

# --- trigger_character_sync 函数负责触发更新 ---
_char_sync_fetch_logger = logging.getLogger("CharSync.Trigger") # 修改 logger 名称
async def trigger_character_sync(context: AppContext) -> tuple[bool, str]:
    """
    触发 GameDataManager 更新角色/背包缓存。
    返回操作是否成功启动。
    """
    logger = _char_sync_fetch_logger
    # 检查核心服务
    if not context or not context.data_manager or not context.telegram_client:
        msg = "内部错误: 核心服务 (DataManager/TGClient) 未准备就绪"
        logger.error(f"【角色/背包同步触发器】失败：{msg}")
        return False, msg

    # 获取用户名和 ID
    username = None; user_id = None
    try:
        user_id = await context.telegram_client.get_my_id()
        username = await context.telegram_client.get_my_username()
        if not user_id or not username: raise ValueError("无法获取 User ID 或 Username")
    except Exception as e:
        msg = f"无法获取助手 ID/用户名: {e}"
        logger.error(f"【角色/背包同步触发器】失败：{msg}")
        return False, msg

    logger.info(f"【角色/背包同步触发器】请求 DataManager 更新用户 {user_id} ({username}) 的缓存...")
    try:
        # 调用 DataManager 的更新方法
        success = await context.data_manager.update_cache_from_api(user_id, username)
        if success:
            logger.info(f"【角色/背包同步触发器】DataManager 缓存更新成功。")
            return True, f"✅ 缓存更新成功 (用户: {username})"
        else:
            logger.error(f"【角色/背包同步触发器】DataManager 缓存更新失败。")
            return False, f"❌ DataManager 缓存更新失败 (用户: {username})"
    except Exception as e:
        msg = f"调用 DataManager 更新缓存时发生意外错误: {e}"
        logger.error(f"【角色/背包同步触发器】{msg}", exc_info=True)
        return False, msg

# --- 定时任务函数 ---
_char_sync_task_logger = logging.getLogger("CharSync.Task")
async def sync_character_task():
    """由 APScheduler 调度的函数，触发统一的角色和背包缓存更新"""
    logger = _char_sync_task_logger
    try:
        context = get_global_context()
        if not context:
            logger.error("【角色/背包同步】[定时任务] 失败：无 AppContext。")
            return
        logger.info("【角色/背包同步】[定时任务] 开始触发 DataManager 更新缓存...")
        success, message = await trigger_character_sync(context) # 调用触发函数
        if success: logger.info(f"【角色/背包同步】[定时任务] 触发成功。")
        else: logger.error(f"【角色/背包同步】[定时任务] 触发失败: {message}")
    except Exception as e:
        logger.error(f"【角色/背包同步】[定时任务] 执行时发生未捕获错误: {e}", exc_info=True)

# --- Plugin Class ---
class Plugin(BasePlugin):
    def __init__(self, context: AppContext, plugin_name: str, cn_name: str | None = None):
        super().__init__(context, plugin_name, cn_name)
        self.info("插件已加载。")

    def register(self):
        sync_interval_minutes = self.config.get("sync_intervals.character", 5)
        if not isinstance(sync_interval_minutes, int) or sync_interval_minutes < 1:
             self.warning(f"配置的角色同步间隔 '{sync_interval_minutes}' 无效，将使用默认值 5 分钟。")
             sync_interval_minutes = 5

        self.info(f"将每 {sync_interval_minutes} 分钟触发一次角色和背包缓存更新。")
        try:
            if self.scheduler: # 确保 scheduler 可用
                self.scheduler.add_job(
                    sync_character_task,
                    trigger='interval',
                    minutes=sync_interval_minutes,
                    id='sync_character_job',
                    replace_existing=True,
                    misfire_grace_time=60
                )
                self.info("已注册 'sync_character_job' (触发缓存更新) 定时任务。")
            else:
                 self.error("无法注册定时任务：Scheduler 不可用。")


            run_on_startup = self.config.get("sync_on_startup.character", True)
            if run_on_startup:
                self.info("将在 TG 客户端启动后触发一次角色和背包缓存更新。")
                self.event_bus.on("telegram_client_started", self.run_startup_sync)

            self.event_bus.on("trigger_character_sync_now", self.run_sync_now)
            self.info("已注册 'trigger_character_sync_now' 事件监听器。")

        except Exception as e:
             self.error(f"注册角色/背包同步任务或监听器时出错: {e}", exc_info=True)

    async def run_startup_sync(self):
        """TG 客户端启动后触发一次同步"""
        self.info("TG 客户端已启动，触发启动时角色/背包缓存更新...")
        await asyncio.sleep(1) # 短暂延迟
        success, message = await trigger_character_sync(self.context)
        if success: self.info("启动时角色/背包缓存更新触发成功。")
        else: self.warning(f"启动时角色/背包缓存更新触发失败: {message}")

    async def run_sync_now(self):
        """响应事件，立即触发一次角色和背包同步"""
        self.info("【角色/背包同步】收到事件触发，立即触发缓存更新...")
        success, message = await trigger_character_sync(self.context)
        if success: self.info("【角色/背包同步】事件触发的缓存更新成功。")
        else: self.error(f"【角色/背包同步】事件触发的缓存更新失败: {message}")


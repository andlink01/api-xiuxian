import logging
import pytz
from datetime import datetime, timedelta
from typing import Optional, Tuple # 增加 Tuple 导入
from plugins.base_plugin import BasePlugin, AppContext
from core.context import get_global_context
from apscheduler.jobstores.base import JobLookupError
import asyncio
import random

logger = logging.getLogger(__name__)
time_parse_logger = logging.getLogger("CharSync.TimeParse")
schedule_logger = logging.getLogger("CharSync.Scheduler")
trigger_logger = logging.getLogger("CharSync.Trigger")
task_logger = logging.getLogger("CharSync.Task")

CHARACTER_SYNC_JOB_ID = 'character_sync_job'

# --- 时间处理函数 ---
def parse_iso_datetime(dt_str: Optional[str]) -> Optional[datetime]:
    """
    解析 ISO 8601 格式的时间字符串 (兼容纯日期 YYYY-MM-DD)。
    返回 timezone-aware 的 datetime 对象 (UTC)。
    """
    if not dt_str or not isinstance(dt_str, str):
        return None
    try:
        # 移除可能存在的毫秒后的多余数字 (最多保留6位)
        if '.' in dt_str:
            parts = dt_str.split('.')
            if len(parts) == 2:
                ms_part = parts[1].split('+')[0].split('-')[0].split('Z')[0]
                if len(ms_part) > 6:
                    dt_str = parts[0] + '.' + ms_part[:6] + dt_str[len(parts[0]) + 1 + len(ms_part):]

        # 尝试解析完整的 ISO datetime 格式
        parsed_dt = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
        # 确保返回的是 aware datetime (通常 fromisoformat 会处理好)
        if parsed_dt.tzinfo is None:
            # 如果解析结果是 naive，假设它是 UTC (虽然不太可能发生)
            return parsed_dt.replace(tzinfo=pytz.utc)
        return parsed_dt.astimezone(pytz.utc) # 统一转换为 UTC
    except ValueError:
        # --- 新增: 尝试解析纯日期格式 ---
        try:
            parsed_date = datetime.strptime(dt_str, '%Y-%m-%d')
            # 将日期转换为当天 UTC 时间的开始 (00:00:00)
            return parsed_date.replace(tzinfo=pytz.utc)
        except ValueError:
            time_parse_logger.warning(f"无法解析时间字符串 '{dt_str}': Invalid ISO format or YYYY-MM-DD format")
            return None
        # --- 新增结束 ---
    except Exception as e:
        time_parse_logger.error(f"解析时间字符串 '{dt_str}' 时发生未知错误: {e}", exc_info=True)
        return None

def format_local_time(dt_aware: Optional[datetime], fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """将 timezone-aware 的 datetime 对象格式化为本地时间字符串"""
    if not dt_aware or not isinstance(dt_aware, datetime) or dt_aware.tzinfo is None:
        return "未知时间"
    try:
        context = get_global_context()
        local_tz_str = context.config.get("system.timezone", "Asia/Shanghai") if context else "Asia/Shanghai"
        local_tz = pytz.timezone(local_tz_str)
        local_dt = dt_aware.astimezone(local_tz)
        return local_dt.strftime(fmt)
    except Exception as e:
        time_parse_logger.error(f"格式化本地时间失败: {e}")
        # Fallback to UTC display if local formatting fails
        try: return dt_aware.strftime(fmt + " (UTC)")
        except: return "时间格式化错误"
# --- 时间处理函数结束 ---

async def trigger_character_sync(context: AppContext, user_id: int, username: str) -> Tuple[bool, str]:
    """触发 DataManager 更新指定用户的缓存"""
    trigger_logger.info(f"【角色/背包同步触发器】请求 DataManager 更新用户 {user_id} ({username}) 的缓存...")
    if not context.data_manager:
        trigger_logger.error("【角色/背包同步触发器】DataManager 不可用！")
        return False, "❌ DataManager 不可用"
    try:
        success = await context.data_manager.update_cache_from_api(user_id, username)
        if success:
            trigger_logger.info(f"【角色/背包同步触发器】DataManager 缓存更新成功 (用户: {username})。")
            return True, f"✅ 角色/背包缓存更新成功 (用户: {username})"
        else:
            trigger_logger.error("【角色/背包同步触发器】DataManager 缓存更新失败。")
            return False, f"❌ DataManager 缓存更新失败 (用户: {username})"
    except Exception as e:
        trigger_logger.error(f"【角色/背包同步触发器】调用 DataManager 更新时发生异常: {e}", exc_info=True)
        return False, f"❌ 调用 DataManager 更新时异常 (用户: {username})"

async def _character_sync_task():
    """由 APScheduler 调度的函数，用于触发角色/背包同步"""
    task_logger.info("【角色/背包同步】[定时任务] 任务启动...")
    context = get_global_context()
    if not context or not context.telegram_client or not context.config:
        task_logger.error("【角色/背包同步】[定时任务] 无法获取核心服务，任务终止。")
        return

    sync_enabled = context.config.get("sync_on_startup.character", True) # 复用启动开关
    if not sync_enabled:
        task_logger.info("【角色/背包同步】[定时任务] 功能未启用 (sync_on_startup.character: false)，任务跳过。")
        return

    user_id = context.telegram_client._my_id
    username = context.telegram_client._my_username
    if not user_id or not username:
        task_logger.error("【角色/背包同步】[定时任务] 无法获取 User ID 或 Username，任务终止。")
        return

    success, message = await trigger_character_sync(context, user_id, username)
    if not success:
        task_logger.error(f"【角色/背包同步】[定时任务] 触发失败: {message}")
        await context.event_bus.emit("send_system_notification", f"⚠️ **角色/背包定时同步失败** ⚠️\n\n原因: {message}")


# --- 插件类 (保持不变) ---
class Plugin(BasePlugin):
    """
    负责定时触发角色和背包数据同步的插件。
    也提供其他插件需要的时间处理函数。
    """
    def __init__(self, context: AppContext, plugin_name: str, cn_name: str | None = None):
        super().__init__(context, plugin_name, cn_name)
        self.load_config()
        if self.sync_enabled: self.info(f"插件已加载。角色/背包将每隔 {self.sync_interval_minutes} 分钟自动同步一次。")
        else: self.info("插件已加载，但定时同步功能未启用 (sync_intervals.character <= 0)。")

    def load_config(self):
        """加载配置"""
        self.sync_interval_minutes = self.config.get("sync_intervals.character", 5)
        self.sync_enabled = self.sync_interval_minutes > 0

    def register(self):
        """注册定时任务和手动触发事件"""
        self.debug("register() 方法被调用。")
        if self.sync_enabled and self.scheduler:
            try:
                self.scheduler.add_job(
                    _character_sync_task, trigger='interval', minutes=self.sync_interval_minutes,
                    id=CHARACTER_SYNC_JOB_ID, replace_existing=True, misfire_grace_time=120
                )
                self.info(f"已注册角色/背包定时同步任务 (每 {self.sync_interval_minutes} 分钟)。")
            except Exception as e:
                self.error(f"注册角色/背包定时同步任务失败: {e}", exc_info=True)
        elif self.sync_enabled:
            self.error("无法注册角色/背包定时同步任务：Scheduler 不可用。")

        # 注册手动触发事件
        self.event_bus.on("trigger_character_sync_now", self.handle_trigger_now)
        self.info("已注册 'trigger_character_sync_now' 事件监听器。")

    async def handle_trigger_now(self):
        """处理手动立即触发同步的事件"""
        self.info("【角色/背包同步】收到立即同步请求...")
        user_id = self.context.telegram_client._my_id
        username = self.context.telegram_client._my_username
        if not user_id or not username:
            self.error("【角色/背包同步】无法获取 User ID 或 Username，无法执行立即同步。")
            await self.context.event_bus.emit("send_system_notification", "❌ 无法执行角色/背包立即同步：缺少用户信息。")
            return

        success, message = await trigger_character_sync(self.context, user_id, username)
        # 手动触发时，将结果通知给管理员
        try:
             await self.context.event_bus.emit("send_system_notification", f"手动触发角色/背包同步结果:\n{message}")
        except Exception as e:
             self.error(f"发送手动同步结果通知失败: {e}")


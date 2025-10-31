import logging
import asyncio
import random
import pytz
from datetime import datetime, timedelta
from typing import Optional, List
from plugins.base_plugin import BasePlugin, AppContext
from core.context import get_global_context
from apscheduler.jobstores.base import JobLookupError

# --- 常量 ---
AUTO_DUEL_JOB_ID = 'auto_duel_job'
REDIS_NEXT_DUEL_TARGET_INDEX_KEY_FORMAT = "auto_duel:next_target_index:{}" # 存储下一个目标索引的 Key
DUEL_COMMAND_FORMAT = ".斗法 {}" # 斗法指令

async def _execute_auto_duel_task():
    """
    由 APScheduler 调度的函数，用于执行一次斗法指令。
    """
    logger = logging.getLogger("AutoDuelPlugin.Task")
    context = get_global_context()

    if not context or not context.config or not context.redis or not context.telegram_client:
        logger.error("【自动斗法】无法执行：核心服务不可用。")
        return

    config = context.config
    auto_enabled = config.get("auto_duel.enabled", False)
    if not auto_enabled:
        logger.debug("【自动斗法】未启用，跳过。")
        return

    targets: List[str] = config.get("auto_duel.targets", [])
    if not targets:
        logger.debug("【自动斗法】未配置斗法目标，跳过。")
        return

    redis_client = context.redis.get_client()
    my_id = context.telegram_client._my_id

    if not my_id or not redis_client:
        logger.error("【自动斗法】无法获取 User ID 或 Redis 客户端，跳过。")
        return

    index_key = REDIS_NEXT_DUEL_TARGET_INDEX_KEY_FORMAT.format(my_id)
    current_index = 0
    try:
        current_index_str = await redis_client.get(index_key)
        if current_index_str and current_index_str.isdigit():
            current_index = int(current_index_str)
        else:
            logger.info(f"【自动斗法】未找到下一个目标索引 (Key: {index_key})，从 0 开始。")
            current_index = 0
    except Exception as e:
        logger.error(f"【自动斗法】从 Redis (Key: {index_key}) 获取目标索引失败: {e}，将从 0 开始。")
        current_index = 0

    # 确保索引在目标列表范围内
    target_index = current_index % len(targets)
    target_username = targets[target_index]

    command_to_send = DUEL_COMMAND_FORMAT.format(target_username)

    try:
        logger.info(f"【自动斗法】({target_index + 1}/{len(targets)}) 正在将指令 '{command_to_send}' 加入发送队列...")
        success = await context.telegram_client.send_game_command(command_to_send)
        
        if success:
            logger.info(f"【自动斗法】指令 '{command_to_send}' 已成功加入队列。")
            # 更新下一个索引
            next_index = (target_index + 1) % len(targets)
            await redis_client.set(index_key, str(next_index))
        else:
            logger.error(f"【自动斗法】将指令 '{command_to_send}' 加入队列失败。")

    except Exception as e:
        logger.error(f"【自动斗法】发送指令 '{command_to_send}' 时出错: {e}", exc_info=True)


class Plugin(BasePlugin):
    """
    自动斗法插件。
    根据配置列表循环发送斗法指令。
    """
    def __init__(self, context: AppContext, plugin_name: str, cn_name: str | None = None):
        super().__init__(context, plugin_name, cn_name)
        self.load_config()
        if self.auto_enabled:
            self.info(f"插件已加载并启用。间隔: {self.interval_seconds} 秒。")
        else:
            self.info("插件已加载但未启用。")

    def load_config(self):
        self.auto_enabled = self.config.get("auto_duel.enabled", False)
        self.interval_seconds = self.config.get("auto_duel.interval_seconds", 305) # 默认 5分5秒
        self.targets = self.config.get("auto_duel.targets", [])

    def register(self):
        """注册定时任务"""
        if not self.auto_enabled:
            return

        if self.interval_seconds < 60:
            self.warning(f"自动斗法间隔 ({self.interval_seconds}秒) 过短，可能导致问题。建议至少 60 秒。")
        
        if not self.targets:
            self.warning("自动斗法已启用，但未配置任何斗法目标 (auto_duel.targets)。")
            # 即使没有目标，也注册任务，以便在配置更新后能自动开始
            
        try:
            if self.scheduler:
                self.scheduler.add_job(
                    _execute_auto_duel_task,
                    trigger='interval',
                    seconds=self.interval_seconds,
                    id=AUTO_DUEL_JOB_ID,
                    replace_existing=True,
                    misfire_grace_time=60 # 允许 1 分钟的偏差
                )
                self.info(f"已注册自动斗法任务 (每 {self.interval_seconds} 秒)。")
            else:
                 self.error("无法注册自动斗法任务：Scheduler 不可用。")
        except Exception as e:
            self.error(f"注册自动斗法定时任务时出错: {e}", exc_info=True)

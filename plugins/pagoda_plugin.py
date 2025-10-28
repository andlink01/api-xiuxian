import logging
import asyncio
import json
import pytz
import random
from datetime import datetime, timedelta, time
from typing import Optional
from plugins.base_plugin import BasePlugin, AppContext
from core.context import get_global_context
from apscheduler.jobstores.base import JobLookupError

# --- 常量 ---
PAGODA_JOB_ID = 'auto_pagoda_job'
PAGODA_COMMAND = ".闯塔"
REDIS_LAST_RUN_KEY = "daily_task_last_run:pagoda"
RETRY_DELAY_MINUTES_CONFIG_KEY = "pagoda.retry_delay_minutes"
DEFAULT_RETRY_DELAY_MINUTES = 60
NEXT_DAY_SCHEDULE_HOUR_START = 1
NEXT_DAY_SCHEDULE_MINUTE_START = 15
NEXT_DAY_SCHEDULE_JITTER_SECONDS = 30 * 60

async def _get_local_timezone(config) -> pytz.BaseTzInfo:
    """获取配置的本地时区，默认为上海"""
    try:
        local_tz_str = config.get("system.timezone", "Asia/Shanghai")
        return pytz.timezone(local_tz_str)
    except pytz.UnknownTimeZoneError:
        logging.getLogger("PagodaPlugin.Utils").error(f"无效的时区配置: {local_tz_str}，回退到 Asia/Shanghai")
        return pytz.timezone("Asia/Shanghai")

async def _schedule_next_day_run(context: AppContext):
    """安排任务在次日凌晨随机时间执行"""
    logger = logging.getLogger("PagodaPlugin.Scheduler")
    if not context or not context.scheduler:
        logger.error("【自动闯塔】无法安排下次任务：Scheduler 不可用。")
        return
    try:
        local_tz = await _get_local_timezone(context.config)
        now_local = datetime.now(local_tz)
        tomorrow = now_local + timedelta(days=1)
        target_time = tomorrow.replace(hour=NEXT_DAY_SCHEDULE_HOUR_START, minute=NEXT_DAY_SCHEDULE_MINUTE_START, second=0, microsecond=0)
        jitter = random.uniform(0, NEXT_DAY_SCHEDULE_JITTER_SECONDS)
        next_run_time = target_time + timedelta(seconds=jitter)
        context.scheduler.add_job(
            _execute_pagoda_and_reschedule,
            trigger='date', run_date=next_run_time, id=PAGODA_JOB_ID,
            replace_existing=True, misfire_grace_time=3600
        )
        logger.info(f"【自动闯塔】已成功安排下次执行时间: {next_run_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    except Exception as e:
        logger.error(f"【自动闯塔】安排下次任务失败: {e}", exc_info=True)

async def _schedule_retry(context: AppContext):
    """安排一个短期重试任务"""
    logger = logging.getLogger("PagodaPlugin.Scheduler")
    if not context or not context.scheduler:
        logger.error("【自动闯塔】无法安排重试：Scheduler 不可用。")
        return
    try:
        retry_minutes = context.config.get(RETRY_DELAY_MINUTES_CONFIG_KEY, DEFAULT_RETRY_DELAY_MINUTES)
        next_run_time = datetime.now(pytz.utc) + timedelta(minutes=retry_minutes)
        context.scheduler.add_job(
            _execute_pagoda_and_reschedule,
            trigger='date', run_date=next_run_time, id=PAGODA_JOB_ID,
            replace_existing=True, misfire_grace_time=600
        )
        local_tz = await _get_local_timezone(context.config)
        logger.info(f"【自动闯塔】执行失败或未完成，已安排在 {next_run_time.astimezone(local_tz).strftime('%Y-%m-%d %H:%M:%S %Z')} 重试。")
    except Exception as e:
        logger.error(f"【自动闯塔】安排重试任务失败: {e}", exc_info=True)


async def _execute_pagoda_and_reschedule():
    """
    由 APScheduler 调度的函数，执行闯塔、更新状态并重新调度。
    """
    logger = logging.getLogger("PagodaPlugin.Execute")
    logger.info("【自动闯塔】任务执行：开始检查是否需要执行闯塔...")

    context = get_global_context()
    if not context or not context.data_manager or not context.redis or not context.telegram_client or not context.event_bus: # 确保 event_bus 可用
        logger.error("【自动闯塔】无法执行：核心服务不可用。")
        return

    config = context.config; event_bus = context.event_bus # 获取 event_bus
    auto_enabled = config.get("pagoda.auto_enabled", False)
    if not auto_enabled:
        logger.info("【自动闯塔】已被禁用，任务终止。")
        return

    redis_client = context.redis.get_client()
    my_id = context.telegram_client._my_id
    my_username = context.telegram_client._my_username # 获取用户名
    if not my_id:
        logger.error("【自动闯塔】无法获取助手 User ID，任务终止。")
        return

    local_tz = await _get_local_timezone(config)
    current_date_str = datetime.now(local_tz).strftime("%Y-%m-%d")
    logger.info(f"【自动闯塔】当前日期 (本地时间): {current_date_str}")

    last_attempt_date_str = None
    try:
        logger.info("【自动闯塔】正在通过 DataManager 强制刷新闯塔缓存数据...")
        # 强制刷新获取最新闯塔日期
        pagoda_cache_data = await context.data_manager.get_pagoda_progress(my_id, use_cache=False)

        if isinstance(pagoda_cache_data, dict):
             pagoda_progress_data = pagoda_cache_data.get("progress")
             if isinstance(pagoda_progress_data, dict):
                 last_attempt_date_str = pagoda_progress_data.get("last_attempt_date")
                 logger.info(f"【自动闯塔】获取到最新上次闯塔日期: {last_attempt_date_str}")
             elif pagoda_progress_data is None: logger.info("【自动闯塔】API 返回的闯塔进度 (progress) 为 null/None。")
             else: logger.warning(f"【自动闯塔】API 返回的闯塔进度 (progress) 格式不正确。")
        else:
            logger.error("【自动闯塔】无法从 DataManager 获取闯塔数据，安排重试。")
            await _schedule_retry(context)
            return
    except Exception as e:
        logger.error(f"【自动闯塔】获取闯塔数据时出错: {e}", exc_info=True)
        await _schedule_retry(context)
        return

    needs_pagoda = False
    if not last_attempt_date_str:
        logger.info("【自动闯塔】API 显示从未闯塔或记录丢失，需要执行。")
        needs_pagoda = True
    elif last_attempt_date_str != current_date_str:
        logger.info(f"【自动闯塔】上次闯塔日期 ({last_attempt_date_str}) 不是今天 ({current_date_str})，需要执行。")
        needs_pagoda = True
    else:
        logger.info("【自动闯塔】根据最新 API 数据，今天已经闯过塔了。")
        needs_pagoda = False

    if needs_pagoda:
        try:
            logger.info(f"【自动闯塔】正在将指令 '{PAGODA_COMMAND}' 加入发送队列...")
            success = await context.telegram_client.send_game_command(PAGODA_COMMAND)
            if success:
                logger.info(f"【自动闯塔】指令 '{PAGODA_COMMAND}' 已成功加入发送队列。")
                logger.info("【自动闯塔】触发角色数据同步...")
                try:
                    await event_bus.emit("trigger_character_sync_now")
                except Exception as sync_e:
                    logger.error(f"【自动闯塔】尝试在发送指令后触发角色同步时出错: {sync_e}", exc_info=True)

                # --- 新增：发送成功通知 ---
                notify_msg = f"✅ [{my_username or my_id}] 今日自动闯塔指令已发送。"
                try:
                    await event_bus.emit("send_system_notification", notify_msg)
                except Exception as notify_e:
                    logger.error(f"【自动闯塔】发送完成通知失败: {notify_e}")
                # --- 新增结束 ---

                if redis_client:
                    try:
                        await redis_client.set(REDIS_LAST_RUN_KEY, current_date_str, ex=timedelta(days=2))
                        logger.info(f"【自动闯塔】已在 Redis 记录今天 ({current_date_str}) 的运行。")
                    except Exception as e_redis:
                        logger.warning(f"【自动闯塔】在 Redis 记录上次运行时出错: {e_redis}")

                await _schedule_next_day_run(context)
            else:
                 logger.error("【自动闯塔】将闯塔指令加入队列失败。安排重试。")
                 await _schedule_retry(context)
        except Exception as e_send:
            logger.error(f"【自动闯塔】将闯塔指令加入队列时出错: {e_send}", exc_info=True)
            await _schedule_retry(context)
    else:
        # 今天已完成的情况
        logger.info("【自动闯塔】今天已完成，安排明天任务。")
        # --- 新增：仅在首次检测到完成时发送通知 ---
        send_completion_notify = False
        if redis_client:
             try:
                 last_recorded_run = await redis_client.get(REDIS_LAST_RUN_KEY)
                 if last_recorded_run != current_date_str:
                     send_completion_notify = True
                     await redis_client.set(REDIS_LAST_RUN_KEY, current_date_str, ex=timedelta(days=2))
                     logger.info(f"【自动闯塔】首次检测到今天 ({current_date_str}) 已完成，记录到 Redis。")
                 else:
                     logger.info(f"【自动闯塔】今天 ({current_date_str}) 已完成状态已记录，无需重复通知。")
             except Exception as e_redis_check:
                 logger.warning(f"【自动闯塔】在 Redis 检查或记录已完成时出错: {e_redis_check}")
                 send_completion_notify = True # 出错时也尝试发送
        else:
            send_completion_notify = True # Redis 不可用时也发送

        if send_completion_notify:
            notify_msg = f"✅ [{my_username or my_id}] 今日自动闯塔已完成。"
            try:
                await event_bus.emit("send_system_notification", notify_msg)
            except Exception as notify_e:
                logger.error(f"【自动闯塔】发送完成通知失败: {notify_e}")
        # --- 新增结束 ---
        await _schedule_next_day_run(context)

# --- 插件类 (逻辑不变) ---
class Plugin(BasePlugin):
    """自动闯塔插件 (智能调度版)"""
    def __init__(self, context: AppContext, plugin_name: str, cn_name: str | None = None):
        super().__init__(context, plugin_name, cn_name)
        self.load_config()
        if self.auto_enabled: self.info(f"插件已加载并启用。采用智能调度。指令: '{PAGODA_COMMAND}'")
        else: self.info("插件已加载但未启用。")

    def load_config(self):
        self.auto_enabled = self.config.get("pagoda.auto_enabled", False)

    def register(self):
        if not self.auto_enabled: return
        self.event_bus.on("telegram_client_started", self.initial_check_and_schedule)
        self.info(f"已注册 'telegram_client_started' 监听器，用于启动时检查闯塔任务。")

    async def initial_check_and_schedule(self):
        """应用启动时检查任务状态"""
        # ... (启动检查逻辑保持不变) ...
        self.info("【自动闯塔】TG 客户端已启动，开始启动时检查任务状态...")
        await asyncio.sleep(random.uniform(10, 15)) # 随机延迟启动检查

        context = self.context
        if not context or not context.scheduler or not context.data_manager or not context.redis or not context.telegram_client: # 确保 telegram_client 可用
             self.error("【自动闯塔】启动检查失败：核心服务不可用。")
             return

        my_id = context.telegram_client._my_id
        if not my_id: self.error("【自动闯塔】启动检查失败：无法获取 User ID。"); return

        try:
            job = None
            try: job = context.scheduler.get_job(PAGODA_JOB_ID)
            except JobLookupError as e: self.warning(f"【自动闯塔】启动检查：无法加载旧任务 (LookupError): {e}。将视为空任务。"); job = None

            local_tz = await _get_local_timezone(context.config); today_date_str = datetime.now(local_tz).strftime("%Y-%m-%d")

            redis_client = context.redis.get_client(); last_run_date_redis = None
            if redis_client:
                 try: last_run_date_redis = await redis_client.get(REDIS_LAST_RUN_KEY)
                 except Exception as e_redis: self.warning(f"【自动闯塔】启动检查：读取 Redis LastRunKey 失败: {e_redis}")

            last_attempt_date = last_run_date_redis # 优先使用 Redis 记录

            if not last_attempt_date:
                self.info("【自动闯塔】启动检查：Redis 中无上次运行记录，检查 DataManager 缓存...")
                pagoda_cache = await context.data_manager.get_pagoda_progress(my_id, use_cache=True)
                if pagoda_cache and isinstance(pagoda_cache.get("progress"), dict):
                    last_attempt_date = pagoda_cache["progress"].get("last_attempt_date")

            self.info(f"【自动闯塔】启动检查：今天日期 '{today_date_str}', 上次完成日期 '{last_attempt_date}'")

            if last_attempt_date == today_date_str:
                self.info("【自动闯塔】启动检查：今天已完成。")
                if not job:
                    self.warning("【自动闯塔】启动检查：今天已完成，但未找到未来的调度任务！将安排明天任务。")
                    await _schedule_next_day_run(context)
                else: self.info(f"【自动闯塔】启动检查：已找到未来的调度任务: {job.next_run_time}")
            else: # 今天未完成
                self.info("【自动闯塔】启动检查：今天尚未完成。")
                if job:
                    job_next_run_local = job.next_run_time.astimezone(local_tz)
                    if job_next_run_local.strftime("%Y-%m-%d") == today_date_str:
                        self.info(f"【自动闯塔】启动检查：任务已安排在今天 {job_next_run_local.strftime('%H:%M:%S')} 执行。")
                    else:
                        self.warning(f"【自动闯塔】启动检查：任务已安排在 {job_next_run_local.strftime('%Y-%m-%d')} (不是今天)，将立即重新安排执行。")
                        context.scheduler.add_job(
                            _execute_pagoda_and_reschedule, trigger='date',
                            run_date=datetime.now(pytz.utc) + timedelta(seconds=10),
                            id=PAGODA_JOB_ID, replace_existing=True
                        )
                else: # 今天未完成，且没有任务
                    self.warning("【自动闯塔】启动检查：今天未完成且无任务安排！将立即安排执行。")
                    context.scheduler.add_job(
                        _execute_pagoda_and_reschedule, trigger='date',
                        run_date=datetime.now(pytz.utc) + timedelta(seconds=10),
                        id=PAGODA_JOB_ID, replace_existing=True
                    )
        except Exception as e_init:
            self.error(f"【自动闯塔】启动时检查任务状态失败: {e_init}", exc_info=True)
            try:
                 self.warning("【自动闯塔】因启动检查失败，尝试立即安排一次执行...")
                 context.scheduler.add_job(
                     _execute_pagoda_and_reschedule, trigger='date',
                     run_date=datetime.now(pytz.utc) + timedelta(seconds=15),
                     id=PAGODA_JOB_ID, replace_existing=True
                 )
            except Exception as e_retry: self.error(f"【自动闯塔】安排紧急重试任务失败: {e_retry}")


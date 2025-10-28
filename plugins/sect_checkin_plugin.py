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
CHECKIN_JOB_ID = 'auto_sect_checkin_job'
CHECKIN_COMMAND = ".宗门点卯"
# --- 修改: 每日运行标记 Key 包含 user_id 和 date 占位符 ---
REDIS_LAST_RUN_KEY_FORMAT = "daily_task_last_run:sect_checkin:{}:{}" # 格式: daily_task_last_run:sect_checkin:<user_id>:<YYYY-MM-DD>
# --- 修改结束 ---
RETRY_DELAY_MINUTES_CONFIG_KEY = "sect_checkin.retry_delay_minutes"
DEFAULT_RETRY_DELAY_MINUTES = 60
NEXT_DAY_SCHEDULE_HOUR_START = 0
NEXT_DAY_SCHEDULE_MINUTE_START = 5
NEXT_DAY_SCHEDULE_JITTER_SECONDS = 30 * 60

async def _get_local_timezone(config) -> pytz.BaseTzInfo:
    """获取配置的本地时区，默认为上海"""
    # ... (逻辑不变) ...
    try:
        local_tz_str = config.get("system.timezone", "Asia/Shanghai")
        return pytz.timezone(local_tz_str)
    except pytz.UnknownTimeZoneError:
        logging.getLogger("SectCheckinPlugin.Utils").error(f"无效的时区配置: {local_tz_str}，回退到 Asia/Shanghai")
        return pytz.timezone("Asia/Shanghai")

async def _schedule_next_day_run(context: AppContext):
    """安排任务在次日凌晨随机时间执行"""
    # ... (逻辑不变) ...
    logger = logging.getLogger("SectCheckinPlugin.Scheduler")
    if not context or not context.scheduler:
        logger.error("【自动点卯】无法安排下次任务：Scheduler 不可用。")
        return
    try:
        local_tz = await _get_local_timezone(context.config)
        now_local = datetime.now(local_tz)
        tomorrow = now_local + timedelta(days=1)
        target_time = tomorrow.replace(hour=NEXT_DAY_SCHEDULE_HOUR_START, minute=NEXT_DAY_SCHEDULE_MINUTE_START, second=0, microsecond=0)
        jitter = random.uniform(0, NEXT_DAY_SCHEDULE_JITTER_SECONDS)
        next_run_time = target_time + timedelta(seconds=jitter)
        context.scheduler.add_job(
            _execute_checkin_and_reschedule,
            trigger='date', run_date=next_run_time, id=CHECKIN_JOB_ID,
            replace_existing=True, misfire_grace_time=3600
        )
        logger.info(f"【自动点卯】已成功安排下次执行时间: {next_run_time.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    except Exception as e:
        logger.error(f"【自动点卯】安排下次任务失败: {e}", exc_info=True)

async def _schedule_retry(context: AppContext):
    """安排一个短期重试任务"""
    # ... (逻辑不变) ...
    logger = logging.getLogger("SectCheckinPlugin.Scheduler")
    if not context or not context.scheduler:
        logger.error("【自动点卯】无法安排重试：Scheduler 不可用。")
        return
    try:
        retry_minutes = context.config.get(RETRY_DELAY_MINUTES_CONFIG_KEY, DEFAULT_RETRY_DELAY_MINUTES)
        next_run_time = datetime.now(pytz.utc) + timedelta(minutes=retry_minutes)
        context.scheduler.add_job(
            _execute_checkin_and_reschedule,
            trigger='date', run_date=next_run_time, id=CHECKIN_JOB_ID,
            replace_existing=True, misfire_grace_time=600
        )
        local_tz = await _get_local_timezone(context.config)
        logger.info(f"【自动点卯】执行失败或未完成，已安排在 {next_run_time.astimezone(local_tz).strftime('%Y-%m-%d %H:%M:%S %Z')} 重试。")
    except Exception as e:
        logger.error(f"【自动点卯】安排重试任务失败: {e}", exc_info=True)


async def _execute_checkin_and_reschedule():
    """
    由 APScheduler 调度的函数，执行点卯、更新状态并重新调度。
    """
    logger = logging.getLogger("SectCheckinPlugin.Execute")
    logger.info("【自动点卯】任务执行：开始检查是否需要执行宗门点卯...")

    context = get_global_context()
    if not context or not context.data_manager or not context.redis or not context.telegram_client or not context.event_bus:
        logger.error("【自动点卯】无法执行：核心服务不可用。")
        return

    config = context.config; event_bus = context.event_bus
    auto_enabled = config.get("sect_checkin.auto_enabled", False)
    if not auto_enabled:
        logger.info("【自动点卯】已被禁用，任务终止。")
        return

    redis_client = context.redis.get_client()
    my_id = context.telegram_client._my_id
    my_username = context.telegram_client._my_username
    if not my_id:
        logger.error("【自动点卯】无法获取助手 User ID，任务终止。")
        return

    local_tz = await _get_local_timezone(config)
    current_date_str = datetime.now(local_tz).strftime("%Y-%m-%d")
    logger.info(f"【自动点卯】当前日期 (本地时间): {current_date_str}")

    last_checkin_date_str = None
    try:
        # ... (强制刷新宗门数据逻辑不变) ...
        logger.info("【自动点卯】正在通过 DataManager 强制刷新宗门缓存数据...")
        sect_data = await context.data_manager.get_sect_info(my_id, use_cache=False)
        if sect_data:
            last_checkin_date_str = sect_data.get("last_sect_check_in")
            logger.info(f"【自动点卯】获取到最新上次点卯日期: {last_checkin_date_str}")
        else:
            logger.error("【自动点卯】无法从 DataManager 获取宗门数据，安排重试。")
            await _schedule_retry(context)
            return
    except Exception as e:
        logger.error(f"【自动点卯】获取宗门数据时出错: {e}", exc_info=True)
        await _schedule_retry(context)
        return

    needs_checkin = False
    # ... (判断是否需要点卯逻辑不变) ...
    if not last_checkin_date_str:
        logger.info("【自动点卯】API 显示从未点卯或记录丢失，需要执行。")
        needs_checkin = True
    elif last_checkin_date_str != current_date_str:
        logger.info(f"【自动点卯】上次点卯日期 ({last_checkin_date_str}) 不是今天 ({current_date_str})，需要执行。")
        needs_checkin = True
    else:
        logger.info("【自动点卯】根据最新 API 数据，今天已经点卯过了。")
        needs_checkin = False

    if needs_checkin:
        try:
            # ... (发送点卯指令逻辑不变) ...
            logger.info(f"【自动点卯】正在将指令 '{CHECKIN_COMMAND}' 加入发送队列...")
            success = await context.telegram_client.send_game_command(CHECKIN_COMMAND)
            if success:
                logger.info(f"【自动点卯】指令 '{CHECKIN_COMMAND}' 已成功加入发送队列。")
                logger.info("【自动点卯】触发角色数据同步...")
                try:
                    await event_bus.emit("trigger_character_sync_now")
                except Exception as sync_e:
                    logger.error(f"【自动点卯】尝试在发送指令后触发角色同步时出错: {sync_e}", exc_info=True)

                notify_msg = f"✅ [{my_username or my_id}] 今日宗门点卯指令已发送。"
                try:
                    await event_bus.emit("send_system_notification", notify_msg)
                except Exception as notify_e:
                    logger.error(f"【自动点卯】发送完成通知失败: {notify_e}")

                # --- 修改: 格式化标记 Key ---
                if redis_client:
                    redis_key = REDIS_LAST_RUN_KEY_FORMAT.format(my_id, current_date_str)
                    try:
                        await redis_client.set(redis_key, "1", ex=timedelta(days=2)) # 使用 "1" 作为值
                        logger.info(f"【自动点卯】已在 Redis 记录今天 ({current_date_str}) 的运行 ({redis_key})。")
                    except Exception as e_redis:
                        logger.warning(f"【自动点卯】在 Redis ({redis_key}) 记录上次运行时出错: {e_redis}")
                # --- 修改结束 ---

                await _schedule_next_day_run(context)
            else:
                 logger.error("【自动点卯】将点卯指令加入队列失败。安排重试。")
                 await _schedule_retry(context)
        except Exception as e_send:
            logger.error(f"【自动点卯】将点卯指令加入队列时出错: {e_send}", exc_info=True)
            await _schedule_retry(context)
    else: # 今天已完成
        logger.info("【自动点卯】今天已完成，安排明天任务。")
        send_completion_notify = False
        # --- 修改: 格式化标记 Key ---
        if redis_client:
             redis_key = REDIS_LAST_RUN_KEY_FORMAT.format(my_id, current_date_str)
             try:
                 last_recorded_run = await redis_client.exists(redis_key) # 检查 Key 是否存在
                 if not last_recorded_run: # Key 不存在，说明是今天首次检测到完成
                     send_completion_notify = True
                     await redis_client.set(redis_key, "1", ex=timedelta(days=2))
                     logger.info(f"【自动点卯】首次检测到今天 ({current_date_str}) 已完成，记录到 Redis ({redis_key})。")
                 else:
                     logger.info(f"【自动点卯】今天 ({current_date_str}) 已完成状态已记录 ({redis_key})，无需重复通知。")
             except Exception as e_redis_check:
                 logger.warning(f"【自动点卯】在 Redis ({redis_key}) 检查或记录已完成时出错: {e_redis_check}")
                 send_completion_notify = True
        else:
            send_completion_notify = True
        # --- 修改结束 ---

        if send_completion_notify:
            notify_msg = f"✅ [{my_username or my_id}] 今日宗门点卯已完成。"
            try:
                await event_bus.emit("send_system_notification", notify_msg)
            except Exception as notify_e:
                logger.error(f"【自动点卯】发送完成通知失败: {notify_e}")

        await _schedule_next_day_run(context)

# --- 插件类 (其余部分保持不变) ---
class Plugin(BasePlugin):
    """自动宗门点卯插件 (智能调度版)"""
    def __init__(self, context: AppContext, plugin_name: str, cn_name: str | None = None):
        super().__init__(context, plugin_name, cn_name)
        self.load_config()
        if self.auto_enabled: self.info(f"插件已加载并启用。采用智能调度。指令: '{CHECKIN_COMMAND}'")
        else: self.info("插件已加载但未启用。")

    def load_config(self):
        self.auto_enabled = self.config.get("sect_checkin.auto_enabled", False)

    def register(self):
        # ... (注册逻辑不变) ...
        if not self.auto_enabled: return
        self.event_bus.on("telegram_client_started", self.initial_check_and_schedule)
        self.info(f"已注册 'telegram_client_started' 监听器，用于启动时检查点卯任务。")

    async def initial_check_and_schedule(self):
        """应用启动时检查任务状态"""
        self.info("【自动点卯】TG 客户端已启动，开始启动时检查任务状态...")
        await asyncio.sleep(random.uniform(5, 10)) # 随机延迟启动检查

        context = self.context
        if not context or not context.scheduler or not context.data_manager or not context.redis or not context.telegram_client:
             self.error("【自动点卯】启动检查失败：核心服务不可用。")
             return

        my_id = context.telegram_client._my_id
        if not my_id: self.error("【自动点卯】启动检查失败：无法获取 User ID。"); return

        try:
            job = None
            try: job = context.scheduler.get_job(CHECKIN_JOB_ID)
            except JobLookupError as e: self.warning(f"【自动点卯】启动检查：无法加载旧任务 (LookupError): {e}。将视为空任务。"); job = None

            local_tz = await _get_local_timezone(context.config); today_date_str = datetime.now(local_tz).strftime("%Y-%m-%d")

            redis_client = context.redis.get_client(); last_run_completed_today = False
            # --- 修改: 格式化标记 Key ---
            if redis_client:
                 redis_key = REDIS_LAST_RUN_KEY_FORMAT.format(my_id, today_date_str)
                 try: last_run_completed_today = await redis_client.exists(redis_key)
                 except Exception as e_redis: self.warning(f"【自动点卯】启动检查：读取 Redis LastRunKey ({redis_key}) 失败: {e_redis}")
            # --- 修改结束 ---

            last_checkin_date_api = None
            # 如果 Redis 没有记录，再尝试从缓存获取 API 数据
            if not last_run_completed_today:
                self.info("【自动点卯】启动检查：Redis 中无今日运行记录，检查 DataManager 缓存...")
                sect_data = await context.data_manager.get_sect_info(my_id, use_cache=True)
                if sect_data: last_checkin_date_api = sect_data.get("last_sect_check_in")
                # 再次判断 API 数据
                if last_checkin_date_api == today_date_str:
                    last_run_completed_today = True # API 显示今天已完成
                    # 可选：如果 API 显示完成但 Redis 没记录，可以在 Redis 补上记录
                    if redis_client:
                         redis_key = REDIS_LAST_RUN_KEY_FORMAT.format(my_id, today_date_str)
                         try: await redis_client.set(redis_key, "1", ex=timedelta(days=2))
                         except: pass

            self.info(f"【自动点卯】启动检查：今天日期 '{today_date_str}', 是否已完成 '{last_run_completed_today}' (基于Redis标记或API缓存: {last_checkin_date_api})")

            if last_run_completed_today:
                self.info("【自动点卯】启动检查：今天已完成。")
                if not job:
                    self.warning("【自动点卯】启动检查：今天已完成，但未找到未来的调度任务！将安排明天任务。")
                    await _schedule_next_day_run(context)
                else: self.info(f"【自动点卯】启动检查：已找到未来的调度任务: {job.next_run_time}")
            else: # 今天未完成
                self.info("【自动点卯】启动检查：今天尚未完成。")
                if job:
                    job_next_run_local = job.next_run_time.astimezone(local_tz)
                    if job_next_run_local.strftime("%Y-%m-%d") == today_date_str:
                        self.info(f"【自动点卯】启动检查：任务已安排在今天 {job_next_run_local.strftime('%H:%M:%S')} 执行。")
                    else:
                        self.warning(f"【自动点卯】启动检查：任务已安排在 {job_next_run_local.strftime('%Y-%m-%d')} (不是今天)，将立即重新安排执行。")
                        context.scheduler.add_job(
                            _execute_checkin_and_reschedule, trigger='date',
                            run_date=datetime.now(pytz.utc) + timedelta(seconds=10),
                            id=CHECKIN_JOB_ID, replace_existing=True
                        )
                else: # 今天未完成，且没有任务
                    self.warning("【自动点卯】启动检查：今天未完成且无任务安排！将立即安排执行。")
                    context.scheduler.add_job(
                        _execute_checkin_and_reschedule, trigger='date',
                        run_date=datetime.now(pytz.utc) + timedelta(seconds=10),
                        id=CHECKIN_JOB_ID, replace_existing=True
                    )
        except Exception as e_init:
            self.error(f"【自动点卯】启动时检查任务状态失败: {e_init}", exc_info=True)
            try:
                 self.warning("【自动点卯】因启动检查失败，尝试立即安排一次执行...")
                 context.scheduler.add_job(
                     _execute_checkin_and_reschedule, trigger='date',
                     run_date=datetime.now(pytz.utc) + timedelta(seconds=15),
                     id=CHECKIN_JOB_ID, replace_existing=True
                 )
            except Exception as e_retry: self.error(f"【自动点卯】安排紧急重试任务失败: {e_retry}")


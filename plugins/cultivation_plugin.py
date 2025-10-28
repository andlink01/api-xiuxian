import logging
import asyncio
import random
import pytz
from datetime import datetime, timedelta
from typing import Optional
from plugins.base_plugin import BasePlugin, AppContext
from pyrogram.types import Message
from plugins.character_sync_plugin import parse_iso_datetime, format_local_time # 时间处理仍需
from apscheduler.jobstores.base import JobLookupError
from core.context import get_global_context

# --- 常量 ---
JOB_ID = 'auto_cultivation_job'
TIMEOUT_JOB_ID = 'cultivation_timeout_job'
REDIS_WAITING_KEY_PREFIX = "cultivation_waiting_msg_id:{}" # Redis 等待状态 Key 格式 (使用占位符)
# --- 修改: 错误通知锁 Key 包含 user_id 占位符 ---
REDIS_ERROR_NOTIFY_LOCK_KEY_FORMAT = "cultivation:error_notify_lock:{}" # Redis 错误通知锁 Key 格式
# --- 修改结束 ---
ERROR_NOTIFY_LOCK_TTL = 3600
RESPONSE_KEYWORDS = ["【闭关成功】", "【闭关失败】", "灵气尚未平复"]
STARTUP_DELAY_SECONDS = 15

async def _send_cultivation_command_to_queue():
    """由 APScheduler 调度的函数，用于将闭关指令加入队列"""
    logger = logging.getLogger("CultivationPlugin.SendCmd")
    logger.info("【自动闭关】定时任务触发：准备将闭关指令加入队列...")

    context = get_global_context()
    if not context or not context.data_manager or not context.telegram_client or not context.redis:
        logger.error("【自动闭关】无法执行：核心服务 (DataManager/TGClient/Redis) 不可用。")
        return

    config = context.config
    auto_enabled = config.get("cultivation.auto_enabled", False)
    command = config.get("cultivation.command", ".闭关修炼")

    if not auto_enabled:
        logger.info("【自动闭关】已被禁用，取消本次指令发送。")
        return

    my_id = context.telegram_client._my_id

    if not my_id:
        logger.error("【自动闭关】无法获取助手 User ID，跳过本次闭关并安排重试调度。")
        asyncio.create_task(_schedule_retry_scheduling())
        return

    redis_client = context.redis.get_client()
    if not redis_client:
        logger.error("【自动闭关】Redis 未连接，无法检查等待状态，跳过本次闭关并安排重试调度。")
        asyncio.create_task(_schedule_retry_scheduling())
        return

    # --- 修改: 格式化等待 Key ---
    redis_key = REDIS_WAITING_KEY_PREFIX.format(my_id)
    # --- 修改结束 ---
    try:
        waiting_msg_id = await redis_client.get(redis_key)
        if waiting_msg_id:
            logger.warning(f"【自动闭关】尝试发送指令，但 Redis 状态显示仍在等待 MsgID: {waiting_msg_id} 的响应，本次跳过。")
            return
        logger.info("【自动闭关】检查 Redis 等待状态：当前非等待状态。")

        logger.info("【自动闭关】获取实时角色状态以确认是否可闭关...")
        latest_status_data = await context.data_manager.get_character_status(my_id, use_cache=False)

        if not latest_status_data:
             logger.error("【自动闭关】无法获取最新的角色状态，取消本次闭关并安排重试。")
             asyncio.create_task(_schedule_retry_scheduling())
             return

        current_status = latest_status_data.get("status")
        cooldown_str = latest_status_data.get("cultivation_cooldown_until")
        now_utc = datetime.now(pytz.utc)
        is_on_cooldown = False
        if cooldown_str:
            cooldown_dt = parse_iso_datetime(cooldown_str)
            if cooldown_dt and cooldown_dt > now_utc:
                is_on_cooldown = True

        logger.info(f"【自动闭关】实时状态检查： Status='{current_status}', CooldownActive={is_on_cooldown}")

        if current_status not in ["normal"] or is_on_cooldown:
             logger.info(f"【自动闭关】实时状态检查不通过 (Status='{current_status}', CooldownActive={is_on_cooldown})，取消本次闭关，重新安排调度。")
             asyncio.create_task(_schedule_next_cultivation()) # 让主调度函数处理
             return

        logger.info(f"【自动闭关】实时状态检查通过，正在将指令 '{command}' 加入发送队列...")
        success = await context.telegram_client.send_game_command(command)
        if success:
            logger.info(f"【自动闭关】指令 '{command}' 已成功加入发送队列。等待 'game_command_sent' 事件以设置超时。")
        else:
             logger.error("【自动闭关】将闭关指令加入队列失败。将安排重试。")
             asyncio.create_task(_schedule_retry_scheduling())

    except Exception as e:
        logger.error(f"【自动闭关】发送闭关指令或检查状态时出错: {e}", exc_info=True)
        asyncio.create_task(_schedule_retry_scheduling())


async def _handle_cultivation_timeout():
    """处理等待响应超时"""
    logger = logging.getLogger("CultivationPlugin.Timeout")
    logger.info("【自动闭关】超时任务触发：检查闭关响应是否超时...")

    context = get_global_context()
    if not context or not context.redis or not context.telegram_client:
        logger.error("【自动闭关】无法处理超时：核心服务不可用。")
        return

    my_id = context.telegram_client._my_id
    if not my_id: logger.error("【自动闭关】无法获取助手 User ID，无法处理超时状态。"); return

    redis_client = context.redis.get_client()
    if not redis_client: logger.error("【自动闭关】Redis 未连接，无法处理超时状态。"); return

    # --- 修改: 格式化等待 Key ---
    redis_key = REDIS_WAITING_KEY_PREFIX.format(my_id)
    # --- 修改结束 ---
    try:
        waiting_msg_id_str = await redis_client.get(redis_key)
        if waiting_msg_id_str:
            logger.warning(f"【自动闭关】确认超时！等待闭关响应 (针对 MsgID: {waiting_msg_id_str}) 超时。清除状态。")
            await redis_client.delete(redis_key)
            logger.info("【自动闭关】超时状态已从 Redis 清除。")

            logger.info("【自动闭关】超时后触发角色数据同步...")
            try: await context.event_bus.emit("trigger_character_sync_now")
            except Exception as sync_e: logger.error(f"【自动闭关】超时后尝试触发角色同步时出错: {sync_e}", exc_info=True)

            logger.info("【自动闭关】超时后立即尝试安排下一次闭关任务...")
            asyncio.create_task(_schedule_next_cultivation())
        else:
            logger.info("【自动闭关】超时任务触发，但 Redis 中已无等待状态，忽略。")
    except Exception as e:
        logger.error(f"【自动闭关】处理闭关超时状态时出错: {e}", exc_info=True)
        logger.warning("【自动闭关】因超时处理出错，仍尝试触发同步并安排下次调度...")
        if context:
            try: await context.event_bus.emit("trigger_character_sync_now")
            except Exception: pass
            asyncio.create_task(_schedule_next_cultivation())


async def _schedule_next_cultivation():
    """顶层函数：查询缓存/API 获取冷却时间并安排下一次闭关任务"""
    logger = logging.getLogger("CultivationPlugin.Schedule")
    logger.info("【自动闭关】调度函数启动：开始检查并安排下一次闭关...")
    context = None
    try:
        context = get_global_context()
        if not context or not context.data_manager or not context.scheduler or not context.telegram_client or not context.redis:
            logger.error("【自动闭关】无法安排闭关：核心服务不可用。")
            return

        logger.info("【自动闭关】核心服务获取成功，继续调度...")
        config = context.config; scheduler = context.scheduler
        redis = context.redis; telegram_client = context.telegram_client
        event_bus = context.event_bus; redis_client = redis.get_client()
        data_manager = context.data_manager

        auto_enabled = config.get("cultivation.auto_enabled", False)
        cultivation_plugin_instance = getattr(context, 'cultivation_plugin', None)
        if not auto_enabled or (cultivation_plugin_instance and not cultivation_plugin_instance.is_running_manually):
             logger.info("【自动闭关】已被禁用或手动停止，停止调度。")
             try: scheduler.remove_job(JOB_ID)
             except JobLookupError: pass
             logger.info(f"【自动闭关】已尝试移除主调度任务 (因禁用或停止)。")
             return

        my_id = telegram_client._my_id
        if not my_id:
            logger.error("【自动闭关】无法获取助手 User ID，暂时无法安排下一次闭关。将在指定延迟后重试调度。")
            asyncio.create_task(_schedule_retry_scheduling())
            return

        logger.info(f"【自动闭关】准备使用 DataManager 获取用户 {my_id} 的状态缓存...")
        status_data = await data_manager.get_character_status(my_id, use_cache=True)
        api_error = False

        if not status_data:
             logger.error("【自动闭关】无法从 DataManager 获取角色状态缓存，将安排重试。")
             api_error = True

        next_run_utc_dt = None
        retry_delay_sec = int(config.get("cultivation.retry_delay_on_fail", 300))
        delay_range = config.get("cultivation.random_delay_range", [1, 5])
        min_delay_sec = max(0, int(delay_range[0]) if len(delay_range) > 0 else 1)
        max_delay_sec = max(min_delay_sec + 1, int(delay_range[1]) if len(delay_range) > 1 else 5)
        logger.info(f"【自动闭关】计算参数：随机延迟范围 [{min_delay_sec}, {max_delay_sec}] 秒, 失败重试延迟 {retry_delay_sec} 秒。")

        # --- 修改: 格式化错误锁 Key ---
        error_lock_key = REDIS_ERROR_NOTIFY_LOCK_KEY_FORMAT.format(my_id) if my_id else None
        # --- 修改结束 ---

        if status_data:
            current_status = status_data.get("status")
            logger.info(f"【自动闭关】角色当前状态 (来自缓存): {current_status}")

            if current_status in ["deep_seclusion", "cultivating", "fleeing"]:
                # ... (处理不适合闭关的状态，逻辑不变) ...
                logger.info(f"【自动闭关】角色当前状态为 '{current_status}'，不适合普通闭关，将安排重试。")
                deep_seclusion_end_str = status_data.get("deep_seclusion_end_time")
                retry_after_seconds = retry_delay_sec
                if current_status == "deep_seclusion" and deep_seclusion_end_str:
                    deep_end_dt = parse_iso_datetime(deep_seclusion_end_str)
                    if deep_end_dt:
                        now_utc = datetime.now(pytz.utc)
                        if deep_end_dt > now_utc:
                            time_diff = deep_end_dt - now_utc
                            retry_after_seconds = max(15, time_diff.total_seconds() + random.uniform(5, 15))
                            logger.info(f"【自动闭关】深度闭关尚未结束 ({status_data.get('deep_seclusion_end_time_formatted')})，将在约 {retry_after_seconds:.0f} 秒后重试调度。")
                        else:
                            retry_after_seconds = random.uniform(15, 30)
                            logger.info(f"【自动闭关】深度闭关结束时间已过，将在 {retry_after_seconds:.0f} 秒后重试调度。")
                asyncio.create_task(_schedule_retry_scheduling(delay_seconds=retry_after_seconds))
                return

            cooldown_str = status_data.get("cultivation_cooldown_until")
            logger.info(f"【自动闭关】从缓存获取到闭关冷却时间字符串: '{cooldown_str}'")

            if cooldown_str is None or cooldown_str == "":
                # ... (计算立即执行时间，逻辑不变) ...
                logger.info("【自动闭关】缓存显示可以立即闭关。计算下次运行时间...")
                now_utc_dt = datetime.now(pytz.utc)
                random_delay = timedelta(seconds=random.uniform(min_delay_sec, max_delay_sec))
                next_run_utc_dt = now_utc_dt + random_delay
            elif isinstance(cooldown_str, str):
                # ... (计算冷却后执行时间，逻辑不变) ...
                logger.info(f"【自动闭关】尝试解析冷却时间字符串 '{cooldown_str}'...")
                cooldown_utc_dt = parse_iso_datetime(cooldown_str)
                if cooldown_utc_dt:
                    now_utc_dt = datetime.now(pytz.utc)
                    random_delay = timedelta(seconds=random.uniform(min_delay_sec, max_delay_sec))
                    if cooldown_utc_dt > now_utc_dt:
                        next_run_utc_dt = cooldown_utc_dt + random_delay
                        logger.info(f"【自动闭关】冷却时间未到 ({status_data.get('cultivation_cooldown_until_formatted')})。计算得到下次运行时间: {format_local_time(next_run_utc_dt)}")
                    else:
                        next_run_utc_dt = now_utc_dt + random_delay
                        logger.info(f"【自动闭关】冷却时间已过。计算得到下次运行时间: {format_local_time(next_run_utc_dt)}")

                    max_reasonable_future = now_utc_dt + timedelta(days=2)
                    min_reasonable_future = now_utc_dt + timedelta(seconds=1)
                    if not (min_reasonable_future <= next_run_utc_dt <= max_reasonable_future):
                         logger.warning(f"【自动闭关】计算出的下次运行时间 {format_local_time(next_run_utc_dt)} 不在合理范围！将安排重试。")
                         next_run_utc_dt = None
                    else:
                         logger.info(f"【自动闭关】计算出的下次运行时间 {format_local_time(next_run_utc_dt)} 在合理范围内。")
                else:
                    logger.error(f"【自动闭关】无法解析缓存中的冷却时间戳字符串: '{cooldown_str}'，将安排重试。")
                    api_error = True
                    # --- 修改: 使用格式化后的 error_lock_key ---
                    if redis_client and event_bus and error_lock_key:
                        if await redis_client.set(error_lock_key, "1", ex=ERROR_NOTIFY_LOCK_TTL, nx=True):
                            await event_bus.emit("send_system_notification", f"⚠️ **自动修炼 - 缓存数据错误** ⚠️\n\n无法解析缓存中的冷却时间戳 (收到字符串: `{cooldown_str}`)。\n自动修炼已暂停并进入重试循环。")
                    # --- 修改结束 ---
            else:
                 logger.error(f"【自动闭关】缓存中的 'cultivation_cooldown_until' 字段类型未知 ({type(cooldown_str)}) 或为空，将安排重试。 Value: '{cooldown_str}'")
                 api_error = True
                 # --- 修改: 使用格式化后的 error_lock_key ---
                 if redis_client and event_bus and error_lock_key:
                     if await redis_client.set(error_lock_key, "1", ex=ERROR_NOTIFY_LOCK_TTL, nx=True):
                         await event_bus.emit("send_system_notification", f"⚠️ **自动修炼 - 缓存数据错误** ⚠️\n\n缓存中 'cultivation_cooldown_until' 字段类型未知或为空 (收到: `{cooldown_str}`)。\n自动修炼已暂停并进入重试循环。")
                 # --- 修改结束 ---

        else: # status_data 获取失败
            logger.error("【自动闭关】无法从 DataManager 获取状态，无法计算冷却时间，将安排重试。")

        if api_error:
             # --- 修改: 使用格式化后的 error_lock_key ---
             if redis_client and event_bus and error_lock_key:
                  if await redis_client.set(error_lock_key, "1", ex=ERROR_NOTIFY_LOCK_TTL, nx=True):
                      error_reason = "获取状态缓存失败" if not status_data else "解析缓存时间戳失败"
                      await event_bus.emit("send_system_notification", f"⚠️ **自动修炼 - 数据错误** ⚠️\n\n{error_reason}，自动修炼已暂停并进入重试循环。")
             # --- 修改结束 ---

        if next_run_utc_dt:
            logger.info(f"【自动闭关】最终确认下次执行闭关指令的时间: {format_local_time(next_run_utc_dt)}")
            try:
                # --- 修改: 使用格式化后的 error_lock_key ---
                if redis_client and error_lock_key:
                    try:
                        deleted_count = await redis_client.delete(error_lock_key)
                        if deleted_count > 0: logger.info(f"【自动闭关】成功清除 Redis 错误通知锁 ({error_lock_key})。")
                    except Exception as e_del: logger.warning(f"【自动闭关】清除闭关错误通知锁 ({error_lock_key}) 失败: {e_del}")
                # --- 修改结束 ---

                logger.info(f"【自动闭关】正在向 APScheduler 添加/更新任务 '{JOB_ID}'...")
                if scheduler:
                    scheduler.add_job(
                        _send_cultivation_command_to_queue,
                        trigger='date', run_date=next_run_utc_dt, id=JOB_ID,
                        replace_existing=True, misfire_grace_time=60
                    )
                    next_run_local_str = format_local_time(next_run_utc_dt)
                    logger.info(f"【自动闭关】成功安排下一次自动闭关任务在: {next_run_local_str}")
                else:
                    logger.error("【自动闭关】无法安排任务：Scheduler 不可用。")
                    asyncio.create_task(_schedule_retry_scheduling(delay_seconds=retry_delay_sec / 2))

            except Exception as e:
                logger.critical(f"【自动闭关】安排下一次闭关任务 (_send_cultivation_command_to_queue) 失败: {e}", exc_info=True)
                asyncio.create_task(_schedule_retry_scheduling(delay_seconds=retry_delay_sec / 2))
        else:
            logger.warning("【自动闭关】未能计算出下次执行时间，将安排重试调度。")
            asyncio.create_task(_schedule_retry_scheduling())

    except Exception as e:
        logger.critical(f"【自动闭关】调度函数 (_schedule_next_cultivation) 执行过程中发生严重意外错误: {e}", exc_info=True)
        try:
            if context: asyncio.create_task(_schedule_retry_scheduling())
            else: logger.error("【自动闭关】在 _schedule_next_cultivation 异常处理中无法安排重试，因为 context 为 None。")
        except Exception as retry_e:
            logger.critical(f"【自动闭关】在处理 _schedule_next_cultivation 异常后安排重试也失败了: {retry_e}")


# --- 重试函数 (_schedule_retry_scheduling) 保持不变 ---
_cult_retry_logger = logging.getLogger("CultivationPlugin.Retry")
async def _schedule_retry_scheduling(delay_seconds: Optional[int | float] = None):
    """安排一个重试任务来再次调用 _schedule_next_cultivation"""
    logger = _cult_retry_logger
    logger.info("【自动闭关】触发重试调度逻辑...")
    context = get_global_context()
    if not context or not context.scheduler:
         logger.error("【自动闭关】无法安排重试：AppContext 或 Scheduler 不可用。")
         return
    scheduler = context.scheduler
    retry_delay_sec_config = int(context.config.get("cultivation.retry_delay_on_fail", 300))
    raw_delay = delay_seconds if delay_seconds is not None else retry_delay_sec_config
    actual_delay = max(15.0, float(raw_delay)) + random.uniform(0, 5)
    retry_dt_aware = datetime.now(pytz.utc) + timedelta(seconds=actual_delay)
    logger.info(f"【自动闭关】将在 {actual_delay:.2f} 秒后 ({format_local_time(retry_dt_aware)}) 重新尝试调度主函数。")
    try:
        try: # 移除旧任务
            logger.info(f"【自动闭关】重试前，尝试移除可能存在的旧任务 '{JOB_ID}'...")
            await asyncio.to_thread(scheduler.remove_job, JOB_ID)
            logger.info(f"【自动闭关】成功移除旧任务 '{JOB_ID}'。")
        except JobLookupError: logger.info(f"【自动闭关】旧任务 '{JOB_ID}' 未找到，无需移除。")
        except Exception as remove_err: logger.warning(f"【自动闭关】重试前移除旧任务 '{JOB_ID}' 时出错: {remove_err}")
        logger.info(f"【自动闭关】正在向 APScheduler 添加/更新重试任务 '{JOB_ID}'...")
        scheduler.add_job(
            _schedule_next_cultivation, # 目标是再次运行主调度
            trigger='date', run_date=retry_dt_aware, id=JOB_ID,
            replace_existing=True, misfire_grace_time=60
        )
        retry_local_str = format_local_time(retry_dt_aware)
        logger.info(f"【自动闭关】已成功安排重试调度任务在 {retry_local_str} 左右执行。")
    except Exception as e:
        logger.critical(f"【自动闭关】安排闭关重试任务失败: {e}", exc_info=True)


# --- 插件类 (其余部分保持不变) ---
class Plugin(BasePlugin):
    """实现自动闭关修炼功能。"""
    def __init__(self, context: AppContext, plugin_name: str, cn_name: str | None = None):
        super().__init__(context, plugin_name, cn_name)
        setattr(context, plugin_name, self) # 附加实例到 context
        self.load_config()
        self.is_running_manually = self.auto_enabled_config
        self.cultivation_command_text = self.config.get("cultivation.command", ".闭关修炼").strip()
        if self.auto_enabled_config: self.info(f"插件已加载并根据配置初始化为【启用】状态。指令: '{self.cultivation_command_text}'") # 使用 self.cultivation_command_text
        else: self.info("插件已加载并根据配置初始化为【禁用】状态。")

    def load_config(self):
        """加载配置"""
        self.info("正在加载/重新加载自动闭关配置...")
        self.auto_enabled_config = self.config.get("cultivation.auto_enabled", False)
        self.command = self.config.get("cultivation.command", ".闭关修炼") # self.command 可能未及时更新，用 self.cultivation_command_text
        self.cultivation_command_text = self.command.strip()
        self.timeout = int(self.config.get("cultivation.response_timeout", 120))
        delay_range = self.config.get("cultivation.random_delay_range", [1, 5])
        try: self.min_delay = int(delay_range[0]); self.max_delay = int(delay_range[1])
        except: self.warning(f"无效的 random_delay_range: {delay_range}，用默认 [1, 5]"); self.min_delay = 1; self.max_delay = 5
        self.retry_delay = int(self.config.get("cultivation.retry_delay_on_fail", 300))
        if self.min_delay < 0: self.min_delay = 0
        if self.max_delay < self.min_delay: self.max_delay = self.min_delay + 1
        self.info(f"配置加载完成: enabled={self.auto_enabled_config}, timeout={self.timeout}, delay=[{self.min_delay},{self.max_delay}], retry={self.retry_delay}")

    def register(self):
        """注册事件监听"""
        self.debug("register() 方法被调用。")
        try:
            self.event_bus.on("telegram_client_started", self.initial_check_and_schedule)
            self.event_bus.on("game_command_sent", self.handle_command_sent)
            self.event_bus.on("game_response_received", self.handle_game_response)
            self.event_bus.on("start_auto_cultivation", self.handle_start_auto_cultivation)
            self.event_bus.on("stop_auto_cultivation", self.handle_stop_auto_cultivation)
            self.info("已注册所有自动闭关相关事件监听器。")
        except Exception as e: self.error(f"注册事件监听器时发生错误: {e}", exc_info=True)

    async def initial_check_and_schedule(self):
        """启动时检查并开始调度"""
        self.info("监听到 'telegram_client_started' 事件，开始启动时检查与调度...")
        try:
            self.load_config()
            self.info(f"启动检查：当前配置 auto_enabled = {self.auto_enabled_config}")
            if self.auto_enabled_config:
                self.info("配置为【启用】，准备延迟后开始调度...")
                self.is_running_manually = True
                await self._ensure_no_duplicate_schedule()
                delay = STARTUP_DELAY_SECONDS + random.uniform(0, 5)
                self.info(f"启动延迟 {delay:.1f} 秒...")
                await asyncio.sleep(delay)
                self.info("启动延迟结束，调用主调度函数...")
                await _schedule_next_cultivation()
                self.info("启动时的主调度函数调用完成。")
            else:
                self.info("配置为【禁用】，不进行启动调度，并确保停止所有相关任务。")
                self.is_running_manually = False
                await self._stop_internal()
        except Exception as e:
             self.critical(f"启动时检查与调度过程中发生严重意外错误: {e}", exc_info=True)
             self.is_running_manually = False
        finally:
            self.info("启动时检查与调度流程执行完毕。")

    async def handle_command_sent(self, sent_message: Message, command_text: str):
        """监听闭关指令发送，设置等待状态和超时"""
        if command_text.strip() != self.cultivation_command_text: return
        self.info(f"监听到【闭关指令】已发送 (MsgID: {sent_message.id})，准备设置等待状态和超时任务。")
        my_id = self.context.telegram_client._my_id
        if not my_id: self.error("无法获取助手 User ID，无法设置等待状态！"); return
        redis_client = self.context.redis.get_client()
        if not redis_client: self.error("Redis 未连接，无法设置等待状态！"); return
        # --- 修改: 格式化等待 Key ---
        redis_key = REDIS_WAITING_KEY_PREFIX.format(my_id)
        # --- 修改结束 ---
        timeout_seconds = self.timeout
        try:
            self.info(f"正在设置 Redis 等待状态 (Key: {redis_key}, Value: {sent_message.id}, TTL: {timeout_seconds + 60}s)...")
            await redis_client.set(redis_key, str(sent_message.id), ex=timeout_seconds + 60)
            self.info(f"Redis 等待状态设置成功。")
            timeout_run_dt_aware = datetime.now(pytz.utc) + timedelta(seconds=timeout_seconds)
            self.info(f"正在安排超时检查任务 '{TIMEOUT_JOB_ID}' 在 {format_local_time(timeout_run_dt_aware)} 左右执行...")
            if self.scheduler:
                self.scheduler.add_job(_handle_cultivation_timeout, trigger='date', run_date=timeout_run_dt_aware, id=TIMEOUT_JOB_ID, replace_existing=True, misfire_grace_time=10)
                self.info(f"超时检查任务安排成功。")
            else:
                 self.error("【自动闭关】无法安排超时任务：Scheduler 不可用。")
                 try: await redis_client.delete(redis_key)
                 except: pass
        except Exception as e:
            self.error(f"设置闭关等待状态或超时任务时出错: {e}", exc_info=True)
            try:
                 self.warning("因设置出错，尝试清理 Redis 等待状态...")
                 if redis_client: await redis_client.delete(redis_key)
                 self.info("Redis 等待状态清理完成。")
            except Exception: pass
            self.warning("因设置出错，安排重试调度...")
            asyncio.create_task(_schedule_retry_scheduling())

    async def handle_game_response(self, message: Message, is_reply_to_me: bool, is_mentioning_me: bool):
        """处理游戏响应，清除等待状态、触发缓存更新并立即安排下次"""
        if not self.is_running_manually: return
        text = message.text or message.caption
        if not text: return
        is_cultivation_response = any(keyword in text for keyword in RESPONSE_KEYWORDS)
        if not is_cultivation_response: return
        self.info(f"收到可能与闭关相关的游戏响应 (MsgID: {message.id})，检查是否回复了我...")
        if not is_reply_to_me: return
        my_id = self.context.telegram_client._my_id
        if not my_id: self.error("无法获取助手 User ID，无法处理响应状态。"); return
        redis_client = self.context.redis.get_client()
        if not redis_client: self.error("Redis 未连接，无法处理响应状态。"); return
        # --- 修改: 格式化等待 Key ---
        redis_key = REDIS_WAITING_KEY_PREFIX.format(my_id)
        # --- 修改结束 ---
        self.info(f"正在检查 Redis 等待状态 (Key: '{redis_key}')...")
        try:
            expected_command_id_str = await redis_client.get(redis_key)
            self.info(f"Redis 返回期望的指令 MsgID: '{expected_command_id_str}'")
            if not expected_command_id_str: self.info("当前未处于等待闭关响应状态，忽略此响应。"); return
            actual_reply_to_id = message.reply_to_message_id
            self.info(f"此响应实际回复的 MsgID: {actual_reply_to_id}")
            if actual_reply_to_id != int(expected_command_id_str): self.info(f"回复的 MsgID 不符，忽略此响应。"); return
            self.info(f"确认收到对我们闭关指令 (MsgID: {expected_command_id_str}) 的有效响应！")
            self.info(f"正在删除 Redis 等待状态 Key '{redis_key}'...")
            await redis_client.delete(redis_key)
            self.info("Redis 等待状态已清除。")
            try:
                self.info(f"正在尝试移除超时任务 '{TIMEOUT_JOB_ID}'...")
                if self.scheduler: await asyncio.to_thread(self.scheduler.remove_job, TIMEOUT_JOB_ID)
                self.info(f"成功移除超时任务 '{TIMEOUT_JOB_ID}' (如果存在)。")
            except JobLookupError: self.info(f"超时任务 '{TIMEOUT_JOB_ID}' 未找到。")
            except Exception as e: self.warning(f"移除超时任务失败: {e}")
            self.info("【自动闭关】收到有效回复后触发角色数据同步...")
            try: await self.context.event_bus.emit("trigger_character_sync_now")
            except Exception as sync_e: self.error(f"【自动闭关】尝试在收到回复后触发角色同步时出错: {sync_e}", exc_info=True)
            self.info("【自动闭关】收到有效回复后立即尝试安排下一次闭关任务...")
            asyncio.create_task(_schedule_next_cultivation())
        except ValueError: self.error(f"Redis 中存储的期望 MsgID '{expected_command_id_str}' 无效！")
        except Exception as e:
            self.error(f"处理闭关响应状态时出错: {e}", exc_info=True)
            self.warning("【自动闭关】因处理响应出错，仍尝试触发同步并安排下次调度...")
            if self.context:
                try: await self.context.event_bus.emit("trigger_character_sync_now")
                except Exception: pass
                asyncio.create_task(_schedule_next_cultivation())

    async def handle_start_auto_cultivation(self):
        """响应手动开启事件"""
        self.info("收到【手动开启】自动闭关的事件。")
        if self.is_running_manually: self.info("自动闭关已处于运行状态，无需再次启动。"); return
        self.info("标记状态为【运行中】，加载最新配置并开始调度...")
        self.is_running_manually = True
        self.load_config()
        await self._ensure_no_duplicate_schedule()
        self.info("开始调用主调度函数...")
        asyncio.create_task(_schedule_next_cultivation())
        self.info("主调度函数任务已创建。")

    async def handle_stop_auto_cultivation(self):
        """响应手动关闭事件"""
        self.info("收到【手动关闭】自动闭关的事件。")
        if not self.is_running_manually: self.info("自动闭关已处于停止状态，无需再次停止。"); return
        self.info("标记状态为【已停止】，并清理相关任务和状态...")
        self.is_running_manually = False
        await self._stop_internal()
        self.info("自动闭关已停止。")

    async def _stop_internal(self):
        """内部停止逻辑"""
        self.info("执行内部停止逻辑：移除定时任务和 Redis 状态...")
        my_id = self.context.telegram_client._my_id if self.context.telegram_client else None
        if not my_id: self.warning("_stop_internal: 无法获取 my_id，无法清理 Redis Key。")
        removed_main = False; removed_timeout = False
        try:
            if self.scheduler:
                try: self.info(f"尝试移除主任务 '{JOB_ID}'..."); await asyncio.to_thread(self.scheduler.remove_job, JOB_ID); removed_main = True; self.info(f"成功移除主任务 '{JOB_ID}'。")
                except JobLookupError: self.info(f"主任务 '{JOB_ID}' 未找到。")
                try: self.info(f"尝试移除超时任务 '{TIMEOUT_JOB_ID}'..."); await asyncio.to_thread(self.scheduler.remove_job, TIMEOUT_JOB_ID); removed_timeout = True; self.info(f"成功移除超时任务 '{TIMEOUT_JOB_ID}'。")
                except JobLookupError: self.info(f"超时任务 '{TIMEOUT_JOB_ID}' 未找到。")
                if removed_main or removed_timeout: self.info(f"自动闭关相关任务已移除。")
            else: self.warning("无法移除任务：Scheduler 不可用。")
        except Exception as e: self.error(f"移除自动闭关任务时出错: {e}", exc_info=True)
        if my_id and self.context.redis:
            redis_client = self.context.redis.get_client()
            if redis_client:
                # --- 修改: 格式化等待 Key ---
                redis_key = REDIS_WAITING_KEY_PREFIX.format(my_id)
                # --- 修改结束 ---
                try:
                    self.info(f"尝试清除 Redis 等待 Key: '{redis_key}'...")
                    deleted_count = await redis_client.delete(redis_key)
                    if deleted_count > 0: self.info(f"成功清除 Redis 等待 Key: '{redis_key}'")
                    else: self.info(f"Redis 等待 Key: '{redis_key}' 不存在。")
                except Exception as e_del: self.error(f"清除 Redis 等待状态时出错: {e_del}", exc_info=True)
            else: self.warning("停止时无法连接 Redis 清理状态。")
        elif my_id: self.warning("停止时 Redis 客户端不可用，无法清理状态。")

    async def _ensure_no_duplicate_schedule(self):
         """确保启动前移除旧任务和状态"""
         self.info("确保移除旧的闭关调度任务和 Redis 状态...")
         await self._stop_internal()
         self.info("旧任务和状态清理完毕。")


import logging
import asyncio
import json
import pytz
import random
import re
from datetime import datetime, timedelta, time
from typing import Optional, Tuple, List
from plugins.base_plugin import BasePlugin, AppContext
from core.context import get_global_context
from apscheduler.jobstores.base import JobLookupError
from pyrogram.types import Message, ReplyParameters
from plugins.character_sync_plugin import format_local_time

# --- 常量 ---
TEACH_JOB_ID = 'auto_sect_teach_job'
# --- 修改: 锁 Key 包含 user_id 占位符 ---
REDIS_TEACH_LOCK_KEY_FORMAT = "sect_teach:action_lock:{}" # 检查锁格式
# --- 修改结束 ---
REDIS_PENDING_PLACEHOLDER_KEY_PREFIX = "sect_teach:pending_placeholder" # 已包含 user_id
REDIS_TEACH_COMPLETED_NOTIFY_KEY = "daily_notify_sent:sect_teach" # 已包含 user_id
PENDING_PLACEHOLDER_TTL = 60
TEACH_COMMAND = ".宗门传功"
PLACEHOLDER_MESSAGES = [
    "嗯", "唔", "...", "。。。", "知道了", "收到", "好的",
    "嗯嗯", "哦", "ok", "k", "行", ".", "可",
]
TEACH_SUCCESS_KEYWORDS = ["传功玉简已记录！", "获得了", "点贡献", "今日已传功"]

async def _get_local_timezone(config) -> pytz.BaseTzInfo:
    """获取配置的本地时区，默认为上海"""
    # ... (逻辑不变) ...
    try:
        local_tz_str = config.get("system.timezone", "Asia/Shanghai")
        return pytz.timezone(local_tz_str)
    except pytz.UnknownTimeZoneError:
        logging.getLogger("SectTeachPlugin.Utils").error(f"无效的时区配置: {local_tz_str}，回退到 Asia/Shanghai")
        return pytz.timezone("Asia/Shanghai")

async def _initiate_teach_sequence(context: AppContext):
    """启动一次传功序列：设置标记，发送随机占位符"""
    # ... (逻辑不变，pending_key 已包含 user_id) ...
    logger = logging.getLogger("SectTeachPlugin.Initiate")
    if not context or not context.redis or not context.telegram_client:
        logger.error("【自动传功】无法启动序列：核心服务不可用。"); return False
    redis_client = context.redis.get_client()
    my_id = context.telegram_client._my_id
    if not redis_client or not my_id:
        logger.error("【自动传功】无法启动序列：Redis 或 User ID 不可用。"); return False
    pending_key = f"{REDIS_PENDING_PLACEHOLDER_KEY_PREFIX}:{my_id}"
    try:
        is_already_pending = await redis_client.exists(pending_key)
        if is_already_pending:
            logger.info(f"【自动传功】尝试启动新序列，但仍在等待上一个占位符 (Key: {pending_key})，取消。"); return False
        logger.info("【自动传功】准备启动一次传功序列..."); random_placeholder = random.choice(PLACEHOLDER_MESSAGES)
        logger.info(f"【自动传功】选定的占位消息: '{random_placeholder}'")
        await redis_client.set(pending_key, "1", ex=PENDING_PLACEHOLDER_TTL)
        logger.info(f"【自动传功】已设置 Redis 占位符等待标记 (Key: {pending_key}, TTL: {PENDING_PLACEHOLDER_TTL}s)。")
        success = await context.telegram_client.send_game_command(random_placeholder)
        if success: logger.info(f"【自动传功】占位消息指令 '{random_placeholder}' 已成功加入队列。"); return True
        else:
            logger.error("【自动传功】将占位消息指令加入队列失败。清除等待标记。"); await redis_client.delete(pending_key); return False
    except Exception as e:
         logger.error(f"【自动传功】启动传功序列时出错: {e}")
         try: await redis_client.delete(pending_key)
         except Exception: pass
         return False

async def _check_sect_teach():
    """由 APScheduler 调度的函数，检查是否需要执行宗门传功"""
    logger = logging.getLogger("SectTeachPlugin.CheckStatus")
    logger.info("【自动传功】周期检查任务启动：检查是否需要执行宗门传功...")
    context = get_global_context()
    if not context or not context.data_manager or not context.redis or not context.telegram_client or not context.event_bus:
        logger.error("【自动传功】无法检查：核心服务不可用。"); return
    config = context.config; event_bus = context.event_bus
    auto_enabled = config.get("sect_teach.auto_enabled", False)
    if not auto_enabled: logger.info("【自动传功】已被禁用，跳过检查。"); return

    redis_client = context.redis.get_client()
    my_id = context.telegram_client._my_id
    my_username = context.telegram_client._my_username

    # --- 修改: 在获取锁之前检查 my_id ---
    if not my_id:
        logger.warning("【自动传功】无法获取助手 User ID，跳过本次检查。")
        return
    # --- 修改结束 ---

    lock_acquired = False
    # --- 修改: 格式化锁 Key ---
    lock_key = REDIS_TEACH_LOCK_KEY_FORMAT.format(my_id)
    # --- 修改结束 ---

    if redis_client:
        try:
            # --- 修改: 使用格式化后的 lock_key ---
            lock_acquired = await redis_client.set(lock_key, "1", ex=60, nx=True)
            if not lock_acquired: logger.info(f"【自动传功】获取检查锁 ({lock_key}) 失败，上次检查或操作可能仍在进行中，跳过本次。"); return
            # --- 修改结束 ---
        except Exception as e: logger.error(f"【自动传功】检查或设置 Redis 锁 ({lock_key}) 失败: {e}，跳过本次。"); return
    else: logger.error("【自动传功】Redis 未连接，无法检查锁，任务终止。"); return

    try:
        # my_id 在前面已检查
        local_tz = await _get_local_timezone(config); current_date_str = datetime.now(local_tz).strftime("%Y-%m-%d")
        logger.info("【自动传功】正在从 DataManager 获取宗门缓存数据...")
        sect_data = await context.data_manager.get_sect_info(my_id, use_cache=True)
        if not sect_data: logger.warning("【自动传功】无法从 DataManager 获取宗门数据，跳过本次检查。"); return

        # ... (点卯依赖检查、传功次数检查逻辑不变) ...
        last_checkin_date_str = sect_data.get("last_sect_check_in")
        logger.info(f"【自动传功】依赖检查：今天日期 '{current_date_str}', 上次点卯日期 '{last_checkin_date_str}'")
        if last_checkin_date_str != current_date_str:
            logger.info("【自动传功】依赖检查：今天尚未完成宗门点卯，跳过本次传功检查。"); return
        logger.info("【自动传功】依赖检查：今日已点卯，继续检查传功次数。")

        last_teach_date_str = sect_data.get("last_teach_date"); teach_count_done_api = sect_data.get("teach_count", 0)
        teach_count_done_today = 0
        if last_teach_date_str == current_date_str: teach_count_done_today = teach_count_done_api
        logger.info(f"【自动传功】上次传功日期: {last_teach_date_str}, 缓存记录已完成次数: {teach_count_done_api}。计算今天已完成次数: {teach_count_done_today} 次。")

        needs_teach = teach_count_done_today < 3
        if needs_teach:
            logger.info(f"【自动传功】检测到今天传功未完成 ({teach_count_done_today} < 3)，尝试启动传功序列...")
            await _initiate_teach_sequence(context)
        else:
            logger.info("【自动传功】今天已完成所有传功次数 ({teach_count_done_today} >= 3)，无需操作。")
            # ... (完成通知逻辑不变) ...
            if redis_client:
                notify_key = f"{REDIS_TEACH_COMPLETED_NOTIFY_KEY}:{my_id}:{current_date_str}"
                try:
                    notify_sent = await redis_client.set(notify_key, "1", ex=25*3600, nx=True)
                    if notify_sent:
                        logger.info(f"【自动传功】首次检测到今日传功已全部完成，发送通知。")
                        notify_msg = f"✅ [{my_username or my_id}] 今日宗门传功已全部完成 (3/3)。"
                        await event_bus.emit("send_system_notification", notify_msg)
                    else:
                        logger.info("【自动传功】今日传功完成通知已发送过，跳过。")
                except Exception as notify_err:
                    logger.error(f"【自动传功】检查或发送完成通知时出错: {notify_err}")


    except Exception as e: logger.error(f"【自动传功】检查传功状态时出错: {e}", exc_info=True)
    finally:
        # --- 修改: 使用格式化后的 lock_key ---
        if lock_acquired and redis_client:
            try: await redis_client.delete(lock_key)
            except Exception as e_lock: logger.error(f"【自动传功】释放检查锁 ({lock_key}) 时出错: {e_lock}")
        # --- 修改结束 ---

# --- 插件类 (其余部分保持不变) ---
class Plugin(BasePlugin):
    """自动宗门传功插件 (v5, 依赖点卯)"""
    def __init__(self, context: AppContext, plugin_name: str, cn_name: str | None = None):
        super().__init__(context, plugin_name, cn_name)
        self.load_config()
        self.placeholder_messages = PLACEHOLDER_MESSAGES
        self._my_id: Optional[int] = None
        self._my_username: Optional[str] = None
        if self.auto_enabled: self.info(f"插件已加载并启用。检查间隔: {self.check_interval_minutes} 分钟。指令: '{TEACH_COMMAND}' (需回复)")
        else: self.info("插件已加载但未启用。")

    def load_config(self):
        self.auto_enabled = self.config.get("sect_teach.auto_enabled", False)
        self.check_interval_minutes = self.config.get("sect_teach.check_interval_minutes", 30)
        self.reply_delay_seconds = self.config.get("sect_teach.reply_delay_seconds", 1.5)
        self.next_teach_delay_range = self.config.get("sect_teach.next_teach_delay_range", [2.0, 5.0])

    def register(self):
        # ... (注册逻辑不变) ...
        if not self.auto_enabled: return
        if self.check_interval_minutes < 1: self.check_interval_minutes = 1; self.warning("传功检查间隔不能小于1分钟，已重置为1。")
        try:
            if self.scheduler:
                self.scheduler.add_job(_check_sect_teach, trigger='interval', minutes=self.check_interval_minutes, id=TEACH_JOB_ID, replace_existing=True, misfire_grace_time=120)
                self.info(f"已注册宗门传功状态定时检查任务 (每 {self.check_interval_minutes} 分钟)。")
            else: self.error("无法注册定时任务：Scheduler 不可用。")
            self.event_bus.on("game_command_sent", self.handle_command_sent)
            self.event_bus.on("game_response_received", self.handle_game_response)
            self.info("已注册传功相关的 game_command_sent, game_response_received 事件监听器。")
            self.event_bus.on("telegram_client_started", self.run_initial_check)
        except Exception as e: self.error(f"注册宗门传功定时任务或监听器时出错: {e}", exc_info=True)

    async def run_initial_check(self):
        # ... (启动检查逻辑不变) ...
        if self.context.telegram_client:
             self._my_id = await self.context.telegram_client.get_my_id()
             self._my_username = await self.context.telegram_client.get_my_username()
        self.info("【自动传功】TG客户端已启动，延迟后执行首次检查...")
        await asyncio.sleep(random.uniform(20, 40)) # 随机延迟
        await _check_sect_teach()

    async def handle_command_sent(self, sent_message: Message, command_text: str):
        """监听占位消息发送成功，然后发送回复指令"""
        # ... (逻辑不变，pending_key 已包含 user_id) ...
        if not self._my_id:
             self._my_id = await self.context.telegram_client.get_my_id()
             if not self._my_id: self.error("【自动传功】handle_command_sent: 无法获取 User ID！"); return
        if not self.context.redis: return
        redis_client = self.context.redis.get_client()
        if not redis_client: return

        is_placeholder = command_text in self.placeholder_messages
        pending_key = f"{REDIS_PENDING_PLACEHOLDER_KEY_PREFIX}:{self._my_id}"
        is_pending = False; deleted_pending_key = False

        if is_placeholder:
            try:
                pending_value = await redis_client.get(pending_key)
                if pending_value:
                    is_pending = True; await redis_client.delete(pending_key); deleted_pending_key = True
                    self.info(f"【自动传功】检测到占位符 '{command_text}' 发送，且 Redis 标记存在，已清除标记。")
                else: self.warning(f"【自动传功】检测到占位符 '{command_text}' 发送，但 Redis 标记不存在或已过期，忽略。")
            except Exception as e: self.error(f"【自动传功】检查或删除 Redis 占位符等待标记时出错: {e}"); return

        if is_placeholder and is_pending and deleted_pending_key:
            placeholder_msg_id = sent_message.id
            self.info(f"【自动传功】步骤 2: 监听到占位消息 '{command_text}' 已发送 (MsgID: {placeholder_msg_id})。")
            delay = self.reply_delay_seconds
            self.info(f"【自动传功】步骤 3: 计划在 {delay:.1f} 秒后将传功指令 '{TEACH_COMMAND}' (回复 {placeholder_msg_id}) 加入队列...")

            async def add_reply_to_queue_task(p_msg_id, delay_sec):
                await asyncio.sleep(delay_sec)
                command_with_reply_info = f"{TEACH_COMMAND} --reply_to {p_msg_id}"
                self.info(f"【自动传功】延迟结束，将传功指令 '{command_with_reply_info}' 加入队列...")
                success = await self.context.telegram_client.send_game_command(command_with_reply_info)
                if success: self.info(f"【自动传功】传功指令 '{command_with_reply_info}' 已成功加入队列。")
                else: self.error("【自动传功】将传功指令加入队列失败。")

            asyncio.create_task(add_reply_to_queue_task(placeholder_msg_id, delay))
            return
        elif command_text.startswith(TEACH_COMMAND): # 检查是否是传功指令本身（带回复标记的）
            self.info(f"【自动传功】步骤 4: 监听到传功指令 '{command_text.split('--reply_to')[0].strip()}' 已发送 (MsgID: {sent_message.id})。等待游戏响应...")


    async def handle_game_response(self, message: Message, is_reply_to_me: bool, is_mentioning_me: bool):
        """处理游戏响应，确认传功是否成功，并可能触发下一次"""
        # ... (逻辑不变) ...
        if not self.auto_enabled or not self.context.data_manager or not self.context.event_bus or not self.context.redis: return
        text = message.text or message.caption
        if not text or not is_reply_to_me: return
        is_success_reply = "传功玉简已记录！" in text and "今日已传功" in text
        is_fail_or_limit_reply = ("传功失败" in text or "次数已用完" in text or "无法传功" in text or "今日传功次数已达上限" in text or "过于频繁" in text)
        if not is_success_reply and not is_fail_or_limit_reply: return

        teach_cmd_msg_id = message.reply_to_message_id
        current_done_count = 0
        if is_success_reply:
            match = re.search(r"今日已传功 (\d+)/3 次", text)
            if match:
                try: current_done_count = int(match.group(1)); self.info(f"【自动传功】从成功回复中解析到已完成次数: {current_done_count}")
                except ValueError: self.warning(f"【自动传功】无法从回复 '{text[:50]}...' 中解析数字次数。")
            else:
                self.warning(f"【自动传功】成功回复 '{text[:50]}...' 中未找到 'x/3 次' 模式。将仅触发同步。")
                current_done_count = 3 # 假设已完成

        should_trigger_next = False; sync_reason = "未知"; send_completion_notify = False

        if is_success_reply and current_done_count > 0:
            if current_done_count < 3:
                self.info(f"【自动传功】收到【成功】回复 ({current_done_count}/3)！准备触发同步并启动下一次。")
                should_trigger_next = True; sync_reason = f"成功完成第 {current_done_count} 次传功"
            else: # current_done_count == 3
                self.info(f"【自动传功】收到【成功】回复 ({current_done_count}/3)！今日次数已完成。准备触发同步并发送通知。")
                should_trigger_next = False; sync_reason = "成功完成全部 3 次传功"
                send_completion_notify = True
        elif is_fail_or_limit_reply:
            self.warning(f"【自动传功】收到【失败或次数用尽】回复: {text[:50]}...")
            should_trigger_next = False
            if "次数已用完" in text or "已达上限" in text or "过于频繁" in text:
                 self.info("【自动传功】检测到次数已用尽或操作过于频繁。准备触发同步并发送通知。")
                 sync_reason = "传功次数已用尽或过于频繁"
                 send_completion_notify = True
            else:
                 sync_reason = "传功失败"

        self.info(f"【自动传功】触发角色同步 (原因: {sync_reason})...")
        try:
            await self.context.event_bus.emit("trigger_character_sync_now")
            self.info("【自动传功】角色同步事件已发出。")
        except Exception as sync_e: self.error(f"【自动传功】尝试触发角色同步时出错: {sync_e}", exc_info=True)

        if send_completion_notify:
            redis_client = self.context.redis.get_client()
            if redis_client and self._my_id:
                local_tz = await _get_local_timezone(self.config); current_date_str = datetime.now(local_tz).strftime("%Y-%m-%d")
                notify_key = f"{REDIS_TEACH_COMPLETED_NOTIFY_KEY}:{self._my_id}:{current_date_str}"
                try:
                    notify_sent = await redis_client.set(notify_key, "1", ex=25*3600, nx=True)
                    if notify_sent:
                        self.info(f"【自动传功】今日传功首次确认完成，发送通知。")
                        notify_msg = f"✅ [{self._my_username or self._my_id}] 今日宗门传功已全部完成 (3/3)。"
                        await self.context.event_bus.emit("send_system_notification", notify_msg)
                    else:
                        self.info("【自动传功】今日传功完成通知已发送过，跳过。")
                except Exception as notify_err:
                    self.error(f"【自动传功】检查或发送完成通知时出错: {notify_err}")

        if should_trigger_next:
            try:
                min_delay = float(self.next_teach_delay_range[0]); max_delay = float(self.next_teach_delay_range[1]); delay = random.uniform(min_delay, max_delay)
                self.info(f"【自动传功】将在 {delay:.1f} 秒后尝试启动下一次传功...")
                await asyncio.sleep(delay)
                await _initiate_teach_sequence(self.context)
            except Exception as e_next: self.error(f"【自动传功】在成功响应后尝试启动下一次传功序列时出错: {e_next}")

        self.info("【自动传功】本次传功回复处理完毕。")


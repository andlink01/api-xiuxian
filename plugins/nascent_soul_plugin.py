import logging
import asyncio
import random
import pytz
import re
from datetime import datetime, timedelta
from typing import Optional, Dict
from plugins.base_plugin import BasePlugin, AppContext
from core.context import get_global_context
from plugins.character_sync_plugin import format_local_time # 导入时间处理
from apscheduler.jobstores.base import JobLookupError
from pyrogram.types import Message

# --- 常量 ---
NASCENT_SOUL_JOB_ID = 'auto_nascent_soul_job' # 唯一的智能调度任务
NASCENT_SOUL_STARTUP_JOB_ID = 'auto_nascent_soul_startup_check' # 启动时的一次性检查
NASCENT_SOUL_TIMEOUT_JOB_ID = 'auto_nascent_soul_timeout' # 状态检查的超时任务

CMD_STATUS = ".元婴状态"
CMD_EGRESS = ".元婴出窍"
SETTLEMENT_TRIGGER_MESSAGES = [
    "嗯", "唔", "...", "。。。", "收到", "好的",
    "嗯嗯", "哦", "ok", "k", "行", ".", "可",
]

# 状态
STATUS_NOURISHING = "窍中温养"
STATUS_EGRESS = "元神出窍"
STATUS_CLOSED_DOOR = "元婴闭关"

# Redis Keys
# --- 修改: 锁 Key 包含 user_id 占位符 ---
ACTION_LOCK_KEY_FORMAT = "nascent_soul:action_lock:{}" # 操作锁格式
# --- 修改结束 ---
ACTION_LOCK_TTL = 120 # 2分钟
WAITING_STATUS_KEY_PREFIX = "nascent_soul:waiting_status:" # 等待状态回复 (已包含 user_id)
WAITING_STATUS_TTL = 120 # 2分钟

# 正则 (保持不变)
REGEX_STATUS_NOURISHING = re.compile(rf"状态: {STATUS_NOURISHING}")
REGEX_STATUS_EGRESS = re.compile(rf"状态: {STATUS_EGRESS}")
REGEX_STATUS_CLOSED_DOOR = re.compile(rf"状态: {STATUS_CLOSED_DOOR}")
REGEX_EGRESS_COUNTDOWN = re.compile(r"归来倒计时: (.+)")
REGEX_EGRESS_SUCCESS = re.compile(r"元婴化作一道流光飞出")
REGEX_EGRESS_ALREADY = re.compile(r"正在执行“元神出窍”任务")
REGEX_SETTLEMENT = re.compile(r"【元神归窍】")
REQUIRED_REALMS = ["元婴", "化神"]

# --- 辅助：解析中文倒计时 (保持不变) ---
def parse_countdown_to_seconds(countdown_str: str) -> Optional[int]:
    seconds = 0
    try:
        hour_match = re.search(r"(\d+)小时", countdown_str)
        min_match = re.search(r"(\d+)分钟", countdown_str)
        sec_match = re.search(r"(\d+)秒", countdown_str)
        if hour_match: seconds += int(hour_match.group(1)) * 3600
        if min_match: seconds += int(min_match.group(1)) * 60
        if sec_match: seconds += int(sec_match.group(1))
        return seconds if hour_match or min_match or sec_match else None
    except Exception as e:
        logging.getLogger("NascentSoulPlugin.Parse").error(f"解析倒计时字符串 '{countdown_str}' 失败: {e}")
        return None

# --- 顶级任务函数 ---
async def _send_status_check_command(is_startup_check: bool = False):
    """(APScheduler 目标) 发送 .元婴状态 指令"""
    logger = logging.getLogger("NascentSoulPlugin.Job")
    log_prefix = "【自动元婴】(启动检查)" if is_startup_check else "【自动元婴】(周期检查)"

    context = get_global_context()
    if not context or not context.telegram_client or not context.redis or not context.scheduler or not context.data_manager:
        logger.error(f"{log_prefix} 无法执行：核心服务不可用。")
        return

    if not context.config.get("nascent_soul.auto_enabled", False):
        logger.info(f"{log_prefix} 已被禁用，跳过。")
        return

    redis_client = context.redis.get_client()
    my_id = context.telegram_client._my_id
    if not my_id or not redis_client:
        logger.warning(f"{log_prefix} 无法获取 User ID 或 Redis 客户端，跳过。")
        return

    # --- (境界检查逻辑不变) ---
    logger.debug(f"{log_prefix} 正在获取角色状态缓存以检查境界...")
    char_status = await context.data_manager.get_character_status(my_id, use_cache=True) # 使用缓存
    if not char_status:
        logger.warning(f"{log_prefix} 无法获取角色状态缓存，暂时跳过检查。")
    else:
        current_realm = char_status.get("cultivation_level")
        if not current_realm:
            logger.warning(f"{log_prefix} 无法从缓存中获取当前境界，暂时跳过检查。")
        elif not any(current_realm.startswith(req) for req in REQUIRED_REALMS):
            logger.info(f"{log_prefix} 当前境界 '{current_realm}' 未达到要求 ({'/'.join(REQUIRED_REALMS)})，跳过本次检查。")
            return
        else:
            logger.debug(f"{log_prefix} 境界 '{current_realm}' 符合要求，继续执行。")

    lock_acquired = False
    wait_key = f"{WAITING_STATUS_KEY_PREFIX}{my_id}"
    # --- 修改: 格式化锁 Key ---
    lock_key = ACTION_LOCK_KEY_FORMAT.format(my_id)
    # --- 修改结束 ---

    try:
        # --- 修改: 使用格式化后的 lock_key ---
        lock_acquired = await redis_client.set(lock_key, "1", ex=ACTION_LOCK_TTL, nx=True)
        if not lock_acquired:
            logger.info(f"{log_prefix} 获取操作锁 ({lock_key}) 失败，上次操作可能仍在进行中，跳过。")
            return
        # --- 修改结束 ---

        logger.info(f"{log_prefix} 成功获取操作锁 ({lock_key})，正在发送 '{CMD_STATUS}' 指令...")
        await redis_client.set(wait_key, "1", ex=WAITING_STATUS_TTL) # 等待标记 Key 不变

        success = await context.telegram_client.send_game_command(CMD_STATUS)
        if success:
            logger.info(f"{log_prefix} 指令 '{CMD_STATUS}' 已加入队列。设置超时任务...")
            timeout_run_at = datetime.now(pytz.utc) + timedelta(seconds=WAITING_STATUS_TTL)
            context.scheduler.add_job(
                _handle_status_timeout,
                trigger='date',
                run_date=timeout_run_at,
                id=NASCENT_SOUL_TIMEOUT_JOB_ID,
                replace_existing=True,
                misfire_grace_time=30
            )
            lock_acquired = False # 锁由响应处理或超时处理释放
        else:
            logger.error(f"{log_prefix} 将状态指令加入队列失败。")
            await redis_client.delete(wait_key)
            # 锁会在 finally 中释放

    except Exception as e:
        logger.error(f"{log_prefix} 执行时出错: {e}", exc_info=True)
        if redis_client and my_id:
             try: await redis_client.delete(wait_key)
             except Exception: pass
        # 锁会在 finally 中释放

    finally:
        # --- 修改: 使用格式化后的 lock_key ---
        if lock_acquired and redis_client:
             logger.debug(f"{log_prefix} 任务异常或发送失败，释放锁 ({lock_key})。")
             await redis_client.delete(lock_key)
        # --- 修改结束 ---

async def _handle_status_timeout():
    """处理状态检查回复超时"""
    logger = logging.getLogger("NascentSoulPlugin.Timeout")
    logger.warning("【自动元婴】状态检查回复超时！")

    context = get_global_context()
    if not context or not context.redis or not context.scheduler or not context.telegram_client:
        logger.error("【自动元婴】[超时] 无法获取核心服务。")
        return

    redis_client = context.redis.get_client()
    my_id = context.telegram_client._my_id
    if not my_id or not redis_client:
        logger.error("【自动元婴】[超时] 无法获取 User ID 或 Redis。")
        return

    # --- 修改: 格式化锁 Key ---
    lock_key = ACTION_LOCK_KEY_FORMAT.format(my_id)
    # --- 修改结束 ---
    await redis_client.delete(f"{WAITING_STATUS_KEY_PREFIX}{my_id}")
    # --- 修改: 使用格式化后的 lock_key ---
    await redis_client.delete(lock_key)
    logger.info(f"【自动元婴】[超时] 已清理等待标记和操作锁 ({lock_key})。")
    # --- 修改结束 ---

    try:
        # ... (安排重试逻辑不变) ...
        recheck_range = context.config.get("nascent_soul.recheck_interval_range_minutes", [25, 35])
        recheck_minutes = random.uniform(recheck_range[0], recheck_range[1])
        next_run = datetime.now(pytz.utc) + timedelta(minutes=recheck_minutes)
        logger.info(f"【自动元婴】[超时] 安排在 {recheck_minutes:.1f} 分钟后重试检查。")
        await _schedule_next_check(context, next_run)
    except Exception as e:
        logger.error(f"【自动元婴】[超时] 安排重试失败: {e}", exc_info=True)

async def _schedule_next_check(context: AppContext, run_time: datetime):
    """辅助函数：在 APScheduler 中安排下一次状态检查"""
    # ... (逻辑不变) ...
    logger = logging.getLogger("NascentSoulPlugin.Scheduler")
    if not context.scheduler:
        logger.error("【自动元婴】无法安排下次任务：Scheduler 不可用。")
        return
    try:
        context.scheduler.add_job(
            _send_status_check_command,
            trigger='date',
            run_date=run_time,
            id=NASCENT_SOUL_JOB_ID, # 使用主任务 ID
            args=[False], # 明确标记为非启动检查
            replace_existing=True,
            misfire_grace_time=300
        )
        logger.info(f"【自动元婴】已成功安排下次检查时间: {format_local_time(run_time)}")
    except Exception as e:
        logger.error(f"【自动元婴】安排下次任务失败: {e}", exc_info=True)

# --- 插件类 ---
class Plugin(BasePlugin):
    """自动执行元婴出窍任务 (基于指令交互)"""
    def __init__(self, context: AppContext, plugin_name: str, cn_name: str | None = None):
        super().__init__(context, plugin_name, cn_name)
        # ... (初始化逻辑不变) ...
        self.load_config()
        self._my_id: Optional[int] = None
        self._my_username: Optional[str] = None
        self.auto_enabled = self.config.get("nascent_soul.auto_enabled", False)
        recheck_range_cfg = self.config.get("nascent_soul.recheck_interval_range_minutes", [25, 35])
        if isinstance(recheck_range_cfg, list) and len(recheck_range_cfg) == 2 and all(isinstance(x, (int, float)) for x in recheck_range_cfg) and recheck_range_cfg[0] <= recheck_range_cfg[1] and recheck_range_cfg[0] >= 1:
            self.min_recheck = recheck_range_cfg[0]
            self.max_recheck = recheck_range_cfg[1]
        else:
            self.warning(f"配置 nascent_soul.recheck_interval_range_minutes ({recheck_range_cfg}) 格式无效，使用默认值 [25, 35]")
            self.min_recheck = 25
            self.max_recheck = 35
        self.egress_hours = self.config.get("nascent_soul.egress_hours", 8)
        buffer_range = self.config.get("nascent_soul.schedule_buffer_minutes", [2, 5])
        self.min_buffer = buffer_range[0]
        self.max_buffer = buffer_range[1]

        if self.auto_enabled:
            self.info(f"插件已加载并启用。闭关状态重查间隔: {self.min_recheck}-{self.max_recheck} 分钟。")
        else:
            self.info("插件已加载但未启用。")

    def load_config(self):
        # (此插件的 load_config 当前为空，可以在 __init__ 中直接加载)
        pass

    def register(self):
        """注册定时检查任务和事件监听器"""
        # ... (注册逻辑不变) ...
        if not self.auto_enabled:
            return
        try:
            if self.scheduler:
                self.event_bus.on("telegram_client_started", self.initial_check_and_schedule)
                self.event_bus.on("game_response_received", self.handle_game_response)
                self.info("已注册元婴出窍相关的启动和游戏响应事件监听器。")
            else:
                 self.error("无法注册元婴出窍功能：Scheduler 不可用。")
        except Exception as e:
            self.error(f"注册元婴出窍功能时出错: {e}", exc_info=True)

    async def initial_check_and_schedule(self):
        """启动时执行一次检查"""
        # ... (逻辑不变) ...
        self.info("【自动元婴】TG客户端已启动，准备启动时检查...")
        if self.context.telegram_client:
             self._my_id = await self.context.telegram_client.get_my_id()
             self._my_username = await self.context.telegram_client.get_my_username()
        if not self._my_id:
             self.error("【自动元婴】无法获取 User ID，无法启动检查。"); return

        job = None
        try:
            if self.scheduler: job = self.scheduler.get_job(NASCENT_SOUL_JOB_ID)
        except JobLookupError: job = None
        except Exception as e: self.error(f"【自动元婴】启动时检查 APScheduler 任务失败: {e}"); job = None

        if job:
            self.info(f"【自动元婴】启动检查：找到现存的调度任务，下次运行: {format_local_time(job.next_run_time)}。")
        else:
            self.info("【自动元婴】启动检查：未找到调度任务。安排一次性状态检查...")
            if self.scheduler:
                 run_at = datetime.now(pytz.utc) + timedelta(seconds=random.uniform(60, 90))
                 self.scheduler.add_job(
                     _send_status_check_command, trigger='date', run_date=run_at,
                     id=NASCENT_SOUL_STARTUP_JOB_ID, args=[True], replace_existing=True
                 )
            else: self.error("【自动元婴】启动检查：Scheduler 不可用，无法安排启动检查！")

    async def handle_game_response(self, message: Message, is_reply_to_me: bool, is_mentioning_me: bool):
        """处理游戏响应，执行出窍或触发结算"""
        if not self.auto_enabled or not is_reply_to_me: return

        text = message.text or message.caption
        if not text: return
        if not self._my_id: self.error("【自动元婴】无法获取 User ID，无法处理响应。"); return
        redis_client = self.redis.get_client()
        if not redis_client: self.error("【自动元婴】Redis 未连接，无法处理响应。"); return

        wait_key = f"{WAITING_STATUS_KEY_PREFIX}{self._my_id}"
        # --- 修改: 格式化锁 Key ---
        lock_key = ACTION_LOCK_KEY_FORMAT.format(self._my_id)
        # --- 修改结束 ---

        try:
            is_waiting_status = await redis_client.get(wait_key)
            if is_waiting_status:
                self.info(f"【自动元婴】收到对 '{CMD_STATUS}' 的回复 (MsgID: {message.id})，开始解析...")

                await redis_client.delete(wait_key)
                try:
                    if self.scheduler: await asyncio.to_thread(self.scheduler.remove_job, NASCENT_SOUL_TIMEOUT_JOB_ID)
                except JobLookupError: pass
                except Exception as e_rem: self.warning(f"移除超时任务 {NASCENT_SOUL_TIMEOUT_JOB_ID} 失败: {e_rem}")

                if REGEX_STATUS_NOURISHING.search(text):
                    self.info(f"【自动元婴】状态: {STATUS_NOURISHING}。尝试发送出窍指令...")
                    # ... (发送前境界检查逻辑不变) ...
                    can_egress = False
                    char_status_now = await self.context.data_manager.get_character_status(self._my_id, use_cache=True)
                    if char_status_now:
                        realm_now = char_status_now.get("cultivation_level")
                        if realm_now and any(realm_now.startswith(req) for req in REQUIRED_REALMS):
                            can_egress = True
                        else:
                            self.info(f"【自动元婴】(发送前检查) 境界 '{realm_now}' 不满足要求，取消发送。")
                    else:
                        self.warning("【自动元婴】(发送前检查) 无法获取角色状态缓存，暂时允许发送。")
                        can_egress = True

                    if can_egress:
                        success = await self.context.telegram_client.send_game_command(CMD_EGRESS)
                        if success: self.info(f"【自动元婴】指令 '{CMD_EGRESS}' 已加入队列。")
                        else: self.error("【自动元婴】将出窍指令加入队列失败。"); await redis_client.delete(lock_key) # 发送失败，释放锁
                    else: # 境界不满足
                        await redis_client.delete(lock_key) # 释放锁
                        self.info("【自动元婴】(温养) 因境界不足取消出窍，操作锁已释放。")
                    # 发送成功后，锁由后续的成功/失败响应处理

                elif REGEX_STATUS_EGRESS.search(text):
                    # ... (出窍中，安排下次检查逻辑不变) ...
                    self.info(f"【自动元婴】状态: {STATUS_EGRESS}。")
                    countdown_sec = 0
                    countdown_match = REGEX_EGRESS_COUNTDOWN.search(text)
                    if countdown_match:
                        countdown_str = countdown_match.group(1)
                        parsed_sec = parse_countdown_to_seconds(countdown_str)
                        if parsed_sec: countdown_sec = parsed_sec
                        self.info(f"【自动元婴】仍在出窍，解析到剩余 {countdown_str} ({countdown_sec}s)。")

                    if countdown_sec > 0:
                        buffer = timedelta(minutes=random.uniform(self.min_buffer, self.max_buffer))
                        next_run = datetime.now(pytz.utc) + timedelta(seconds=countdown_sec) + buffer
                        await _schedule_next_check(self.context, next_run)
                        await redis_client.delete(lock_key) # 状态确认，释放锁
                    else: # 倒计时结束或未找到
                        trigger_msg = random.choice(SETTLEMENT_TRIGGER_MESSAGES)
                        self.info(f"【自动元婴】出窍倒计时结束或未找到，发送 '{trigger_msg}' 触发结算...")
                        success = await self.context.telegram_client.send_game_command(trigger_msg)
                        if not success: await redis_client.delete(lock_key) # 发送失败，释放锁
                        # 发送成功，锁由结算消息处理

                elif REGEX_STATUS_CLOSED_DOOR.search(text):
                    # ... (闭关中，安排下次检查逻辑不变) ...
                    self.info(f"【自动元婴】状态: {STATUS_CLOSED_DOOR}。等待下次检查。")
                    recheck_minutes = random.uniform(self.min_recheck, self.max_recheck)
                    next_run = datetime.now(pytz.utc) + timedelta(minutes=recheck_minutes)
                    self.info(f"【自动元婴】(闭关中) 安排在 {recheck_minutes:.1f} 分钟后再次检查。")
                    await _schedule_next_check(self.context, next_run)
                    await redis_client.delete(lock_key) # 状态确认，释放锁

                else: # 解析失败
                    self.warning(f"【自动元婴】无法解析 '{CMD_STATUS}' 的回复内容。安排重试。")
                    recheck_minutes = random.uniform(self.min_recheck, self.max_recheck)
                    next_run = datetime.now(pytz.utc) + timedelta(minutes=recheck_minutes)
                    self.info(f"【自动元婴】(解析失败) 安排在 {recheck_minutes:.1f} 分钟后重试检查。")
                    await _schedule_next_check(self.context, next_run)
                    await redis_client.delete(lock_key) # 状态确认，释放锁

            elif REGEX_EGRESS_SUCCESS.search(text):
                # ... (出窍成功，安排下次检查逻辑不变) ...
                self.info(f"【自动元婴】收到 '{CMD_EGRESS}' 成功回复。")
                buffer = timedelta(minutes=random.uniform(self.min_buffer, self.max_buffer))
                next_run = datetime.now(pytz.utc) + timedelta(hours=self.egress_hours) + buffer
                await _schedule_next_check(self.context, next_run)
                await redis_client.delete(lock_key) # 操作完成，释放锁
                self.info(f"【自动元婴】(出窍成功) 操作锁 ({lock_key}) 已释放。")

            elif REGEX_EGRESS_ALREADY.search(text):
                # ... (已在出窍，安排下次检查逻辑不变) ...
                self.info(f"【自动元婴】收到 '{CMD_EGRESS}' 失败回复（已在出窍）。")
                recheck_minutes = random.uniform(self.min_recheck, self.max_recheck)
                next_run = datetime.now(pytz.utc) + timedelta(minutes=recheck_minutes)
                self.info(f"【自动元婴】(已在出窍) 安排在 {recheck_minutes:.1f} 分钟后再次检查。")
                await _schedule_next_check(self.context, next_run)
                await redis_client.delete(lock_key) # 状态确认，释放锁
                self.info(f"【自动元婴】(已在出窍) 操作锁 ({lock_key}) 已释放。")

            elif REGEX_SETTLEMENT.search(text) and message.edit_date:
                # ... (收到结算消息，尝试立即再次出窍逻辑不变) ...
                self.info(f"【自动元婴】检测到【元神归窍】结算消息 (MsgID: {message.id})。")
                self.info("【自动元婴】归窍后尝试立即开始下一次出窍...")
                # --- 修改: 使用格式化的 lock_key ---
                lock_acquired_settle = await redis_client.set(lock_key, "1", ex=ACTION_LOCK_TTL, nx=True)
                # --- 修改结束 ---
                if lock_acquired_settle:
                    self.info(f"【自动元婴】(归窍后) 成功获取锁 ({lock_key})，发送 '.元婴出窍' 指令。")
                    # ... (发送前境界检查逻辑不变) ...
                    can_egress_settle = False
                    char_status_settle = await self.context.data_manager.get_character_status(self._my_id, use_cache=True)
                    if char_status_settle:
                        realm_settle = char_status_settle.get("cultivation_level")
                        if realm_settle and any(realm_settle.startswith(req) for req in REQUIRED_REALMS):
                            can_egress_settle = True
                        else:
                            self.info(f"【自动元婴】(归窍后检查) 境界 '{realm_settle}' 不满足要求，取消发送。")
                    else:
                        self.warning("【自动元婴】(归窍后检查) 无法获取角色状态缓存，暂时允许发送。")
                        can_egress_settle = True

                    if can_egress_settle:
                        success = await self.context.telegram_client.send_game_command(CMD_EGRESS)
                        if not success:
                            self.error("【自动元婴】(归窍后) 发送出窍指令失败！")
                            await redis_client.delete(lock_key) # 发送失败，释放锁
                        # 发送成功，锁由后续响应处理
                    else: # 境界不满足
                         await redis_client.delete(lock_key) # 释放锁
                         self.info("【自动元婴】(归窍后) 因境界不足取消出窍，操作锁已释放。")
                else:
                    self.info("【自动元婴】(归窍后) 无法获取操作锁，等待下个周期检查。")

        except Exception as e:
            self.error(f"【自动元婴】处理游戏响应时出错: {e}", exc_info=True)
            # 尝试清理锁和状态
            try:
                if redis_client and self._my_id:
                    lock_key_err = ACTION_LOCK_KEY_FORMAT.format(self._my_id)
                    wait_key_err = f"{WAITING_STATUS_KEY_PREFIX}{self._my_id}"
                    await redis_client.delete(lock_key_err)
                    await redis_client.delete(wait_key_err)
            except Exception as clean_err:
                 self.error(f"【自动元婴】在响应处理异常后清理状态失败: {clean_err}")


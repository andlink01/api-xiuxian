import logging
import asyncio
import random
import pytz
import json
from datetime import datetime, timedelta
from typing import Optional
from plugins.base_plugin import BasePlugin, AppContext
from pyrogram.types import Message
from plugins.character_sync_plugin import parse_iso_datetime, format_local_time # 时间处理工具
from apscheduler.jobstores.base import JobLookupError
from core.context import get_global_context

# --- 常量 ---
YINDAO_JOB_ID = 'auto_yindao_job' # 周期检查任务 ID
YINDAO_TIMEOUT_JOB_ID = 'yindao_timeout_job' # 超时检查任务 ID
REDIS_YINDAO_WAITING_KEY_PREFIX = "yindao_waiting_msg_id" # Redis 等待状态 Key 前缀
YINDAO_COMMAND = ".引道 水" # 引道指令
YINDAO_INTERVAL_HOURS = 12 # 引道间隔（小时）
YINDAO_SECT_NAME = "太一门" # 需要执行引道的宗门名称
YINDAO_SUCCESS_KEYWORDS = ["引道成功", "获得", "水之精华"] # 引道成功的关键词

async def _trigger_yindao_command():
    """由 APScheduler 调度的函数，用于将引道指令加入队列"""
    logger = logging.getLogger("YindaoPlugin.TriggerCmd")
    logger.info(f"【自动引道】定时任务触发：准备将 '{YINDAO_COMMAND}' 加入队列...")
    context = get_global_context()
    if not context or not context.telegram_client or not context.redis:
        logger.error("【自动引道】无法执行：核心服务 (TGClient/Redis) 不可用。")
        return
    config = context.config; auto_enabled = config.get("yindao.auto_enabled", False)
    if not auto_enabled: logger.info("【自动引道】已被禁用，取消本次执行。"); return
    my_id = context.telegram_client._my_id
    if not my_id: logger.error("【自动引道】无法获取助手 User ID，无法检查等待状态，暂时跳过。"); return
    redis_client = context.redis.get_client()
    if not redis_client: logger.error("【自动引道】Redis 未连接，无法检查等待状态，暂时跳过。"); return
    redis_key = f"{REDIS_YINDAO_WAITING_KEY_PREFIX}:{my_id}"
    try:
        waiting_msg_id = await redis_client.get(redis_key)
        if waiting_msg_id: logger.warning(f"【自动引道】尝试发送指令，但仍在等待 MsgID: {waiting_msg_id} 的响应，本次跳过。"); return
        logger.info("【自动引道】检查 Redis 状态：当前非等待状态，可以发送指令。")
    except Exception as e: logger.error(f"【自动引道】检查 Redis 等待状态时出错: {e}，为安全起见跳过本次。"); return
    try:
        logger.info(f"【自动引道】正在将指令 '{YINDAO_COMMAND}' 加入发送队列...")
        success = await context.telegram_client.send_game_command(YINDAO_COMMAND)
        if success: logger.info(f"【自动引道】指令 '{YINDAO_COMMAND}' 已成功加入发送队列。等待 'game_command_sent' 事件以设置超时。")
        else: logger.error("【自动引道】将引道指令加入队列失败。等待下次调度检查。")
    except Exception as e: logger.error(f"【自动引道】将引道指令加入队列时出错: {e}", exc_info=True)

async def _handle_yindao_timeout():
    """由 APScheduler 调度的函数，用于处理等待引道响应超时"""
    logger = logging.getLogger("YindaoPlugin.Timeout")
    logger.info("【自动引道】超时任务触发：检查引道响应是否超时...")
    context = get_global_context()
    if not context or not context.redis or not context.telegram_client or not context.event_bus:
        logger.error("【自动引道】无法处理超时：核心服务不可用。"); return
    my_id = context.telegram_client._my_id
    if not my_id: logger.error("【自动引道】无法获取助手 User ID，无法处理超时状态。"); return
    redis_client = context.redis.get_client()
    if not redis_client: logger.error("【自动引道】Redis 未连接，无法处理超时状态。"); return
    redis_key = f"{REDIS_YINDAO_WAITING_KEY_PREFIX}:{my_id}"
    try:
        logger.info(f"【自动引道】正在检查 Redis 等待状态 (Key: {redis_key})...")
        waiting_msg_id_str = await redis_client.get(redis_key)
        if waiting_msg_id_str:
            logger.warning(f"【自动引道】确认超时！等待引道响应 (针对 MsgID: {waiting_msg_id_str}) 超时。清除状态。")
            await redis_client.delete(redis_key)
            logger.info("【自动引道】超时状态已从 Redis 清除。")
            logger.info("【自动引道】超时后触发角色数据同步...")
            try: await context.event_bus.emit("trigger_character_sync_now")
            except Exception as sync_e: logger.error(f"【自动引道】超时后尝试触发角色同步时出错: {sync_e}", exc_info=True)
        else: logger.info("【自动引道】超时任务触发，但 Redis 中已无等待状态，忽略。")
    except Exception as e:
        logger.error(f"【自动引道】处理引道超时状态时出错: {e}", exc_info=True)
        logger.warning("【自动引道】因超时处理出错，尝试清除状态并触发同步。")
        try:
            if redis_client: await redis_client.delete(redis_key)
            if context and context.event_bus: await context.event_bus.emit("trigger_character_sync_now")
        except Exception as cleanup_e:
            logger.error(f"【自动引道】在超时错误处理中尝试清理和同步失败: {cleanup_e}")


async def _check_yindao_status():
    """由 APScheduler 调度的函数，检查是否需要执行引道"""
    logger = logging.getLogger("YindaoPlugin.CheckStatus")
    logger.info("【自动引道】周期检查任务启动：检查是否需要执行引道...")
    context = get_global_context()
    if not context or not context.data_manager or not context.scheduler or not context.telegram_client:
        logger.error("【自动引道】无法检查：核心服务 (DataManager/Scheduler/TGClient) 不可用。")
        return

    config = context.config; scheduler = context.scheduler; data_manager = context.data_manager
    auto_enabled = config.get("yindao.auto_enabled", False)
    if not auto_enabled: logger.info("【自动引道】已被禁用，跳过检查。"); return

    char_status = None; sect_info = None; my_id = context.telegram_client._my_id
    if not my_id: logger.warning("【自动引道】无法获取助手 User ID，跳过本次检查。"); return

    try:
        logger.info("【自动引道】正在通过 DataManager 获取角色状态和宗门缓存...")
        # 同时获取两种缓存
        char_status = await data_manager.get_character_status(my_id, use_cache=True)
        sect_info = await data_manager.get_sect_info(my_id, use_cache=True) # <-- 改为读取 sect_info

        # --- 新增日志 ---
        logger.info(f"【自动引道】获取到的 char_status 数据: {char_status}")
        logger.info(f"【自动引道】获取到的 sect_info 数据: {sect_info}") # <-- 添加 sect_info 日志
        # --- 新增结束 ---

        if not char_status: logger.warning("【自动引道】无法从 DataManager 获取角色状态缓存，跳过本次检查。"); return
        if not sect_info: logger.warning("【自动引道】无法从 DataManager 获取宗门信息缓存，跳过本次检查。"); return # <-- 检查 sect_info

        logger.info("【自动引道】角色状态和宗门缓存获取成功。")
    except Exception as e: logger.error(f"【自动引道】通过 DataManager 获取缓存时发生未知错误: {e}", exc_info=True); return

    # --- 修改: 从 sect_info 获取宗门名称 ---
    sect_name = sect_info.get("sect_name") if isinstance(sect_info, dict) else None
    # --- 修改结束 ---

    if sect_name != YINDAO_SECT_NAME: logger.info(f"【自动引道】角色宗门为 '{sect_name}' (不是 {YINDAO_SECT_NAME})，跳过检查。"); return

    # --- 继续从 char_status 获取引道冷却时间 ---
    last_yindao_str = char_status.get("last_yindao_time"); logger.info(f"【自动引道】获取到上次引道时间: {last_yindao_str}")
    # --- 获取结束 ---

    now_utc = datetime.now(pytz.utc); can_execute = False; next_run_time_utc = None
    interval_seconds = YINDAO_INTERVAL_HOURS * 3600; random_buffer = random.uniform(5, 60)

    if not last_yindao_str:
        logger.info("【自动引道】缓存显示从未执行过引道或记录丢失，可以立即执行。")
        can_execute = True; next_run_time_utc = now_utc + timedelta(seconds=random_buffer)
    else:
        last_yindao_dt_utc = parse_iso_datetime(last_yindao_str)
        if last_yindao_dt_utc:
            next_available_time_utc = last_yindao_dt_utc + timedelta(seconds=interval_seconds + random_buffer)
            # --- 修改: 从 char_status 获取格式化时间 ---
            last_yindao_formatted = char_status.get("last_yindao_time_formatted", format_local_time(last_yindao_dt_utc))
            # --- 修改结束 ---
            logger.info(f"【自动引道】上次引道时间: {last_yindao_formatted}, 计算下次可执行时间 (含随机延迟): {format_local_time(next_available_time_utc)}")
            if now_utc >= next_available_time_utc:
                logger.info("【自动引道】当前时间已到达或超过下次可执行时间，可以执行。")
                can_execute = True; next_run_time_utc = now_utc + timedelta(seconds=random.uniform(1, 5))
            else: logger.info("【自动引道】冷却时间未到，无需操作。"); can_execute = False
        else: logger.error(f"【自动引道】无法解析上次引道时间戳: '{last_yindao_str}'，本周期跳过。"); can_execute = False

    if can_execute and next_run_time_utc and scheduler:
        logger.info(f"【自动引道】准备安排一次性任务在 {format_local_time(next_run_time_utc)} 执行引道指令...")
        try:
            scheduler.add_job(
                _trigger_yindao_command, trigger='date', run_date=next_run_time_utc,
                id=YINDAO_JOB_ID + "_trigger", replace_existing=True, misfire_grace_time=60
            ); logger.info("【自动引道】一次性指令触发任务安排成功。")
        except Exception as e: logger.error(f"【自动引道】安排一次性指令触发任务失败: {e}", exc_info=True)
    elif can_execute and not scheduler: logger.error("【自动引道】判断可以执行，但无法安排任务：Scheduler 不可用。")


# --- Plugin Class ---
class Plugin(BasePlugin):
    """自动引道插件 (太一门专属)"""
    def __init__(self, context: AppContext, plugin_name: str, cn_name: str | None = None):
        super().__init__(context, plugin_name, cn_name)
        self.load_config()
        if self.auto_enabled: self.info(f"插件已加载并启用。检查间隔: {self.check_interval_minutes} 分钟。宗门: {YINDAO_SECT_NAME}。指令: '{YINDAO_COMMAND}'")
        else: self.info("插件已加载但未启用。")

    def load_config(self):
        self.auto_enabled = self.config.get("yindao.auto_enabled", False)
        self.check_interval_minutes = self.config.get("yindao.check_interval_minutes", 10)
        self.response_timeout = self.config.get("yindao.response_timeout", 120)

    def register(self):
        if not self.auto_enabled: return
        if self.check_interval_minutes < 1: self.check_interval_minutes = 1; self.warning("引道检查间隔不能小于1分钟，已重置为1。")
        try:
            if self.scheduler:
                self.scheduler.add_job(
                    _check_yindao_status, trigger='interval', minutes=self.check_interval_minutes,
                    id=YINDAO_JOB_ID, replace_existing=True, misfire_grace_time=60
                ); self.info(f"已注册引道状态定时检查任务 (每 {self.check_interval_minutes} 分钟)。")
            else: self.error("无法注册引道定时任务：Scheduler 不可用。")
            self.event_bus.on("game_command_sent", self.handle_command_sent)
            self.event_bus.on("game_response_received", self.handle_game_response)
            self.info("已注册引道相关的 game_command_sent 和 game_response_received 事件监听器。")
        except Exception as e: self.error(f"注册引道定时任务或监听器时出错: {e}", exc_info=True)

    async def handle_command_sent(self, sent_message: Message, command_text: str):
        if command_text.strip() != YINDAO_COMMAND: return
        self.info(f"【自动引道】监听到引道指令已发送 (MsgID: {sent_message.id})，设置等待状态和超时。")
        my_id = self.context.telegram_client._my_id
        if not my_id: self.error("【自动引道】无法获取助手 User ID，无法设置等待状态！"); return
        redis_client = self.context.redis.get_client()
        if not redis_client: self.error("【自动引道】Redis 未连接，无法设置等待状态！"); return
        redis_key = f"{REDIS_YINDAO_WAITING_KEY_PREFIX}:{my_id}"; timeout_seconds = self.response_timeout
        try:
            self.info(f"【自动引道】正在设置 Redis 等待状态 (Key: {redis_key}, Value: {sent_message.id}, TTL: {timeout_seconds + 60}s)...")
            await redis_client.set(redis_key, str(sent_message.id), ex=timeout_seconds + 60)
            self.info("【自动引道】Redis 等待状态设置成功。")
            timeout_run_dt_aware = datetime.now(pytz.utc) + timedelta(seconds=timeout_seconds)
            self.info(f"【自动引道】正在安排超时检查任务 '{YINDAO_TIMEOUT_JOB_ID}' 在 {format_local_time(timeout_run_dt_aware)} 左右执行...")
            if self.scheduler:
                self.scheduler.add_job(_handle_yindao_timeout, trigger='date', run_date=timeout_run_dt_aware, id=YINDAO_TIMEOUT_JOB_ID, replace_existing=True, misfire_grace_time=10)
                self.info("【自动引道】超时检查任务安排成功。")
            else:
                 self.error("【自动引道】无法安排超时任务：Scheduler 不可用。")
                 try:
                     if redis_client:
                         await redis_client.delete(redis_key)
                 except Exception as del_err:
                     self.warning(f"无法安排超时后清理 Redis key 失败: {del_err}")
        except Exception as e:
            self.error(f"【自动引道】设置等待状态或超时任务时出错: {e}", exc_info=True)
            try:
                 if redis_client:
                     await redis_client.delete(redis_key)
            except Exception as del_err_f:
                 self.warning(f"设置出错后清理 Redis key 失败: {del_err_f}")


    async def handle_game_response(self, message: Message, is_reply_to_me: bool, is_mentioning_me: bool):
        if not self.auto_enabled: return
        text = message.text or message.caption;
        if not text or not is_reply_to_me: return
        my_id = self.context.telegram_client._my_id
        if not my_id: self.error("【自动引道】无法获取助手 User ID，无法处理响应状态。"); return
        redis_client = self.context.redis.get_client()
        if not redis_client: self.error("【自动引道】Redis 未连接，无法处理响应状态。"); return
        redis_key = f"{REDIS_YINDAO_WAITING_KEY_PREFIX}:{my_id}"
        self.debug(f"【自动引道】检查游戏响应是否与等待状态匹配 (Key: '{redis_key}')...")
        expected_command_id_str = None # 初始化
        try:
            expected_command_id_str = await redis_client.get(redis_key)
            self.debug(f"【自动引道】Redis 返回期望的指令 MsgID: '{expected_command_id_str}'")
            if not expected_command_id_str: self.debug("【自动引道】当前未处于等待引道响应状态，忽略此响应。"); return
            actual_reply_to_id = message.reply_to_message_id
            self.debug(f"【自动引道】此响应实际回复的 MsgID: {actual_reply_to_id}")
            # 确保 expected_command_id_str 是数字才比较
            if not expected_command_id_str.isdigit() or actual_reply_to_id != int(expected_command_id_str):
                 self.debug(f"【自动引道】回复的 MsgID 不符或期望 ID 无效，忽略此响应。"); return
            self.info(f"【自动引道】确认收到对引道指令 (MsgID: {expected_command_id_str}) 的回复，检查内容...")
            is_success = any(keyword in text for keyword in YINDAO_SUCCESS_KEYWORDS); is_fail = "引道失败" in text
            status_text = ""
            if is_success: status_text = "成功"; self.info(f"【自动引道】回复确认成功！清除等待状态并取消超时任务。")
            elif is_fail: status_text = "失败"; self.warning(f"【自动引道】收到对引道指令的【失败】回复: {text[:50]}...")
            else: status_text = "未知"; self.warning(f"【自动引道】收到对引道指令的回复，但不含明确成功/失败关键词: {text[:50]}...")

            # --- 清理状态 ---
            self.info(f"【自动引道】正在删除 Redis 等待状态 Key '{redis_key}'..."); await redis_client.delete(redis_key)
            self.info("【自动引道】Redis 等待状态已清除。")
            try:
                self.info(f"【自动引道】正在尝试移除超时任务 '{YINDAO_TIMEOUT_JOB_ID}'...")
                if self.scheduler: await asyncio.to_thread(self.scheduler.remove_job, YINDAO_TIMEOUT_JOB_ID)
                self.info(f"【自动引道】成功移除超时任务 '{YINDAO_TIMEOUT_JOB_ID}' (如果存在)。")
            except JobLookupError: self.info(f"【自动引道】超时任务 '{YINDAO_TIMEOUT_JOB_ID}' 未找到。")
            except Exception as e_rem: self.warning(f"【自动引道】移除超时任务失败: {e_rem}")

            # --- 触发缓存更新 ---
            self.info(f"【自动引道】收到 {status_text} 回复后触发角色数据同步...")
            try: await self.context.event_bus.emit("trigger_character_sync_now")
            except Exception as sync_e: self.error(f"【自动引道】尝试在收到回复后触发角色同步时出错: {sync_e}", exc_info=True)

            self.info("【自动引道】本次引道流程结束。")

        except ValueError: self.error(f"【自动引道】Redis 中存储的期望 MsgID '{expected_command_id_str}' 不是有效的整数！")
        except Exception as e:
            self.error(f"【自动引道】处理引道响应状态时出错: {e}", exc_info=True)
            self.warning("【自动引道】因处理响应出错，尝试清除状态并触发同步。")
            # --- 正确的清理逻辑 ---
            try:
                if redis_client and 'redis_key' in locals() and redis_key:
                    await redis_client.delete(redis_key)
                if self.context and self.context.event_bus:
                    await self.context.event_bus.emit("trigger_character_sync_now")
            except Exception as cleanup_e:
                self.error(f"【自动引道】在响应错误处理中尝试清理和同步失败: {cleanup_e}")
            # --- 清理逻辑结束 ---

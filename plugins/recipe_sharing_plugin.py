import logging
import asyncio
import random
import json
import uuid
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple, Set, Any
from plugins.base_plugin import BasePlugin, AppContext
from core.context import get_global_context # <--- 导入 get_global_context
from apscheduler.jobstores.base import JobLookupError

# --- 从 GameDataManager 导入 Key ---
from modules.game_data_manager import (
    CHAR_INVENTORY_KEY,
    CHAR_RECIPES_KEY,
    GAME_ITEMS_MASTER_KEY
)
# --- 从 Marketplace 插件导入 Key (用于发布订单) ---
from plugins.marketplace_transfer_plugin import (
    get_item_id_by_name,
    get_item_name_by_id,
    REDIS_ORDER_EXEC_LOCK_PREFIX # 复用市场购买锁前缀
)


logger = logging.getLogger(__name__)

# --- 常量 ---
RECIPE_SHARING_JOB_ID = 'recipe_sharing_job'
ACTION_LOCK_KEY_FORMAT = "recipe_sharing:action_lock:{}" # 操作锁 (防止并发)
ACTION_LOCK_TTL = 1800 # 锁 TTL (30分钟，应足够完成一轮)
DEFAULT_PAY_ITEM_NAME = "灵石" # 学生挂单时使用的支付物品
DEFAULT_PAY_QTY = 1        # 学生挂单时使用的支付数量

# --- 新增: _publish_task 作为顶层函数 ---
async def _publish_task_internal(context: AppContext, target_user_id: int, task_type: str, payload: Dict[str, Any], task_id: Optional[str] = None) -> Optional[str]:
    """辅助函数：发布任务到主任务频道 (顶层函数版本)"""
    if not context.redis or not context.config:
        logger.error(f"无法发布任务 '{task_type}' 给 {target_user_id}: Redis 或 Config 未初始化。")
        return None

    task_channel = context.config.get("communication.task_channel", "assistant_tasks")
    admin_id = context.config.get("telegram.admin_id") # 获取 Admin ID

    if not task_channel:
         logger.error(f"无法发布任务 '{task_type}' 给 {target_user_id}: 任务频道未配置。")
         return None

    if not task_id:
        task_id = f"{task_type}_{uuid.uuid4()}"

    message_data = {
        "task_id": task_id,
        "task_type": task_type,
        "source_user_id": admin_id, # 发起者是 Admin
        "target_user_id": target_user_id,
        "payload": payload,
        "timestamp": datetime.utcnow().isoformat()
    }
    success = await context.redis.publish(task_channel, message_data)
    if success:
        logger.info(f"已向频道 '{task_channel}' 发布任务 '{task_type}' (ID: {task_id}) 给用户 {target_user_id}。")
        return task_id
    else:
        logger.error(f"向频道 '{task_channel}' 发布任务 '{task_type}' (ID: {task_id}) 给用户 {target_user_id} 失败！")
        return None
# --- 新增结束 ---


# --- 修改: _share_recipes_cycle 改为顶层函数 ---
async def _share_recipes_cycle_task():
    """主任务：检查并执行配方共享 (由 APScheduler 调用)"""
    logger.info("【配方共享】周期任务启动...")
    context = get_global_context()
    if not context or not context.redis or not context.data_manager or not context.telegram_client:
        logger.error("【配方共享】核心服务不可用，任务中止。")
        return

    # 获取插件实例以访问内部状态和锁 (如果需要)
    plugin_instance: Optional['Plugin'] = getattr(context, 'recipe_sharing_plugin', None)
    if not plugin_instance:
         logger.error("【配方共享】无法获取插件实例，任务中止。")
         return
    # 检查是否为 Admin 实例
    if not plugin_instance._is_admin_instance:
         logger.debug("非 Admin 实例，跳过配方共享周期任务。")
         return

    redis_client = context.redis.get_client()
    if not redis_client:
        logger.error("【配方共享】无法连接 Redis，任务中止。")
        return

    # --- 使用 asyncio.Lock 控制并发访问插件内部状态 ---
    async with plugin_instance._transfer_lock:
        if plugin_instance._current_transfer:
             logger.info("【配方共享】检测到上一个转移流程可能仍在进行中，跳过本轮检查。")
             return
        # 标记开始处理
        plugin_instance._current_transfer = {"status": "starting", "timestamp": datetime.utcnow()}
    # --- Lock 结束 ---

    lock_key = ACTION_LOCK_KEY_FORMAT.format(plugin_instance._my_id or "admin")
    lock_acquired = False
    try:
        lock_acquired = await redis_client.set(lock_key, "1", ex=ACTION_LOCK_TTL, nx=True)
        if not lock_acquired:
            logger.info(f"【配方共享】获取 Redis 操作锁 ({lock_key}) 失败，上次任务可能仍在进行中，跳过。")
            async with plugin_instance._transfer_lock: plugin_instance._current_transfer = None # 清理内部标记
            return
        logger.info(f"【配方共享】成功获取 Redis 操作锁 ({lock_key})。")

        # --- Snapshot Phase (逻辑不变, 使用 context) ---
        logger.info("【配方共享】开始获取所有助手数据快照...")
        learned_recipes_snapshot: Dict[int, Set[str]] = {}
        inventory_recipes_snapshot: Dict[int, Dict[str, str]] = {} # {user_id: {recipe_item_id: recipe_name}}
        assistant_ids: List[int] = []
        assistant_usernames: Dict[int, str] = {}

        async for inv_key in redis_client.scan_iter(match=f"{CHAR_INVENTORY_KEY.format('*')}"):
             try:
                 user_id_str = inv_key.split(':')[-1]
                 if user_id_str.isdigit(): assistant_ids.append(int(user_id_str))
             except Exception as e: logger.warning(f"解析 Redis Key '{inv_key}' 获取 User ID 失败: {e}")

        if not assistant_ids:
             logger.warning("【配方共享】未找到任何助手的缓存数据，无法执行共享。")
             async with plugin_instance._transfer_lock: plugin_instance._current_transfer = None
             return # finally 会释放 Redis 锁

        logger.info(f"【配方共享】找到 {len(assistant_ids)} 个助手，开始获取数据...")
        item_master = await context.data_manager.get_item_master_data(use_cache=True)
        if not item_master:
             logger.error("【配方共享】无法获取物品主数据，任务中止。")
             async with plugin_instance._transfer_lock: plugin_instance._current_transfer = None
             return # finally 会释放 Redis 锁

        for user_id in assistant_ids:
            status_data = await context.data_manager.get_character_status(user_id, use_cache=True)
            username = status_data.get("username") if status_data else f"User_{user_id}"
            assistant_usernames[user_id] = username
            learned_ids = await context.data_manager.get_learned_recipes(user_id, use_cache=True)
            learned_recipes_snapshot[user_id] = set(learned_ids) if learned_ids else set()
            inv_data = await context.data_manager.get_inventory(user_id, use_cache=True)
            user_inv_recipes: Dict[str, str] = {}
            if inv_data and isinstance(inv_data.get("items_by_type"), dict):
                recipe_list = inv_data.get("items_by_type", {}).get("recipe", [])
                if isinstance(recipe_list, list):
                    for item in recipe_list:
                         if isinstance(item, dict) and item.get("item_id") and item.get("name"):
                             user_inv_recipes[item["item_id"]] = item["name"]
            inventory_recipes_snapshot[user_id] = user_inv_recipes
            logger.debug(f"助手 {username}({user_id}): 已学 {len(learned_recipes_snapshot[user_id])}, 背包配方 {len(inventory_recipes_snapshot[user_id])}")
        logger.info("【配方共享】数据快照获取完毕。")

        # --- Decision Phase (逻辑不变) ---
        logger.info("【配方共享】开始决策需要执行的配方转移...")
        transfers_to_execute: List[Tuple[int, int, str, str]] = [] # (teacher_id, student_id, recipe_item_id, recipe_name)
        recipes_in_transfer: Set[str] = set() # 记录本轮已安排转移的配方ID
        teacher_ids_shuffled = random.sample(assistant_ids, len(assistant_ids))
        for teacher_id in teacher_ids_shuffled:
            teacher_username = assistant_usernames.get(teacher_id, f"User_{teacher_id}")
            student_ids_shuffled = random.sample(assistant_ids, len(assistant_ids))
            for recipe_item_id, recipe_name in inventory_recipes_snapshot.get(teacher_id, {}).items():
                if recipe_item_id in learned_recipes_snapshot.get(teacher_id, set()) and recipe_item_id not in recipes_in_transfer:
                    logger.debug(f"教师 {teacher_username} 已学习配方 '{recipe_name}' 且持有图纸，寻找学生...")
                    for student_id in student_ids_shuffled:
                        if teacher_id == student_id: continue
                        student_username = assistant_usernames.get(student_id, f"User_{student_id}")
                        if (recipe_item_id not in learned_recipes_snapshot.get(student_id, set()) and
                            recipe_item_id not in inventory_recipes_snapshot.get(student_id, {})):
                            logger.info(f"找到转移目标: 教师 {teacher_username}({teacher_id}) -> 学生 {student_username}({student_id}), 配方: '{recipe_name}'({recipe_item_id})")
                            transfers_to_execute.append((teacher_id, student_id, recipe_item_id, recipe_name))
                            recipes_in_transfer.add(recipe_item_id)
                            break
        logger.info(f"【配方共享】决策完成，共找到 {len(transfers_to_execute)} 个可执行的转移。")

        # --- Execution Phase (使用顶层 _publish_task_internal) ---
        if transfers_to_execute:
            logger.info("【配方共享】开始执行转移流程...")
            pay_item_id_default = await get_item_id_by_name(context, DEFAULT_PAY_ITEM_NAME) # 使用 context
            if not pay_item_id_default:
                logger.error("【配方共享】无法获取默认支付物品 '灵石' 的 ID，无法执行转移！")
                async with plugin_instance._transfer_lock: plugin_instance._current_transfer = None
                return # finally 会释放 Redis 锁

            random.shuffle(transfers_to_execute)

            for index, (teacher_id, student_id, recipe_item_id, recipe_name) in enumerate(transfers_to_execute):
                async with plugin_instance._transfer_lock:
                    plugin_instance._current_transfer = {
                        "status": f"processing_{index+1}/{len(transfers_to_execute)}",
                        "teacher": teacher_id, "student": student_id, "recipe": recipe_name,
                        "timestamp": datetime.utcnow()
                    }
                teacher_username = assistant_usernames.get(teacher_id, f"User_{teacher_id}")
                student_username = assistant_usernames.get(student_id, f"User_{student_id}")
                transfer_log_prefix = f"【配方共享 {index+1}/{len(transfers_to_execute)}】(T:{teacher_username} -> S:{student_username}, R:{recipe_name})"
                transfer_task_id = f"recipe_share_{uuid.uuid4()}"

                # 1. 指示学生上架
                post_command = f".上架 {DEFAULT_PAY_ITEM_NAME}*{DEFAULT_PAY_QTY} 换 {recipe_name}*1"
                logger.info(f"{transfer_log_prefix} 步骤1: 指示学生 {student_username} ({student_id}) 执行 '{post_command}'...")
                post_task_id = await _publish_task_internal( # 使用顶层函数
                    context=context,
                    target_user_id=student_id,
                    task_type="send_command",
                    payload={"command": post_command},
                    task_id=f"{transfer_task_id}_post"
                )
                if not post_task_id:
                    logger.error(f"{transfer_log_prefix} 指示学生上架失败，跳过此转移。")
                    continue # 处理下一个转移

                logger.info(f"{transfer_log_prefix} 等待 {plugin_instance.student_post_delay} 秒让学生上架...")
                await asyncio.sleep(plugin_instance.student_post_delay)

                # 2. (Admin) 指示老师购买 (通过 marketplace:orders Pub/Sub)
                market_request_id = f"{transfer_task_id}_buy"
                order_data = {
                    "request_id": market_request_id, "recipient_id": student_id, "recipient_username": student_username,
                    "receive_item_id": pay_item_id_default, "receive_item_name": DEFAULT_PAY_ITEM_NAME, "receive_qty": DEFAULT_PAY_QTY,
                    "pay_item_id": recipe_item_id, "pay_item_name": recipe_name, "pay_qty": 1,
                    "designated_seller_id": teacher_id, "origin": "recipe_sharing_buy",
                    "timestamp": datetime.utcnow().isoformat()
                }
                logger.info(f"{transfer_log_prefix} 步骤2: 准备通过 Pub/Sub ({plugin_instance.order_channel}) 指示老师 {teacher_username} ({teacher_id}) 购买学生挂单...")
                if context.redis and plugin_instance.order_channel:
                    pub_success = await context.redis.publish(plugin_instance.order_channel, order_data)
                    if pub_success:
                        logger.info(f"{transfer_log_prefix} 购买指令已通过 Pub/Sub 发布给老师 (MarketReqID: {market_request_id})。")
                        logger.info(f"{transfer_log_prefix} 等待 {plugin_instance.student_learn_delay} 秒让交易完成...")
                        await asyncio.sleep(plugin_instance.student_learn_delay)

                        # 3. (Admin) 指示学生学习
                        learn_command = f".学习 {recipe_name}"
                        logger.info(f"{transfer_log_prefix} 步骤3: 指示学生 {student_username} ({student_id}) 执行 '{learn_command}'...")
                        learn_task_id = await _publish_task_internal( # 使用顶层函数
                            context=context,
                            target_user_id=student_id,
                            task_type="send_command",
                            payload={"command": learn_command},
                            task_id=f"{transfer_task_id}_learn"
                        )
                        if not learn_task_id:
                            logger.error(f"{transfer_log_prefix} 指示学生学习失败。")
                        else:
                             logger.info(f"{transfer_log_prefix} 指示学生学习的指令已发布。")
                    else:
                        logger.error(f"{transfer_log_prefix} 向频道 '{plugin_instance.order_channel}' 指派购买任务失败！")
                else:
                     logger.error(f"{transfer_log_prefix} 无法指派购买任务：Redis 不可用或未配置订单频道。")

                logger.info(f"{transfer_log_prefix} 本次转移流程结束，等待 5-10 秒...")
                await asyncio.sleep(random.uniform(5, 10))

            logger.info("【配方共享】本轮转移指令已全部发出。")
        else:
             logger.info("【配方共享】本轮无可执行的配方转移。")

    except Exception as e:
        logger.error(f"【配方共享】周期任务执行过程中发生意外错误: {e}", exc_info=True)
    finally:
        # 释放 Redis 锁
        if lock_acquired and redis_client:
            try:
                await redis_client.delete(lock_key)
                logger.info(f"【配方共享】Redis 操作锁 ({lock_key}) 已释放。")
            except Exception as e_lock_final:
                logger.error(f"【配方共享】释放 Redis 锁 ({lock_key}) 时出错: {e_lock_final}")
        # 清理内部处理标记
        if plugin_instance: # 检查实例是否存在
             async with plugin_instance._transfer_lock:
                 plugin_instance._current_transfer = None
        logger.info("【配方共享】周期任务结束。")
# --- 修改结束 ---


class Plugin(BasePlugin):
    """
    自动在助手之间共享已学习的配方图纸。
    """
    def __init__(self, context: AppContext, plugin_name: str, cn_name: str | None = None):
        super().__init__(context, plugin_name, cn_name)
        # --- 新增: 将实例附加到 context ---
        setattr(context, plugin_name, self) # 允许顶层函数访问实例属性
        # --- 新增结束 ---
        self.load_config()
        self._my_id: Optional[int] = None
        self._my_username: Optional[str] = None
        self._is_admin_instance: bool = False
        self._current_transfer: Optional[Dict[str, Any]] = None
        self._transfer_lock = asyncio.Lock()

        if self.auto_enabled:
            self.info(f"插件已加载并启用。检查间隔: {self.check_interval_hours} 小时。")
        else:
            self.info("插件已加载但未启用。")

    def load_config(self):
        self.auto_enabled = self.config.get("recipe_sharing.enabled", True)
        self.check_interval_hours = self.config.get("recipe_sharing.check_interval_hours", 2)
        self.student_post_delay = self.config.get("recipe_sharing.student_post_delay_seconds", 30)
        self.student_learn_delay = self.config.get("recipe_sharing.student_learn_delay_seconds", 30)
        self.order_channel = self.config.get("marketplace_transfer.order_channel", "marketplace:orders")
        self.task_channel = self.config.get("communication.task_channel", "assistant_tasks")

    def register(self):
        """注册定时任务和初始化"""
        if not self.auto_enabled:
            return

        if not isinstance(self.check_interval_hours, (int, float)) or self.check_interval_hours <= 0:
            self.warning(f"无效的检查间隔 {self.check_interval_hours}, 使用默认值 2 小时。")
            self.check_interval_hours = 2

        try:
            if self.scheduler:
                # --- 修改: 调度顶层函数 ---
                self.scheduler.add_job(
                    _share_recipes_cycle_task, # <--- 改为顶层函数
                    trigger='interval',
                    hours=self.check_interval_hours,
                    jitter=300,
                    id=RECIPE_SHARING_JOB_ID,
                    replace_existing=True,
                    misfire_grace_time=600
                )
                # --- 修改结束 ---
                self.info(f"已注册配方共享定时任务 (每 {self.check_interval_hours} 小时运行一次)。")
                self.event_bus.on("telegram_client_started", self._initialize_instance)
            else:
                 self.error("无法注册配方共享任务：Scheduler 不可用。")
        except Exception as e:
            self.error(f"注册配方共享定时任务时出错: {e}", exc_info=True)

        # 订单频道的监听由 TelegramClient 处理，这里无需重复

    async def _initialize_instance(self):
        """确定当前实例是否为 Admin 实例"""
        if not self.context.telegram_client: return
        self._my_id = await self.context.telegram_client.get_my_id()
        self._my_username = await self.context.telegram_client.get_my_username()
        admin_id_config = self.config.get("telegram.admin_id")
        self._is_admin_instance = (admin_id_config is not None and self._my_id == admin_id_config)
        self.info(f"实例初始化: ID={self._my_id}, Admin={self._is_admin_instance}")

    # --- 移除 _publish_task 实例方法 ---


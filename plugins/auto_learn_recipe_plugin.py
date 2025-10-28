import logging
import asyncio
import json
import random
from typing import Optional, List, Dict
from plugins.base_plugin import BasePlugin, AppContext
from core.context import get_global_context
from apscheduler.jobstores.base import JobLookupError

# --- 常量 ---
LEARN_RECIPE_JOB_ID = 'auto_learn_recipe_job' # 定时检查任务 ID
LEARN_RECIPE_COMMAND_FORMAT = ".学习 {}" # 学习指令格式
# --- 修改: 锁 Key 包含 user_id 占位符 ---
REDIS_LEARN_RECIPE_LOCK_KEY_FORMAT = "learn_recipe:action_lock:{}" # Redis 操作锁 Key 格式
# --- 修改结束 ---
LEARN_RECIPE_LOCK_TTL = 120 # 锁的 TTL (秒)

async def _check_and_learn_recipes():
    """由 APScheduler 调度的函数，检查背包中是否有未学习的配方并尝试学习"""
    logger = logging.getLogger("AutoLearnRecipePlugin.Check")
    logger.info("【自动学习配方】任务启动，开始检查...")

    context = get_global_context()
    if not context or not context.data_manager or not context.redis or not context.telegram_client:
        logger.error("【自动学习配方】无法执行：核心服务 (DataManager/Redis/TGClient) 不可用。")
        return

    config = context.config
    data_manager = context.data_manager

    auto_enabled = config.get("auto_learn_recipe.enabled", False)
    if not auto_enabled:
        logger.info("【自动学习配方】已被禁用，跳过本次执行。")
        return

    redis_client = context.redis.get_client()
    my_id = context.telegram_client._my_id

    # --- 修改: 在获取锁之前检查 my_id ---
    if not my_id:
        logger.warning("【自动学习配方】无法获取助手 User ID，跳过本次检查。")
        return
    # --- 修改结束 ---

    lock_acquired = False
    # --- 修改: 格式化锁 Key ---
    lock_key = REDIS_LEARN_RECIPE_LOCK_KEY_FORMAT.format(my_id)
    # --- 修改结束 ---

    if redis_client:
        try:
            # --- 修改: 使用格式化后的 lock_key ---
            lock_acquired = await redis_client.set(lock_key, "1", ex=LEARN_RECIPE_LOCK_TTL, nx=True)
            if not lock_acquired:
                logger.info(f"【自动学习配方】获取操作锁 ({lock_key}) 失败，上次检查可能仍在进行中，跳过本次。")
                return
            logger.info(f"【自动学习配方】成功获取操作锁 ({lock_key})。")
            # --- 修改结束 ---
        except Exception as e:
            logger.error(f"【自动学习配方】检查或设置 Redis 锁 ({lock_key}) 失败: {e}，为安全起见跳过本次检查。")
            return
    else:
        logger.error("【自动学习配方】Redis 未连接，无法检查锁，任务终止。")
        return

    try:
        # my_id 在前面已检查
        learned_recipes_ids: List[str] = []
        inventory_recipes: List[Dict] = []

        # ... (获取已学配方和背包配方逻辑不变) ...
        try:
            logger.info("【自动学习配方】正在通过 DataManager 获取已学配方缓存...")
            known_ids_list = await data_manager.get_learned_recipes(my_id, use_cache=True)
            if known_ids_list is not None:
                if isinstance(known_ids_list, list):
                    learned_recipes_ids = known_ids_list
                    logger.info(f"【自动学习配方】获取到已学习配方 {len(learned_recipes_ids)} 个。")
                else:
                    logger.error(f"【自动学习配方】DataManager 返回的已学配方数据格式不正确 (非列表)，类型: {type(known_ids_list)}，本次跳过。")
                    learned_recipes_ids = []
            else:
                logger.warning("【自动学习配方】无法从 DataManager 获取已学配方缓存，跳过本次检查。")
                return # finally 会释放锁
        except Exception as e:
            logger.error(f"【自动学习配方】通过 DataManager 获取已学配方时出错: {e}", exc_info=True)
            return # finally 会释放锁

        try:
            logger.info("【自动学习配方】正在通过 DataManager 获取背包缓存...")
            inventory_cache = await data_manager.get_inventory(my_id, use_cache=True)
            if inventory_cache and isinstance(inventory_cache, dict):
                inventory_recipes = inventory_cache.get("items_by_type", {}).get("recipe", [])
                if isinstance(inventory_recipes, list):
                     logger.info(f"【自动学习配方】获取到背包中配方 {len(inventory_recipes)} 个。")
                else:
                    logger.error(f"【自动学习配方】背包缓存中的配方数据格式不正确（非列表），类型: {type(inventory_recipes)}，本次跳过。")
                    inventory_recipes = []
            else:
                logger.warning("【自动学习配方】无法从 DataManager 获取背包缓存，跳过本次检查。")
                return # finally 会释放锁
        except Exception as e:
            logger.error(f"【自动学习配方】通过 DataManager 获取背包数据时出错: {e}", exc_info=True)
            return # finally 会释放锁

        # ... (查找未学习配方、发送学习指令逻辑不变) ...
        recipe_to_learn_name: Optional[str] = None
        recipe_to_learn_id: Optional[str] = None
        learned_set = set(learned_recipes_ids) # 转换为集合以便快速查找
        for recipe in inventory_recipes:
            if isinstance(recipe, dict):
                item_id = recipe.get("item_id")
                item_name = recipe.get("name")
                if item_id and item_name:
                    if item_id not in learned_set:
                        recipe_to_learn_id = item_id; recipe_to_learn_name = item_name
                        logger.info(f"【自动学习配方】发现未学习配方: '{recipe_to_learn_name}' (ID: {recipe_to_learn_id})")
                        break # 每次只学习一个
                else: logger.warning(f"【自动学习配方】背包中的配方条目缺少 ID 或名称: {recipe}")
            else: logger.warning(f"【自动学习配方】背包配方列表中的条目格式不正确（不是字典）: {recipe}")

        if recipe_to_learn_name:
            learn_command = LEARN_RECIPE_COMMAND_FORMAT.format(recipe_to_learn_name)
            try:
                logger.info(f"【自动学习配方】准备将学习指令 '{learn_command}' 加入发送队列...")
                success = await context.telegram_client.send_game_command(learn_command)
                if success:
                    logger.info(f"【自动学习配方】指令 '{learn_command}' 已成功加入发送队列。")
                    logger.info("【自动学习配方】触发角色数据同步...")
                    try:
                        await context.event_bus.emit("trigger_character_sync_now")
                    except Exception as sync_e:
                        logger.error(f"【自动学习配方】尝试在发送学习指令后触发角色同步时出错: {sync_e}", exc_info=True)
                else:
                     logger.error(f"【自动学习配方】将学习指令 '{learn_command}' 加入队列失败。")
            except Exception as e:
                logger.error(f"【自动学习配方】将学习指令 '{learn_command}' 加入队列时出错: {e}", exc_info=True)
        else:
            logger.info("【自动学习配方】背包中未发现需要学习的新配方。")

    finally:
        # --- 修改: 使用格式化后的 lock_key ---
        if lock_acquired and redis_client:
            try:
                await redis_client.delete(lock_key)
                logger.info(f"【自动学习配方】操作锁 ({lock_key}) 已释放。")
            except Exception as e_lock_final:
                logger.error(f"【自动学习配方】释放 Redis 锁 ({lock_key}) 时出错: {e_lock_final}")
        # --- 修改结束 ---

# --- 插件类 (其余部分保持不变) ---
class Plugin(BasePlugin):
    """
    自动学习背包中未学习配方的插件
    """
    def __init__(self, context: AppContext, plugin_name: str, cn_name: str | None = None):
        super().__init__(context, plugin_name, cn_name)
        self.load_config()
        if self.auto_enabled:
            self.info(f"插件已加载并启用。检查频率约为每天 {self.check_times_per_day} 次。学习指令格式: '{LEARN_RECIPE_COMMAND_FORMAT.format('<配方名>')}'")
        else:
            self.info("插件已加载但未启用。")

    def load_config(self):
        """加载配置"""
        self.auto_enabled = self.config.get("auto_learn_recipe.enabled", False)
        self.check_times_per_day = self.config.get("auto_learn_recipe.checks_per_day", 5)
        if not isinstance(self.check_times_per_day, int) or self.check_times_per_day < 1:
            self.warning(f"无效的 auto_learn_recipe.checks_per_day 配置 '{self.check_times_per_day}'，使用默认值 5")
            self.check_times_per_day = 5
        self.check_interval_hours = 24 / self.check_times_per_day
        self.jitter_seconds = int(self.check_interval_hours * 3600 / 4)

    def register(self):
        """注册定时任务"""
        # ... (注册逻辑不变) ...
        if not self.auto_enabled:
            return
        try:
            if self.scheduler:
                self.scheduler.add_job(
                    _check_and_learn_recipes,
                    trigger='interval',
                    hours=self.check_interval_hours,
                    jitter=self.jitter_seconds,
                    id=LEARN_RECIPE_JOB_ID,
                    replace_existing=True,
                    misfire_grace_time=300
                )
                self.info(f"已注册自动学习配方任务 (约每 {self.check_interval_hours:.1f} 小时检查一次)。")
            else:
                 self.error("无法注册自动学习配方任务：Scheduler 不可用。")
        except Exception as e:
            self.error(f"注册自动学习配方定时任务时出错: {e}", exc_info=True)


import logging
import json
import asyncio
from datetime import datetime, timedelta
import pytz
from typing import Optional, Dict, List, Any, Tuple # 增加 Tuple 导入
from plugins.base_plugin import AppContext # 用于类型提示
from plugins.constants import ( # 导入 Redis Key 常量
    REDIS_CHAR_KEY_PREFIX, REDIS_INV_KEY_PREFIX, REDIS_ITEM_MASTER_KEY,
    REDIS_SHOP_KEY_PREFIX
)
# 导入时间处理函数
from plugins.character_sync_plugin import parse_iso_datetime, format_local_time
# 从 collections 导入 defaultdict
from collections import defaultdict
import copy # 导入 copy 模块

logger = logging.getLogger("GameDataManager")

# --- 定义新的 Redis Key 结构 ---
CHAR_STATUS_KEY = "char:status:{}" # 角色状态、属性、冷却 (TTL 短)
CHAR_INVENTORY_KEY = "char:inventory:{}" # 背包 (TTL 中)
CHAR_SECT_KEY = "char:sect:{}" # 宗门信息 (TTL 中/长)
CHAR_GARDEN_KEY = "char:garden:{}" # 药园 (TTL 短)
CHAR_PAGODA_KEY = "char:pagoda:{}" # 闯塔 (TTL 长)
CHAR_RECIPES_KEY = "char:recipes:{}" # 已学配方 (TTL 中/长)
# --- 新增: 观星台 Key ---
CHAR_STAR_PLATFORM_KEY = "char:star_platform:{}" # 观星台 (TTL 短)
# --- 新增结束 ---
GAME_ITEMS_MASTER_KEY = "game:items:master" # 物品主数据 (TTL 长)
GAME_SHOP_KEY = "game:shop:{}" # 商店数据 (TTL 长)
# --- Key 定义结束 ---

# --- 辅助函数：格式化 TTL ---
def format_ttl_internal(ttl_seconds: int | None) -> str:
    if ttl_seconds is None or ttl_seconds < 0: return "未知或已过期"
    if ttl_seconds < 60: return f"{ttl_seconds} 秒"
    elif ttl_seconds < 3600: return f"{round(ttl_seconds / 60)} 分钟"
    else: return f"{round(ttl_seconds / 3600, 1)} 小时"
# --- 辅助函数结束 ---

class GameDataManager:
    def __init__(self, context: AppContext):
        self.context = context
        self.redis = context.redis
        self.http = context.http
        self.config = context.config
        self._item_master_cache: Dict[str, Dict] = {} # 物品主数据内存缓存

    async def _get_redis_client(self):
        """获取 Redis 客户端，带重连尝试"""
        client = self.redis.get_client()
        if not client:
            logger.warning("【数据管理器】Redis 未连接，尝试重连...")
            try:
                await self.redis.connect()
                client = self.redis.get_client()
                if client: logger.info("【数据管理器】Redis 重连成功。")
                else: logger.error("【数据管理器】Redis 重连失败。")
            except Exception as e:
                logger.error(f"【数据管理器】Redis 重连时出错: {e}")
        return client

    # --- 数据更新核心方法 (内部调用或由同步插件调用) ---

    async def _update_cache_from_api_internal(self, user_id: int, username: str) -> bool:
        """【内部核心】调用 /api/cultivator 并更新所有相关的 Redis 缓存"""
        logger.debug(f"【数据管理器】内部更新开始: 用户 {user_id} ({username})")
        redis_client = await self._get_redis_client()
        if not redis_client:
            logger.error(f"【数据管理器】内部更新失败 (用户 {user_id})：无法连接 Redis。")
            return False

        try:
            raw_data = await self.http.get_cultivator_data(username)
            if not raw_data:
                logger.error(f"【数据管理器】内部更新失败 (用户 {user_id})：API 请求失败或返回空数据。")
                return False

            now_aware_dt = datetime.now().astimezone()
            now_aware_str = now_aware_dt.strftime("%Y-%m-%d %H:%M:%S %Z%z")

            DEFAULT_TTL = {
                'status': 360, 'inventory': 1200, 'sect': 3600, 'garden': 360,
                'pagoda': 86400, 'recipes': 43200, 'star_platform': 360 # <-- 添加观星台默认 TTL
            }
            def get_ttl(key_type: str) -> int:
                return self.config.get(f"cache_ttl.{key_type}", DEFAULT_TTL.get(key_type, 600))

            status_ttl = get_ttl('status')
            inv_ttl = get_ttl('inventory')
            sect_ttl = get_ttl('sect')
            garden_ttl = get_ttl('garden')
            pagoda_ttl = get_ttl('pagoda')
            recipes_ttl = get_ttl('recipes')
            star_platform_ttl = get_ttl('star_platform') # <-- 获取观星台 TTL

            async with redis_client.pipeline(transaction=False) as pipe:
                executed_pipe = False

                # --- 1. 处理角色核心状态 (移除 herb_garden, pagoda_progress 初始化) ---
                status_data = {
                    "_internal_last_updated": now_aware_str, "telegram_id": raw_data.get("telegram_id"),
                    "username": raw_data.get("username"), "dao_name": raw_data.get("dao_name"),
                    "status": raw_data.get("status"), "cultivation_level": raw_data.get("cultivation_level"),
                    "cultivation_points": raw_data.get("cultivation_points"), "is_bottleneck": raw_data.get("is_bottleneck"),
                    "drug_poison_points": raw_data.get("drug_poison_points"), "spirit_root": raw_data.get("spirit_root"),
                    "shenshi_points": raw_data.get("shenshi_points"), "kill_count": raw_data.get("kill_count"),
                    "death_count": raw_data.get("death_count"), "active_badge": raw_data.get("active_badge"),
                    "divination_count_today": raw_data.get("divination_count_today"),
                    "cultivation_cooldown_until": raw_data.get("cultivation_cooldown_until"),
                    "deep_seclusion_start_time": raw_data.get("deep_seclusion_start_time"),
                    "deep_seclusion_end_time": raw_data.get("deep_seclusion_end_time"),
                    "last_yindao_time": raw_data.get("last_yindao_time"),
                    "last_battle_time": raw_data.get("last_battle_time"), "last_dummy_practice_time": raw_data.get("last_dummy_practice_time"),
                    "last_dungeon_time": raw_data.get("last_dungeon_time"), "last_trial_time": raw_data.get("last_trial_time"),
                    "last_treasure_hunt_time": raw_data.get("last_treasure_hunt_time"),
                    "force_seclusion_cooldown_until": raw_data.get("force_seclusion_cooldown_until"),
                    "last_elixir_time": raw_data.get("last_elixir_time"), "last_bet_date": raw_data.get("last_bet_date"),
                    # "herb_garden": None, "pagoda_progress": None, # <--- 移除初始化
                }
                # --- 处理嵌套 JSON 和时间格式化 (将 herb_garden, pagoda_progress, star_platform 移到后面单独处理) ---
                processed_nested_data = {} # 存储解析后的嵌套数据
                for field_name in ["active_formation", "active_yindao_buff", "herb_garden", "pagoda_progress", "star_platform"]: # <--- 添加 star_platform
                    field_str = raw_data.get(field_name); field_data = None
                    if isinstance(field_str, str) and field_str.strip() and field_str.strip() != '{}' and field_str.strip() != '[]': # 检查非空字符串
                        try: field_data = json.loads(field_str)
                        except json.JSONDecodeError: logger.warning(f"解析字段 '{field_name}' 的 JSON 字符串失败: {field_str[:100]}...")
                    elif isinstance(field_str, dict): field_data = field_str # 如果 API 直接返回字典
                    elif field_str is None: field_data = None # 处理 null 值

                    if isinstance(field_data, dict):
                        processed_nested_data[field_name] = copy.deepcopy(field_data)
                        current_field_dict = processed_nested_data[field_name]

                        # 特殊处理时间
                        # 药园地块时间
                        if field_name == "herb_garden" and isinstance(current_field_dict.get("plots"), dict):
                              for plot_id, plot_info in list(current_field_dict["plots"].items()):
                                  if isinstance(plot_info, dict) and plot_info.get("plant_time"):
                                      parsed_dt_p = parse_iso_datetime(plot_info["plant_time"])
                                      current_field_dict["plots"][plot_id]["plant_time_formatted"] = format_local_time(parsed_dt_p)
                        # 观星台地块时间
                        elif field_name == "star_platform" and isinstance(current_field_dict.get("plots"), dict):
                            for plot_id, plot_info in list(current_field_dict["plots"].items()):
                                if isinstance(plot_info, dict) and plot_info.get("start_time"):
                                    parsed_dt_sp = parse_iso_datetime(plot_info["start_time"])
                                    current_field_dict["plots"][plot_id]["start_time_formatted"] = format_local_time(parsed_dt_sp)
                                    # 计算剩余时间 (如果需要)
                                    # ... (可以在插件端计算) ...

                        # 处理其他嵌套字典中的时间
                        for key, value in list(current_field_dict.items()):
                             if isinstance(value, str) and ("_time" in key or "_until" in key or key == "expiry" or "_date" in key):
                                  parsed_dt_inner = parse_iso_datetime(value)
                                  if parsed_dt_inner:
                                      current_field_dict[key + "_formatted"] = format_local_time(parsed_dt_inner)
                    elif field_data is None: # 如果 API 返回 null
                         processed_nested_data[field_name] = None
                    # else: field_data 解析失败或不是字典/列表，processed_nested_data 中无此 key

                # 将需要存入 status_data 的嵌套数据加入
                if "active_formation" in processed_nested_data:
                    status_data["active_formation"] = processed_nested_data["active_formation"]
                if "active_yindao_buff" in processed_nested_data:
                    status_data["active_yindao_buff"] = processed_nested_data["active_yindao_buff"]

                # 格式化顶层时间戳 (逻辑不变)
                time_keys_to_format = ["cultivation_cooldown_until", "deep_seclusion_start_time", "deep_seclusion_end_time", "last_yindao_time", "last_battle_time", "last_dummy_practice_time", "last_dungeon_time", "last_trial_time", "last_treasure_hunt_time", "force_seclusion_cooldown_until", "last_elixir_time", "sect_leave_cooldown_until"]
                for key in time_keys_to_format:
                    if raw_data.get(key):
                        parsed_dt = parse_iso_datetime(raw_data[key]); status_data[key + "_formatted"] = format_local_time(parsed_dt)
                        # 移除原始值，只保留格式化后的？或者都保留？目前保留原始值
                        # status_data[key] = raw_data[key]

                status_key = CHAR_STATUS_KEY.format(user_id)
                pipe.set(status_key, json.dumps(status_data, ensure_ascii=False), ex=status_ttl)
                logger.debug(f"【数据管理器】准备更新 {status_key} (TTL: ~{status_ttl}s)")
                executed_pipe = True

                # --- 2. 处理背包数据 (逻辑不变) ---
                inventory_data_processed = await self._process_inventory_data(raw_data.get("inventory"), now_aware_str)
                if inventory_data_processed:
                    inv_key = CHAR_INVENTORY_KEY.format(user_id)
                    pipe.set(inv_key, json.dumps(inventory_data_processed, ensure_ascii=False), ex=inv_ttl)
                    logger.debug(f"【数据管理器】准备更新 {inv_key} (TTL: ~{inv_ttl}s)")
                    executed_pipe = True
                else: logger.warning("【数据管理器】API 返回的背包数据为空或处理失败，未更新背包缓存。")

                # --- 3. 处理宗门信息 (逻辑不变) ---
                sect_data = {
                    "_internal_last_updated": now_aware_str, "sect_name": raw_data.get("sect_name"), "sect_id": raw_data.get("sect_id"),
                    "sect_contribution": raw_data.get("sect_contribution"), "is_sect_elder": raw_data.get("is_sect_elder"),
                    "is_grand_elder": raw_data.get("is_grand_elder"), "last_sect_check_in": raw_data.get("last_sect_check_in"),
                    "consecutive_check_in_days": raw_data.get("consecutive_check_in_days"), "last_teach_date": raw_data.get("last_teach_date"),
                    "teach_count": raw_data.get("teach_count"), "last_salary_claim_month": raw_data.get("last_salary_claim_month"),
                    "sect_leave_cooldown_until": raw_data.get("sect_leave_cooldown_until"),
                }
                if sect_data.get("sect_leave_cooldown_until"):
                     parsed_dt_s = parse_iso_datetime(sect_data["sect_leave_cooldown_until"]); sect_data["sect_leave_cooldown_until_formatted"] = format_local_time(parsed_dt_s)
                sect_key = CHAR_SECT_KEY.format(user_id)
                pipe.set(sect_key, json.dumps(sect_data, ensure_ascii=False), ex=sect_ttl)
                logger.debug(f"【数据管理器】准备更新 {sect_key} (TTL: ~{sect_ttl}s)")
                executed_pipe = True

                # --- 4. 处理药园数据 (使用 processed_nested_data) ---
                if "herb_garden" in processed_nested_data and processed_nested_data["herb_garden"] is not None:
                     garden_data_to_store = { "_internal_last_updated": now_aware_str, **processed_nested_data["herb_garden"] }
                     garden_key = CHAR_GARDEN_KEY.format(user_id)
                     pipe.set(garden_key, json.dumps(garden_data_to_store, ensure_ascii=False), ex=garden_ttl)
                     logger.debug(f"【数据管理器】准备更新 {garden_key} (TTL: ~{garden_ttl}s)")
                     executed_pipe = True
                else: logger.info("【数据管理器】API 未返回药园数据或解析失败/为None，不更新药园缓存。")

                # --- 5. 处理闯塔数据 (使用 processed_nested_data) ---
                if "pagoda_progress" in processed_nested_data and processed_nested_data["pagoda_progress"] is not None:
                     pagoda_data_to_store = {
                         "_internal_last_updated": now_aware_str,
                         "progress": processed_nested_data["pagoda_progress"], # 解析后的字典
                         "failed_floor": raw_data.get("pagoda_failed_floor"),
                         "resets_today": raw_data.get("pagoda_resets_today"),
                         "claimed_floors_str": raw_data.get("pagoda_claimed_floors") # 原始字符串
                     }
                     # 尝试解析 claimed_floors
                     try:
                         claimed_floors_list = json.loads(pagoda_data_to_store["claimed_floors_str"]) if pagoda_data_to_store["claimed_floors_str"] else []
                         pagoda_data_to_store["claimed_floors"] = claimed_floors_list if isinstance(claimed_floors_list, list) else []
                     except:
                         logger.warning(f"解析闯塔已领取楼层JSON失败: {pagoda_data_to_store['claimed_floors_str']}")
                         pagoda_data_to_store["claimed_floors"] = []

                     pagoda_key = CHAR_PAGODA_KEY.format(user_id)
                     pipe.set(pagoda_key, json.dumps(pagoda_data_to_store, ensure_ascii=False), ex=pagoda_ttl)
                     logger.debug(f"【数据管理器】准备更新 {pagoda_key} (TTL: ~{pagoda_ttl}s)")
                     executed_pipe = True
                else: logger.info("【数据管理器】API 未返回闯塔进度数据或解析失败/为None，不更新闯塔缓存。")

                # --- 6. 处理已学配方 (逻辑不变) ---
                recipes_str = raw_data.get("recipes_known"); recipes_list_processed = None
                if isinstance(recipes_str, str) and recipes_str.strip() and recipes_str.strip() != '[]':
                    try: recipes_list_processed = json.loads(recipes_str)
                    except: logger.warning("解析已学配方 JSON 字符串失败。")
                elif isinstance(recipes_str, list): recipes_list_processed = recipes_str
                if isinstance(recipes_list_processed, list):
                     recipes_data_to_store = { "_internal_last_updated": now_aware_str, "known_ids": recipes_list_processed }
                     recipes_key = CHAR_RECIPES_KEY.format(user_id)
                     pipe.set(recipes_key, json.dumps(recipes_data_to_store, ensure_ascii=False), ex=recipes_ttl)
                     logger.debug(f"【数据管理器】准备更新 {recipes_key} (TTL: ~{recipes_ttl}s)")
                     executed_pipe = True
                else: logger.warning("【数据管理器】API 未返回已学配方数据或解析失败，未更新配方缓存。")

                # --- 7. 处理观星台数据 (使用 processed_nested_data) ---
                if "star_platform" in processed_nested_data and processed_nested_data["star_platform"] is not None:
                     star_platform_data_to_store = { "_internal_last_updated": now_aware_str, **processed_nested_data["star_platform"] }
                     star_platform_key = CHAR_STAR_PLATFORM_KEY.format(user_id)
                     pipe.set(star_platform_key, json.dumps(star_platform_data_to_store, ensure_ascii=False), ex=star_platform_ttl)
                     logger.debug(f"【数据管理器】准备更新 {star_platform_key} (TTL: ~{star_platform_ttl}s)")
                     executed_pipe = True
                else: logger.info("【数据管理器】API 未返回观星台数据或解析失败/为None，不更新观星台缓存。")
                # --- 新增结束 ---

                # --- 执行 Pipeline ---
                if executed_pipe:
                    await pipe.execute()
                    logger.info(f"【数据管理器】用户 {user_id} ({username}) 的缓存更新完成。")
                    return True
                else:
                    logger.warning(f"【数据管理器】用户 {user_id} ({username}) 无任何缓存需要更新?")
                    return False

        except Exception as e:
            logger.error(f"【数据管理器】内部更新缓存时发生意外错误: {e}", exc_info=True)
            return False

    # ... (update_cache_from_api, _process_inventory_data, update_item_master_cache, update_shop_cache 保持不变) ...
    async def update_cache_from_api(self, user_id: int, username: str) -> bool:
        """【公开】调用 /api/cultivator 并更新缓存"""
        return await self._update_cache_from_api_internal(user_id, username)

    async def _process_inventory_data(self, inventory_data: Optional[Dict], updated_time_str: str) -> Optional[Dict]:
        """【内部】处理来自 API 的 inventory 字典"""
        if not inventory_data or not isinstance(inventory_data, dict): return None
        logger.debug("【数据管理器】开始处理背包数据...")
        item_master_data = await self.get_item_master_data(use_cache=True)
        if not item_master_data:
            logger.warning("【数据管理器】处理背包时物品主数据为空，材料名称可能不准确。正在尝试强制刷新...")
            if await self.update_item_master_cache():
                item_master_data = await self.get_item_master_data(use_cache=True)
            if not item_master_data: logger.error("【数据管理器】强制刷新后物品主数据仍为空！"); item_master_data = {}
        items_list = inventory_data.get("items", []); materials_dict = inventory_data.get("materials", {})
        categorized_inventory = defaultdict(list); total_items_count = 0; material_types_count = 0
        if isinstance(items_list, list):
            for item in items_list:
                if isinstance(item, dict) and all(k in item for k in ["item_id", "name", "quantity", "type"]):
                    categorized_inventory[item["type"]].append({"item_id": item["item_id"], "name": item["name"], "quantity": item["quantity"]})
                    total_items_count += 1
                else: logger.warning(f"【数据管理器】跳过 items 列表中格式错误的条目: {item}")
        else: logger.warning("【数据管理器】API 数据中 'inventory.items' 字段不是列表。")
        if isinstance(materials_dict, dict):
            material_types_count = len(materials_dict); total_items_count += material_types_count
            for mat_id, quantity in materials_dict.items():
                item_info = item_master_data.get(mat_id); item_name = f"未知({mat_id})"; item_type = "material"
                if item_info and isinstance(item_info, dict):
                    item_name = item_info.get("name", item_name); item_type = item_info.get("type", item_type)
                    if item_type != "material": logger.warning(f"材料 '{mat_id}'({item_name}) 主数据类型为 '{item_type}'?")
                elif item_master_data: logger.warning(f"物品主数据未找到材料 '{mat_id}' 的信息。")
                try: qty_int = int(quantity)
                except: qty_int = 0; logger.warning(f"材料'{mat_id}'数量'{quantity}'无效")
                categorized_inventory[item_type].append({"item_id": mat_id, "name": item_name, "quantity": qty_int})
        else: logger.warning("【数据管理器】API 数据中 'inventory.materials' 字段不是字典。")
        summary = {"total_types": total_items_count, "material_types": material_types_count, "last_updated": updated_time_str}
        full_inventory_data = {"summary": summary, "items_by_type": dict(categorized_inventory)}
        logger.debug(f"【数据管理器】背包数据处理完成，共 {total_items_count} 种物品。")
        return full_inventory_data

    async def update_item_master_cache(self) -> bool:
        """【公开】调用 /api/all_items 并更新 game:items:master 缓存"""
        logger.info("【数据管理器】开始更新全局物品主数据缓存...")
        redis_client = await self._get_redis_client()
        if not redis_client: return False
        try:
            response_data = await self.http.get_all_items()
            if response_data is None: logger.error("【数据管理器】获取物品主数据失败 (API 返回 None)。"); return False
            if not isinstance(response_data, list): logger.error(f"【数据管理器】物品主数据 API 返回格式错误 ({type(response_data)})。"); return False
            items_dict = {}
            parsed_count = 0
            for item in response_data:
                if isinstance(item, dict) and all(k in item for k in ["item_id", "name", "type"]):
                    items_dict[item["item_id"]] = {"name": item["name"], "type": item["type"]}
                    parsed_count += 1
                else: logger.warning(f"【数据管理器】跳过格式不正确的物品主数据条目: {item}")
            now_aware_str = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z%z")
            data_to_store = {"_internal_last_updated": now_aware_str, "items": items_dict}
            ttl_seconds = self.config.get("cache_ttl.item_master", 90000)
            await redis_client.set(GAME_ITEMS_MASTER_KEY, json.dumps(data_to_store, ensure_ascii=False), ex=ttl_seconds)
            logger.info(f"【数据管理器】全局物品主数据已更新到 Redis ({parsed_count} 条)，Key: {GAME_ITEMS_MASTER_KEY} (TTL: ~{ttl_seconds}s)")
            self._item_master_cache = items_dict # 更新内存缓存
            return True
        except Exception as e:
            logger.error(f"【数据管理器】更新物品主数据缓存时出错: {e}", exc_info=True)
            self._item_master_cache = {} # 出错时清空内存缓存
            return False

    async def update_shop_cache(self, user_id: int) -> bool:
        """【公开】调用 /api/shop_items 并更新 game:shop:{id} 缓存"""
        logger.info(f"【数据管理器】开始更新用户 {user_id} 的商店缓存...")
        redis_client = await self._get_redis_client()
        if not redis_client: return False
        try:
            response_data = await self.http.get_shop_items()
            if response_data is None: logger.error("【数据管理器】获取商店数据失败 (API 返回 None)。"); return False
            if not isinstance(response_data, list): logger.error(f"【数据管理器】商店 API 返回格式错误 ({type(response_data)})。"); return False
            shop_items_dict = {}
            parsed_count = 0
            for item in response_data:
                if isinstance(item, dict) and "item_id" in item:
                    shop_items_dict[item["item_id"]] = {"name": item.get("name"), "type": item.get("type"), "price": item.get("shop_price"), "sect_exclusive": item.get("sect_exclusive")}
                    parsed_count += 1
                else: logger.warning(f"【数据管理器】跳过格式不正确的商店物品条目: {item}")
            now_aware_str = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z%z")
            data_to_store = {"_internal_last_updated": now_aware_str, "items": shop_items_dict}
            shop_key = GAME_SHOP_KEY.format(user_id)
            ttl_seconds = self.config.get("cache_ttl.shop", 90000)
            await redis_client.set(shop_key, json.dumps(data_to_store, ensure_ascii=False), ex=ttl_seconds)
            logger.info(f"【数据管理器】用户 {user_id} 的商店数据已更新到 Redis ({parsed_count} 条)，Key: {shop_key} (TTL: ~{ttl_seconds}s)")
            return True
        except Exception as e:
            logger.error(f"【数据管理器】更新商店缓存时出错: {e}", exc_info=True)
            return False

    # --- 数据获取方法 (供插件调用) ---

    async def _get_cache_data(self, key: str) -> Optional[Tuple[Any, Optional[int], Optional[str]]]:
        """【内部】读取指定 key 的缓存数据、TTL 和更新时间"""
        redis_client = await self._get_redis_client()
        if not redis_client: return None, None, None # 返回三元组以匹配类型提示
        try:
            async with redis_client.pipeline(transaction=False) as pipe:
                pipe.get(key)
                pipe.ttl(key)
                results = await pipe.execute()
            data_json = results[0]
            ttl = results[1] if isinstance(results[1], int) and results[1] >= 0 else (None if results[1] == -2 else -1) # 区分不存在 (-2) 和无 TTL (-1)

            if data_json:
                data = json.loads(data_json)
                last_updated = None
                if isinstance(data, dict):
                    last_updated = data.get("_internal_last_updated")
                    if not last_updated and isinstance(data.get("summary"), dict): # 兼容旧背包
                        last_updated = data["summary"].get("last_updated")
                return data, ttl, last_updated
            else:
                logger.debug(f"【数据管理器】缓存未命中: Key '{key}'")
                return None, ttl, None # 返回 None 数据
        except json.JSONDecodeError:
            logger.error(f"【数据管理器】解析 Redis Key '{key}' 的 JSON 失败。"); return None, None, None
        except Exception as e:
            logger.error(f"【数据管理器】读取 Redis Key '{key}' 时出错: {e}"); return None, None, None


    async def _get_data_generic(self, user_id: int, key_template: str, data_key_in_cache: Optional[str] = None, use_cache: bool = True):
        """通用获取缓存或实时数据的内部方法"""
        key = key_template.format(user_id)
        result_data = None

        if use_cache:
            cache_result = await self._get_cache_data(key)
            # cache_result 是 (data, ttl, last_updated)
            if cache_result and cache_result[0] is not None:
                data = cache_result[0]
                if data_key_in_cache and isinstance(data, dict):
                    result_data = data.get(data_key_in_cache)
                else:
                    result_data = data
                logger.debug(f"缓存命中 {key}")
                return result_data
            else:
                logger.info(f"缓存未命中或数据为空 {key}，将尝试强制刷新...")

        logger.info(f"强制刷新缓存 {key}...")
        username = self.context.telegram_client._my_username if self.context.telegram_client else None
        if not username:
            logger.error("无法强制刷新：缺少用户名。")
            return None

        if await self._update_cache_from_api_internal(user_id, username):
            result_after_update = await self._get_cache_data(key)
            if result_after_update and result_after_update[0] is not None:
                data_after = result_after_update[0]
                if data_key_in_cache and isinstance(data_after, dict):
                    result_data = data_after.get(data_key_in_cache)
                else:
                    result_data = data_after
                logger.debug(f"强制刷新后成功读取 {key}")
                return result_data
            else:
                logger.error(f"强制刷新成功，但再次读取缓存 {key} 失败或数据为空。")
                return None
        else:
            logger.error(f"强制刷新缓存 {key} 失败。")
            return None

    # --- 具体数据类型的获取方法 (保持不变) ---
    async def get_character_status(self, user_id: int, use_cache: bool = True) -> Optional[Dict]:
        return await self._get_data_generic(user_id, CHAR_STATUS_KEY, use_cache=use_cache)

    async def get_inventory(self, user_id: int, use_cache: bool = True) -> Optional[Dict]:
        return await self._get_data_generic(user_id, CHAR_INVENTORY_KEY, use_cache=use_cache)

    async def get_sect_info(self, user_id: int, use_cache: bool = True) -> Optional[Dict]:
        return await self._get_data_generic(user_id, CHAR_SECT_KEY, use_cache=use_cache)

    async def get_herb_garden(self, user_id: int, use_cache: bool = True) -> Optional[Dict]:
        """获取独立的药园缓存"""
        return await self._get_data_generic(user_id, CHAR_GARDEN_KEY, use_cache=use_cache)

    async def get_pagoda_progress(self, user_id: int, use_cache: bool = True) -> Optional[Dict]:
        """获取独立的闯塔缓存 (包含 progress, failed_floor 等)"""
        return await self._get_data_generic(user_id, CHAR_PAGODA_KEY, use_cache=use_cache)

    # --- 新增: 获取观星台数据 ---
    async def get_star_platform(self, user_id: int, use_cache: bool = True) -> Optional[Dict]:
        """获取独立的观星台缓存"""
        return await self._get_data_generic(user_id, CHAR_STAR_PLATFORM_KEY, use_cache=use_cache)
    # --- 新增结束 ---

    async def get_learned_recipes(self, user_id: int, use_cache: bool = True) -> Optional[List[str]]:
        """获取已学配方 ID 列表"""
        return await self._get_data_generic(user_id, CHAR_RECIPES_KEY, data_key_in_cache="known_ids", use_cache=use_cache)

    async def get_item_master_data(self, use_cache: bool = True) -> Optional[Dict]:
        """获取物品主数据 {item_id: {name, type}}"""
        if use_cache and self._item_master_cache:
            logger.debug("命中物品主数据内存缓存。")
            return self._item_master_cache

        cache_key = GAME_ITEMS_MASTER_KEY
        if use_cache:
            result = await self._get_cache_data(cache_key)
            if result and result[0] is not None and isinstance(result[0], dict):
                 items_data = result[0].get("items")
                 if isinstance(items_data, dict):
                      logger.debug("命中物品主数据 Redis 缓存，更新内存缓存。")
                      self._item_master_cache = items_data; return items_data
                 else: logger.error("物品主数据 Redis 缓存内部格式错误。")
            logger.warning("物品主数据缓存未命中或格式错误，尝试强制刷新...")
            if await self.update_item_master_cache():
                 return self._item_master_cache
            else:
                 return None
        else: # use_cache = False
            logger.info("强制刷新物品主数据缓存...")
            if await self.update_item_master_cache():
                 return self._item_master_cache
            else:
                 return None

    async def get_shop_data(self, user_id: int, use_cache: bool = True) -> Optional[Dict]:
        """获取商店数据 {item_id: {details}}"""
        key = GAME_SHOP_KEY.format(user_id)
        shop_items_dict = None
        if use_cache:
            result = await self._get_cache_data(key)
            if result and result[0] is not None and isinstance(result[0], dict):
                items_data = result[0].get("items")
                if isinstance(items_data, dict):
                     shop_items_dict = items_data
                     logger.debug(f"命中商店缓存 {key}")
                     return shop_items_dict
            logger.info(f"商店缓存未命中或数据为空 {key}，将尝试强制刷新...")

        logger.info(f"强制刷新商店缓存 {key}...")
        if await self.update_shop_cache(user_id):
             result_after_update = await self._get_cache_data(key)
             if result_after_update and result_after_update[0] is not None and isinstance(result_after_update[0], dict):
                  items_data_after = result_after_update[0].get("items")
                  if isinstance(items_data_after, dict):
                       shop_items_dict = items_data_after
                       logger.debug(f"强制刷新后成功读取商店缓存 {key}")
                       return shop_items_dict
             logger.error(f"强制刷新商店缓存成功，但再次读取缓存 {key} 失败或数据为空。")
             return None
        else:
            logger.error(f"强制刷新商店缓存 {key} 失败。")
            return None

    # --- 获取带 TTL 和更新时间的方法 (保持不变) ---
    async def get_cached_data_with_details(self, data_type: str, user_id: int) -> Tuple[Optional[Any], Optional[int], Optional[str]]:
        """
        获取指定类型的缓存数据及其 TTL 和上次更新时间。
        """
        key_map = {
            'status': CHAR_STATUS_KEY, 'inventory': CHAR_INVENTORY_KEY, 'sect': CHAR_SECT_KEY,
            'garden': CHAR_GARDEN_KEY, 'pagoda': CHAR_PAGODA_KEY, 'recipes': CHAR_RECIPES_KEY,
            'shop': GAME_SHOP_KEY, 'item_master': GAME_ITEMS_MASTER_KEY,
            'star_platform': CHAR_STAR_PLATFORM_KEY, # <-- 添加观星台映射
        }
        key_template = key_map.get(data_type)
        if not key_template:
             logger.error(f"无效的数据类型 '{data_type}' 请求 get_cached_data_with_details")
             return None, None, None
        key = key_template if data_type == 'item_master' else key_template.format(user_id)
        result = await self._get_cache_data(key)
        return result if result else (None, None, None)

    # --- 获取我的挂单 (仍为 Phase 3 待办) ---
    async def get_my_marketplace_listings(self, user_id: int, username: str, use_cache: bool = True) -> Optional[List[Dict]]:
         # TODO: 实现缓存逻辑 (需要新的 Redis Key 和更新策略)
         logger.warning("get_my_marketplace_listings 尚未实现缓存。")
         if self.http:
             data = await self.http.get_marketplace_listings(search_term=username)
             return data.get("listings") if isinstance(data, dict) else None
         return None

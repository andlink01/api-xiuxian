import logging
import asyncio
import json
import re
from typing import Optional, Dict, List, Tuple, Any # 添加 Any
from plugins.base_plugin import BasePlugin, AppContext
from core.context import get_global_context
from pyrogram.types import Message
import uuid # 用于生成请求 ID
from datetime import datetime, timedelta # 用于超时
import random # 增加 random 导入

# 导入 GDM Key 和常量
from modules.game_data_manager import CHAR_INVENTORY_KEY
from plugins.constants import GAME_CRAFTING_RECIPES_KEY
# 导入辅助函数 (现在 edit_or_reply 会返回 Message)
from plugins.utils import edit_or_reply, get_my_id

logger = logging.getLogger(__name__) # 使用标准 logger

# --- 常量 ---
DEFAULT_PAY_ITEM_NAME = "灵石" # 收货时默认支付物品
DEFAULT_PAY_QTY = 1        # 收货时默认支付数量
MATERIAL_WAIT_TIMEOUT = 300 # 等待材料到账的超时时间 (秒), 5分钟
MATERIAL_CHECK_INTERVAL = 15 # 检查材料是否到账的间隔 (秒)
LEARN_RECIPE_COMMAND_FORMAT = ".学习 {}" # 学习指令格式

# --- 辅助函数 ---
async def get_recipe(redis_client: Any, item_name: str) -> Optional[Dict[str, int]]: # 添加类型提示
    """从 Redis 获取指定物品名称的配方"""
    if not redis_client or not item_name: return None
    try:
        recipe_json = await redis_client.hget(GAME_CRAFTING_RECIPES_KEY, item_name)
        if recipe_json:
            recipe_dict = json.loads(recipe_json)
            if isinstance(recipe_dict, dict): return recipe_dict
            else: logger.error(f"配方 '{item_name}' 格式不正确: {recipe_json}"); return None
        else: logger.warning(f"未找到物品 '{item_name}' 的配方。"); return None
    except Exception as e: logger.error(f"获取配方 '{item_name}' 出错: {e}", exc_info=True); return None

# --- (修改: check_materials 接受 quantity 参数) ---
async def check_materials(inventory_data: Optional[Dict], recipe: Dict[str, int], item_master: Dict[str, Dict], quantity: int = 1) -> Tuple[bool, Dict[str, int]]:
    """检查背包数据是否满足配方需求 (考虑炼制数量)"""
    missing_materials: Dict[str, int] = {}
    if not inventory_data or not isinstance(inventory_data.get("items_by_type"), dict):
        logger.warning("检查材料：背包数据无效。")
        # 如果背包无效，返回所有材料都缺少 (乘以数量)
        total_required = {name: qty * quantity for name, qty in recipe.items()}
        return False, total_required

    current_materials: Dict[str, int] = {}
    items_by_type = inventory_data.get("items_by_type", {})
    all_items_flat: List[Dict] = [item for sublist in items_by_type.values() if isinstance(sublist, list) for item in sublist]

    for item in all_items_flat:
        if isinstance(item, dict) and "name" in item and "quantity" in item:
            # 累加同名物品的数量 (以防背包数据中有重复条目)
            current_materials[item["name"]] = current_materials.get(item["name"], 0) + item.get("quantity", 0)

    has_enough = True
    for material_name, required_qty_per_item in recipe.items():
        total_required_qty = required_qty_per_item * quantity # 计算总需求量
        available_qty = current_materials.get(material_name, 0)
        if available_qty < total_required_qty:
            has_enough = False
            missing_qty = total_required_qty - available_qty
            missing_materials[material_name] = missing_qty
            logger.info(f"材料检查 ({quantity}个): 缺少 '{material_name}', 需 {total_required_qty}, 有 {available_qty}, 缺 {missing_qty}")
        else:
             logger.debug(f"材料检查 ({quantity}个): '{material_name}' 充足, 需 {total_required_qty}, 有 {available_qty}")

    return has_enough, missing_materials
# --- (修改结束) ---

async def get_item_id_by_name_local(item_name: str, item_master: Dict[str, Dict]) -> Optional[str]:
    """在本插件内部通过名称查找物品 ID"""
    if not item_name or not item_master: return None
    name_to_find = item_name.strip()
    for item_id, details in item_master.items():
        if isinstance(details, dict) and details.get("name") == name_to_find:
            return item_id
    logger.warning(f"物品主数据中未找到名称为 '{name_to_find}' 的物品。")
    return None

async def find_recipe_item_in_inventory(inventory_data: Optional[Dict], recipe_item_id: str) -> bool:
    """检查背包中是否存在指定的配方物品 ID"""
    if not inventory_data or not isinstance(inventory_data.get("items_by_type"), dict) or not recipe_item_id:
        return False
    recipe_list = inventory_data.get("items_by_type", {}).get("recipe", [])
    if isinstance(recipe_list, list):
        for item in recipe_list:
            if isinstance(item, dict) and item.get("item_id") == recipe_item_id:
                return True
    return False

async def get_total_material_availability(context: AppContext, crafter_id: int, missing_materials: Dict[str, int], item_master: Dict[str, Dict]) -> Tuple[Dict[str, int], Dict[str, int]]:
    """检查所有其他助手总共拥有多少缺少的材料 (返回 total_available, shortfall)"""
    total_available: Dict[str, int] = {name: 0 for name in missing_materials}
    shortfall: Dict[str, int] = missing_materials.copy() # 先假设全部缺少
    redis_client = context.redis.get_client()
    if not redis_client: return total_available, shortfall
    if not context.data_manager: # 增加 data_manager 检查
        logger.error("无法检查助手库存：DataManager 未初始化。")
        return total_available, shortfall

    logger.info("开始扫描其他助手的库存...")
    try:
        # --- (修改: 使用 scan_iter 匹配 char:inventory:* ) ---
        async for inv_key in redis_client.scan_iter(match=f"{CHAR_INVENTORY_KEY.format('*')}"):
        # --- (修改结束) ---
            try:
                assistant_id_str = inv_key.split(':')[-1]
                if not assistant_id_str.isdigit(): continue
                assistant_id = int(assistant_id_str)
                if assistant_id == crafter_id: continue

                # 使用 DataManager 获取缓存，减少 Redis 直接读取压力
                inv_data = await context.data_manager.get_inventory(assistant_id, use_cache=True)
                if not inv_data or not isinstance(inv_data.get("items_by_type"), dict): continue

                current_assistant_materials: Dict[str, int] = {}
                items_by_type = inv_data.get("items_by_type", {})
                all_items_flat_other: List[Dict] = [item for sublist in items_by_type.values() if isinstance(sublist, list) for item in sublist]
                for item in all_items_flat_other:
                    if isinstance(item, dict) and "name" in item and "quantity" in item:
                        current_assistant_materials[item["name"]] = current_assistant_materials.get(item["name"], 0) + item.get("quantity", 0) # 累加同名物品

                for mat_name, needed_qty in list(shortfall.items()): # 遍历 shortfall 的副本，因为可能在循环中删除
                    qty_on_assistant = current_assistant_materials.get(mat_name, 0)
                    if qty_on_assistant > 0:
                        total_available[mat_name] += qty_on_assistant
                        can_provide = min(needed_qty, qty_on_assistant) # 该助手能提供的数量
                        if can_provide > 0:
                            shortfall[mat_name] -= can_provide
                            if shortfall[mat_name] <= 0:
                                del shortfall[mat_name] # 从缺口字典中移除

            except Exception as check_e: logger.error(f"检查助手 {inv_key} 库存时出错: {check_e}")
    except Exception as scan_e: logger.error(f"扫描 Redis 库存键时出错: {scan_e}")

    final_shortfall = {k: v for k, v in shortfall.items() if v > 0} # 最终仍然缺少的
    logger.info(f"库存扫描完成。总共可从其他助手获取: {total_available}, 最终缺口: {final_shortfall}")
    return total_available, final_shortfall


# --- 插件类 ---
class Plugin(BasePlugin):
    """处理智能炼制指令的插件"""
    def __init__(self, context: AppContext, plugin_name: str, cn_name: str | None = None):
        super().__init__(context, plugin_name, cn_name or "智能炼制")
        self.info("插件已加载。")
        self._active_crafting_tasks: Dict[str, asyncio.Task] = {}

    def register(self):
        """注册指令事件监听器"""
        # --- (修改: 监听器签名改变，增加 quantity) ---
        self.event_bus.on("smart_crafting_command", self.handle_smart_crafting_command)
        # --- (修改结束) ---
        self.info("已注册 smart_crafting_command 事件监听器。")

    # --- (修改: 增加 quantity 参数) ---
    async def handle_smart_crafting_command(self, message: Message, item_name: str, quantity: int, edit_target_id: int | None):
    # --- (修改结束) ---
        self.info(f"收到智能炼制指令: 炼制 '{item_name}' x{quantity}")

        task_id = f"craft_{uuid.uuid4()}"
        if self._active_crafting_tasks:
             is_running = any(not task.done() for task in self._active_crafting_tasks.values())
             if is_running:
                 await edit_or_reply(self, message.chat.id, edit_target_id, f"⚠️ 已有另一个智能炼制任务正在进行中，请稍后再试。", original_message=message)
                 return

        # --- (修改: 传递 quantity, 传入 None 作为 edit_target_id) ---
        task = asyncio.create_task(self._execute_smart_crafting(message, item_name, quantity, None, task_id)) # 初始 edit_target_id 设为 None
        # --- (修改结束) ---
        self._active_crafting_tasks[task_id] = task
        task.add_done_callback(lambda t: self._active_crafting_tasks.pop(task_id, None))

    # --- (修改: 增加 quantity 参数, 使用 status_message_id) ---
    async def _execute_smart_crafting(self, message: Message, item_name: str, quantity: int, _: int | None, task_id: str): # edit_target_id 不再使用，用 _ 占位
        """执行智能炼制的后台任务"""
        status_message_id: Optional[int] = None # 用于存储状态消息的 ID

        async def update_status(text: str):
            """内部辅助函数，用于发送或编辑状态消息"""
            nonlocal status_message_id
            status_msg = await edit_or_reply(self, message.chat.id, status_message_id, text, original_message=message)
            if status_msg and status_message_id is None: # 如果是第一次发送且成功
                status_message_id = status_msg.id
            elif status_msg is None and status_message_id is None: # 如果第一次发送就失败
                logger.error("无法发送初始状态消息，任务中止。")
                raise Exception("Failed to send initial status message") # 抛出异常中断任务

        try:
            crafter_id = await get_my_id(self, message, None) # 获取 ID 时无需编辑
            crafter_username = self.context.telegram_client._my_username if self.context.telegram_client else None
            if not crafter_id or not crafter_username:
                await update_status("❌ 无法获取当前助手信息，无法执行。")
                return
            admin_id = self.config.get("telegram.admin_id")
            is_self = (admin_id is not None and crafter_id == admin_id)
            self.info(f"炼制者确定为当前助手: {crafter_username} (ID: {crafter_id}), 是否管理员: {is_self}")

            redis_client = self.redis.get_client()
            if not self.data_manager or not redis_client or not self.context.telegram_client or not self.context.event_bus:
                await update_status("❌ 内部错误：缺少核心服务。")
                return

            # 0. 获取物品主数据
            await update_status(f"⏳ 正在加载基础数据...")
            item_master = await self.data_manager.get_item_master_data(use_cache=True)
            if not item_master:
                await update_status("❌ 无法获取物品主数据，无法继续。")
                return

            target_item_id = await get_item_id_by_name_local(item_name, item_master)
            if not target_item_id:
                 await update_status(f"❌ 在物品主数据中找不到物品 '{item_name}'。")
                 return

            # --- 修改: 更健壮地查找配方ID和名称 ---
            actual_recipe_id: Optional[str] = None
            actual_recipe_name: Optional[str] = None
            possible_suffixes = ["丹方", "图纸", "阵图", "配方"] # 可能的配方后缀

            # 尝试直接通过产品ID构造 recipe_ID 查找
            potential_recipe_id = f"recipe_{target_item_id}"
            if potential_recipe_id in item_master and item_master[potential_recipe_id].get("type") == "recipe":
                 actual_recipe_id = potential_recipe_id
                 actual_recipe_name = item_master[actual_recipe_id].get("name", item_name + "配方") # 使用 item master 中的名字
                 logger.info(f"通过构造 recipe_{target_item_id} 找到配方 ID: {actual_recipe_id}, 名称: {actual_recipe_name}")
            else:
                 # 尝试通过名称匹配 (产品名称 + 后缀)
                 logger.debug(f"未通过构造找到配方 ID，尝试通过名称 '{item_name}' + 后缀查找...")
                 for item_id, details in item_master.items():
                     if details.get("type") == "recipe":
                         recipe_name = details.get("name", "")
                         # 检查是否以 产品名+后缀 结尾
                         is_match = False
                         for suffix in possible_suffixes:
                             if recipe_name == item_name + suffix:
                                 is_match = True
                                 break
                         # 如果不匹配，再检查是否以 产品名 开头 (作为备选)
                         if not is_match and recipe_name.startswith(item_name):
                              is_match = True # 容错

                         if is_match:
                             actual_recipe_id = item_id
                             actual_recipe_name = recipe_name
                             logger.info(f"通过名称匹配找到配方 ID: {actual_recipe_id}, 名称: {actual_recipe_name}")
                             break # 找到第一个就停止

            if not actual_recipe_id:
                self.warning(f"无法在物品主数据中明确找到物品 '{item_name}' (ID: {target_item_id}) 对应的配方物品 ID 和名称。将跳过学习检查。")
            # --- 修改结束 ---

            # 1. 获取配方
            await update_status(f"⏳ 正在查找 '{item_name}' 的配方...")
            recipe_materials = await get_recipe(redis_client, item_name)
            if not recipe_materials:
                await update_status(f"❌ 未找到物品 '{item_name}' 的配方。请先使用 `,更新配方` 添加。")
                return

            # --- 修改: 使用 actual_recipe_id 和 actual_recipe_name ---
            # 2. 配方学习检查
            if actual_recipe_id and actual_recipe_name: # 只有在找到确切配方信息时才检查
                await update_status(f"⏳ 正在检查 {crafter_username} 是否已学习配方 '{actual_recipe_name}'...")
                learned_recipes_list = await self.data_manager.get_learned_recipes(crafter_id, use_cache=False)
                if learned_recipes_list is None:
                    await update_status(f"❌ 无法获取 {crafter_username} 已学习的配方列表。"); return

                if actual_recipe_id not in learned_recipes_list:
                    self.warning(f"助手 {crafter_username} 未学习配方 '{actual_recipe_name}' (ID: {actual_recipe_id})。检查背包...")
                    await update_status(f"⚠️ {crafter_username} 未学习配方，正在检查背包...")
                    inventory_data_learn = await self.data_manager.get_inventory(crafter_id, use_cache=False)
                    recipe_item_found = await find_recipe_item_in_inventory(inventory_data_learn, actual_recipe_id)

                    if recipe_item_found:
                        learn_command = LEARN_RECIPE_COMMAND_FORMAT.format(actual_recipe_name) # 使用配方物品名称
                        self.info(f"找到配方物品 '{actual_recipe_name}'，准备发送学习指令 '{learn_command}'...")
                        await update_status(f"⏳ 找到配方物品，正在发送学习指令 `.学习 {actual_recipe_name}`...")
                        learn_success = await self.context.telegram_client.send_game_command(learn_command)
                        if learn_success: await update_status(f"✅ 配方未学习，已找到配方物品并发送学习指令。\n请在学习成功后重新尝试炼制。")
                        else: await update_status(f"❌ 配方未学习，找到配方物品但发送学习指令失败！")
                        return
                    else:
                        self.error(f"助手 {crafter_username} 未学习配方 '{actual_recipe_name}' 且背包中无此配方。")
                        await update_status(f"❌ 炼制失败：{crafter_username} 未学习配方 '{actual_recipe_name}'，且背包中也未找到该配方。")
                        return
                else: self.info(f"助手 {crafter_username} 已学习配方 '{actual_recipe_name}'。")
            else: self.warning(f"无法确定配方物品 ID/名称 for '{item_name}'，跳过学习检查。")
            # --- 修改结束 ---

            # --- 3. 材料检查与收集循环 ---
            materials_gathered = False
            final_missing_report = ""

            for check_attempt in range(3): # 最多尝试3轮收集
                self.info(f"第 {check_attempt + 1} 次检查材料 (炼制 {quantity} 个)...")
                await update_status(f"⏳ 第 {check_attempt + 1} 次检查 {crafter_username} 的材料 (炼制 {quantity} 个)...")

                inventory_data = await self.data_manager.get_inventory(crafter_id, use_cache=False) # 强制刷新背包
                if not inventory_data:
                    await update_status(f"❌ 无法获取 {crafter_username} 的背包信息。")
                    return

                # --- (修改: 调用 check_materials 时传递 quantity) ---
                has_enough, missing_materials = await check_materials(inventory_data, recipe_materials, item_master, quantity)
                # --- (修改结束) ---

                if has_enough:
                    self.info(f"炼制者 {crafter_username} 材料足够炼制 '{item_name}' x{quantity}。")
                    await update_status(f"✅ {crafter_username} 材料足够 (炼制 {quantity} 个)，准备发送炼制指令...")
                    materials_gathered = True
                    break # 材料足够，跳出收集循环

                # --- 材料不足，开始收集逻辑 ---
                self.warning(f"炼制者 {crafter_username} 材料不足炼制 '{item_name}' x{quantity}。缺少: {missing_materials}")
                missing_str_lines = [f"`{name}` x{qty}" for name, qty in missing_materials.items()]
                missing_text = "\n".join(missing_str_lines)
                await update_status(f"⚠️ {crafter_username} 材料不足 (炼制 {quantity} 个)，缺少：\n{missing_text}\n⏳ 正在检查其他助手库存...")

                # 检查所有助手总库存是否够
                total_available, final_shortfall = await get_total_material_availability(self.context, crafter_id, missing_materials, item_master)

                if final_shortfall: # 如果最终缺口字典不为空 (所有助手加起来都不够)
                    final_missing_report_lines = [f"`{name}` (缺 {qty})" for name, qty in final_shortfall.items()]
                    final_missing_report = ", ".join(final_missing_report_lines)
                    self.error(f"所有助手库存总和仍不足以炼制 '{item_name}' x{quantity}。最终缺少: {final_missing_report}")
                    await update_status(f"❌ 所有助手库存总和不足！(炼制 {quantity} 个)\n最终缺少: {final_missing_report}\n炼制任务 '{item_name}' 已停止。")
                    return # 任务失败，退出

                # --- 总库存足够，触发收货流程 ---
                materials_to_request = missing_materials # 需要请求的就是当前缺少的
                self.info(f"库存足够，准备为缺少的材料发布 {len(materials_to_request)} 个转账请求...")
                await update_status(f"⏳ 其他助手库存足够，正在为缺少的 {len(materials_to_request)} 种材料发布转账请求...")

                request_channel = self.config.get("marketplace_transfer.request_channel")
                if not self.context.redis or not request_channel:
                     await update_status("❌ 无法发布转账请求：Redis 或请求频道未配置。"); return

                pay_item_name_default = self.config.get("marketplace_transfer.default_pay_item_name", DEFAULT_PAY_ITEM_NAME)
                pay_qty_default = self.config.get("marketplace_transfer.default_pay_quantity", DEFAULT_PAY_QTY)
                pay_item_id_default = await get_item_id_by_name_local(pay_item_name_default, item_master)
                if not pay_item_id_default:
                     await update_status(f"❌ 无法获取默认支付物品 '{pay_item_name_default}' 的 ID。"); return

                publish_success_count = 0; publish_fail_count = 0; all_request_ids = []
                for mat_name, qty_needed in materials_to_request.items():
                    mat_id = await get_item_id_by_name_local(mat_name, item_master)
                    if not mat_id: self.error(f"无法获取材料 '{mat_name}' 的 ID"); publish_fail_count += 1; continue
                    req_id = f"craft_req_{task_id}_{mat_id}_{random.randint(100, 999)}"; all_request_ids.append(req_id)
                    request_data = {
                        "request_id": req_id, "recipient_id": crafter_id, "recipient_username": crafter_username,
                        "receive_item_id": mat_id, "receive_item_name": mat_name, "receive_qty": qty_needed,
                        "pay_item_id": pay_item_id_default, "pay_item_name": pay_item_name_default, "pay_qty": pay_qty_default,
                        "timestamp": datetime.utcnow().isoformat(), "origin": "smart_crafting"
                    }
                    pub_success = await self.context.redis.publish(request_channel, request_data)
                    if pub_success: publish_success_count += 1; self.info(f"已为 '{mat_name}' x{qty_needed} 发布转账请求 (ID: {req_id})。")
                    else: publish_fail_count += 1; self.error(f"为 '{mat_name}' x{qty_needed} 发布转账请求失败！")

                await update_status(f"⏳ 已发布 {publish_success_count} 个材料请求，失败 {publish_fail_count} 个。\n⏳ 等待材料到账 (最多 {MATERIAL_WAIT_TIMEOUT} 秒)...")
                if publish_fail_count > 0: self.warning("部分材料请求发布失败，可能无法完成收集。")

                # --- 等待材料到账 ---
                wait_start_time = asyncio.get_event_loop().time()
                materials_arrived = False
                while asyncio.get_event_loop().time() - wait_start_time < MATERIAL_WAIT_TIMEOUT:
                    await asyncio.sleep(MATERIAL_CHECK_INTERVAL)
                    remaining_time = int(MATERIAL_WAIT_TIMEOUT - (asyncio.get_event_loop().time() - wait_start_time))
                    self.info(f"等待材料中... {remaining_time}s 剩余...")
                    await update_status(f"⏳ 等待材料到账 ({remaining_time} 秒)...")

                    inv_data_wait = await self.data_manager.get_inventory(crafter_id, use_cache=False) # 再次强制刷新
                    if not inv_data_wait: continue

                    # --- (修改: 调用 check_materials 时传递 quantity) ---
                    has_enough_now, still_missing_now = await check_materials(inv_data_wait, recipe_materials, item_master, quantity)
                    # --- (修改结束) ---
                    if has_enough_now: self.info("材料已全部到账！"); materials_arrived = True; break
                    else: self.info(f"材料收集中... 仍缺少: {still_missing_now}")

                if not materials_arrived: # 超时
                    self.error(f"等待材料超时 ({MATERIAL_WAIT_TIMEOUT} 秒)！")
                    inv_data_final = await self.data_manager.get_inventory(crafter_id, use_cache=False)
                    # --- (修改: 调用 check_materials 时传递 quantity) ---
                    _, final_missing = await check_materials(inv_data_final or {}, recipe_materials, item_master, quantity)
                    # --- (修改结束) ---
                    final_missing_lines = [f"`{name}` x{qty}" for name, qty in final_missing.items()]
                    final_missing_text = "\n".join(final_missing_lines) if final_missing_lines else "未知"
                    await update_status(f"❌ 等待材料超时！(炼制 {quantity} 个)\n最终仍缺少:\n{final_missing_text}\n炼制任务 '{item_name}' 已停止。")
                    return # 任务失败，退出

                # 材料已到账，继续下一次外层循环检查（将在下次循环开始时成功并 break）
                self.info("材料收集完成，进入下一轮检查确认。")
                await update_status(f"⏳ 材料收集完成，正在进行最终确认...")
            # --- 材料检查与收集循环结束 ---

            # --- 4. 发送炼制指令 ---
            if materials_gathered:
                # --- (修改: 构建带数量的炼制指令) ---
                craft_command = f".炼制 {item_name}*{quantity}"
                # --- (修改结束) ---
                self.info(f"最终确认材料足够，准备将炼制指令 '{craft_command}' 加入队列...")
                queue_success = await self.context.telegram_client.send_game_command(craft_command)
                if queue_success:
                    await update_status(f"✅ 材料已集齐！炼制指令 `{craft_command}` 已成功加入队列。")
                else:
                    await update_status(f"❌ 材料已集齐，但将炼制指令 `{craft_command}` 加入队列失败！")
            else:
                 # 理论上应该在收集循环中因为 final_shortfall 或超时而返回了
                 await update_status(f"❌ 未知错误：材料收集流程结束但未确认集齐 (炼制 {quantity} 个)。最终缺少: {final_missing_report or '未知'}")

        except Exception as e:
            logger.error(f"执行智能炼制任务 (ID: {task_id}) 时发生意外错误: {e}", exc_info=True)
            try:
                await update_status(f"❌ 执行智能炼制时发生内部错误: {str(e)[:100]}")
            except Exception as final_e:
                 logger.critical(f"在处理智能炼制主异常后发送最终状态时再次出错: {final_e}")


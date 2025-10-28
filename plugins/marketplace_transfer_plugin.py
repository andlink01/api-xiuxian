import logging
import asyncio
import json
import random
import re
import uuid # 用于生成唯一请求 ID
from datetime import datetime
from typing import Optional, Dict, List, Tuple, Any, Coroutine
from plugins.base_plugin import BasePlugin, AppContext
from core.context import get_global_context
from pyrogram.types import Message
# --- 修改: 导入 GameDataManager 定义的 Key ---
from modules.game_data_manager import (
    CHAR_INVENTORY_KEY, GAME_ITEMS_MASTER_KEY
)
# --- 修改结束 ---

# --- 常量 ---
TRANSFER_COMMAND = ",收货" # 管理员指令
FIND_LISTING_RETRY_DELAY = 5 # 查找挂单失败后的重试延迟（秒）
FIND_LISTING_MAX_ATTEMPTS = 12 # 最多尝试查找挂单次数 (5 * 12 = 60 秒)
REDIS_ORDER_EXEC_LOCK_PREFIX = "marketplace_order_exec:lock:" # 防止重复执行订单的锁

# --- 辅助函数 ---
_item_master_cache: Dict[str, Dict] = {} # 内存缓存物品主数据

async def _get_item_master_data(context: AppContext) -> Dict[str, Dict]:
    """获取物品主数据 (带内存缓存, 通过 DataManager)"""
    global _item_master_cache
    if _item_master_cache:
        return _item_master_cache
    # --- 修改: 通过 DataManager 获取 ---
    if not context.data_manager:
        logging.getLogger("MarketplaceTransferPlugin.Utils").error("无法获取物品主数据：DataManager 未初始化。")
        return {}
    # 使用 DataManager 的方法获取数据
    items_data = await context.data_manager.get_item_master_data(use_cache=True)
    if items_data:
        _item_master_cache = items_data # 更新内存缓存
        logging.getLogger("MarketplaceTransferPlugin.Utils").debug(f"物品主数据已从 DataManager 加载到内存缓存，共 {len(_item_master_cache)} 条。")
    else:
        _item_master_cache = {} # 获取失败则清空
        logging.getLogger("MarketplaceTransferPlugin.Utils").warning("无法从 DataManager 获取物品主数据。")
    return _item_master_cache
    # --- 修改结束 ---

async def get_item_id_by_name(context: AppContext, item_name: str) -> Optional[str]:
    """通过名称查找物品 ID"""
    if not item_name: return None
    master_data = await _get_item_master_data(context) # 使用更新后的函数
    if not master_data:
        logging.getLogger("MarketplaceTransferPlugin.Utils").error("物品主数据为空，无法通过名称查找 ID。")
        return None
    name_to_find = item_name.strip()
    for item_id, details in master_data.items():
        if isinstance(details, dict) and details.get("name") == name_to_find:
            return item_id
    logging.getLogger("MarketplaceTransferPlugin.Utils").warning(f"物品主数据中未找到名称为 '{name_to_find}' 的物品。")
    return None

async def get_item_name_by_id(context: AppContext, item_id: str) -> Optional[str]:
    """通过 ID 查找物品名称"""
    if not item_id: return None
    master_data = await _get_item_master_data(context) # 使用更新后的函数
    if not master_data:
        logging.getLogger("MarketplaceTransferPlugin.Utils").error("物品主数据为空，无法通过 ID 查找名称。")
        return item_id # 返回原始 ID 作为后备
    item_details = master_data.get(item_id)
    return item_details.get("name", item_id) if isinstance(item_details, dict) else item_id

async def get_inventory_item_quantity(context: AppContext, user_id: int, item_id_to_check: str) -> int:
    """获取指定用户背包中指定物品 ID 的数量 (直接查 Redis，使用新 Key)"""
    redis_client = context.redis.get_client()
    if not redis_client or not item_id_to_check: return 0
    inv_key = CHAR_INVENTORY_KEY.format(user_id) # 使用新 Key
    try:
        inv_data_json = await redis_client.get(inv_key)
        if inv_data_json:
            inv_data = json.loads(inv_data_json)
            items_by_type = inv_data.get("items_by_type", {})
            for item_type, items in items_by_type.items():
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict) and item.get("item_id") == item_id_to_check:
                            qty = item.get("quantity", 0)
                            return int(qty) if isinstance(qty, (int, float, str)) and str(qty).isdigit() else 0
    except Exception as e:
        logging.getLogger("MarketplaceTransferPlugin.Utils").error(f"获取用户 {user_id} 背包物品数量时出错 (Key: {inv_key}, ID: {item_id_to_check}): {e}")
    return 0

# --- 插件类 ---
class Plugin(BasePlugin):
    """
    处理多账号资源转移的插件 (基于 Redis Pub/Sub)
    """
    def __init__(self, context: AppContext, plugin_name: str, cn_name: str | None = None):
        super().__init__(context, plugin_name, cn_name)
        self.load_config()
        self._my_id: Optional[int] = None
        self._my_username: Optional[str] = None
        self._is_admin_instance: bool = False
        self._active_buy_tasks: Dict[str, asyncio.Task] = {}

        if self.auto_enabled:
            self.info(f"插件已加载。管理员指令 '{TRANSFER_COMMAND}'. PubSub 请求频道 '{self.request_channel}', 指派频道 '{self.order_channel}', 结果频道 '{self.result_channel}'")
        else:
            self.info("插件已加载但【未启用】。")

    async def _initialize_plugin(self):
        """异步初始化，获取自身信息，判断角色，订阅频道"""
        if not self.context.telegram_client or not self.context.telegram_client.app.is_connected:
            self.error("无法初始化市场插件：Telegram 客户端未连接。")
            return

        self._my_id = await self.context.telegram_client.get_my_id()
        self._my_username = await self.context.telegram_client.get_my_username()

        if not self._my_id or not self._my_username:
             self.error("无法获取自身 ID 或用户名，插件功能受限！")
             return

        admin_id_config = self.config.get("telegram.admin_id")
        self._is_admin_instance = (admin_id_config is not None and self._my_id == admin_id_config)

        self.info(f"实例初始化完成: ID={self._my_id}, Username={self._my_username}, 是否管理实例={self._is_admin_instance}")

        await _get_item_master_data(self.context) # 预加载物品数据
        self.info("物品主数据已预加载到内存缓存。")

        if self.context.redis:
            if self._is_admin_instance and self.request_channel:
                 await self.context.redis.subscribe(self.request_channel, self.handle_transfer_request)
                 self.info(f"已订阅交易请求频道: {self.request_channel}")
            if self.order_channel:
                await self.context.redis.subscribe(self.order_channel, self.handle_assigned_order)
                self.info(f"已订阅共享交易指令频道: {self.order_channel}")
            if self._is_admin_instance and self.result_channel:
                 await self.context.redis.subscribe(self.result_channel, self.handle_transfer_result)
                 self.info(f"已订阅交易结果频道: {self.result_channel}")
        else:
            self.error("Redis 不可用，无法订阅 Pub/Sub 频道！")

    def load_config(self):
        """加载配置"""
        self.auto_enabled = self.config.get("marketplace_transfer.enabled", True)
        self.default_pay_item_name = self.config.get("marketplace_transfer.default_pay_item_name", "灵石")
        self.default_pay_quantity = self.config.get("marketplace_transfer.default_pay_quantity", 1)
        self.request_channel = self.config.get("marketplace_transfer.request_channel", "marketplace:requests")
        self.order_channel = self.config.get("marketplace_transfer.order_channel", "marketplace:orders")
        self.result_channel = self.config.get("marketplace_transfer.result_channel", "marketplace:results")

    def register(self):
        """注册事件监听器"""
        if not self.auto_enabled:
            return
        self.event_bus.on("admin_command_received", self.handle_admin_command)
        self.event_bus.on("telegram_client_started", self._initialize_plugin)
        self.info(f"已注册管理员指令和 TG 启动监听器。Pub/Sub 订阅将在初始化时进行。")

    # --- 接收方逻辑 (handle_admin_command 已在上次修改中优化) ---
    async def handle_admin_command(self, message: Message, my_username: str | None):
        """处理管理员发来的 ,收货 指令 (由接收方机器人处理)"""
        # ... (代码与上一次提供的相同，保持不变) ...
        if not self._my_id or not self._my_username:
             if not self._my_id:
                 self.warning("收到收货指令，但插件尚未初始化，等待 TG 客户端信息...")
                 await asyncio.sleep(1)
                 if not self._my_id:
                      self.error("插件未完全初始化，无法处理收货指令。")
                      try: await message.reply_text("❌ 错误：市场插件尚未完全初始化，请稍后再试。", quote=True)
                      except: pass
                      return
             else: self.error("插件初始化不完整 (缺少用户名?)，无法处理收货指令。"); return

        raw_text = message.text or message.caption; command_prefix = None
        if raw_text:
             if raw_text.lower().startswith(TRANSFER_COMMAND): command_prefix = TRANSFER_COMMAND
             elif raw_text.lower().startswith(TRANSFER_COMMAND.lstrip(',')): command_prefix = TRANSFER_COMMAND.lstrip(',')
        if not command_prefix: return

        args_text = raw_text[len(command_prefix):].strip(); parts = args_text.split(); num_parts = len(parts)
        receive_item_name: Optional[str] = None; receive_qty: Optional[int] = None
        pay_item_name: str = self.default_pay_item_name; pay_qty: int = self.default_pay_quantity
        error_msg = None

        if num_parts == 2:
            try: receive_item_name = parts[0]; receive_qty = int(parts[1])
            except ValueError: error_msg = f"❌ 解析数量失败: '{parts[1]}' 不是有效数字。"
            except IndexError: error_msg = "❌ 指令格式错误 (参数不足)。"
        elif num_parts == 4:
            try: receive_item_name = parts[0]; receive_qty = int(parts[1]); pay_item_name = parts[2]; pay_qty = int(parts[3])
            except ValueError: error_msg = f"❌ 解析数量失败: '{parts[1]}' 或 '{parts[3]}' 不是有效数字。"
            except IndexError: error_msg = "❌ 指令格式错误 (参数不足)。"
        else: error_msg = "❌ 指令格式错误。\n用法1: `,收货 <物品> <数量>` (默认用1灵石换)\n用法2: `,收货 <需求物品> <需求数量> <支付物品> <支付数量>`"

        reply_target = message
        if error_msg:
            try: await reply_target.reply_text(error_msg, quote=True)
            except Exception:
                if self.context.telegram_client: await self.context.telegram_client.send_admin_reply(error_msg, original_message=message)
            return

        if receive_qty is None or pay_qty is None or receive_qty <= 0 or pay_qty <= 0:
            msg = "❌ 物品数量和支付数量必须大于 0。"
            try: await reply_target.reply_text(msg, quote=True)
            except Exception:
                 if self.context.telegram_client: await self.context.telegram_client.send_admin_reply(msg, original_message=message)
            return

        pay_item_id = await get_item_id_by_name(self.context, pay_item_name)
        receive_item_id = await get_item_id_by_name(self.context, receive_item_name)

        if not pay_item_id:
            msg = f"❌ 找不到支付物品 '{pay_item_name}' 的 ID。请先 `,同步物品`。"
            try: await reply_target.reply_text(msg, quote=True)
            except Exception:
                 if self.context.telegram_client: await self.context.telegram_client.send_admin_reply(msg, original_message=message)
            return
        if not receive_item_id:
            msg = f"❌ 找不到需求物品 '{receive_item_name}' 的 ID。请先 `,同步物品`。"
            try: await reply_target.reply_text(msg, quote=True)
            except Exception:
                if self.context.telegram_client: await self.context.telegram_client.send_admin_reply(msg, original_message=message)
            return

        self.info(f"收到收货指令: 求购 '{receive_item_name}'(ID:{receive_item_id}) x{receive_qty}, 支付 '{pay_item_name}'(ID:{pay_item_id}) x{pay_qty}")
        post_command = f".上架 {pay_item_name}*{pay_qty} 换 {receive_item_name}*{receive_qty}"

        self.info(f"准备将上架指令 '{post_command}' 加入队列...")
        queue_success = await self.context.telegram_client.send_game_command(post_command)

        response_lines = []; redis_pub_status = "未尝试"; request_id = "N/A"

        if queue_success:
            response_lines.append(f"✅ 上架指令 '{post_command}' 已加入队列。")
            self.info(f"指令 '{post_command}' 加入队列成功。")
            request_id = str(uuid.uuid4())
            request_data = {
                "request_id": request_id, "recipient_id": self._my_id, "recipient_username": self._my_username,
                "receive_item_id": receive_item_id, "receive_item_name": receive_item_name, "receive_qty": receive_qty,
                "pay_item_id": pay_item_id, "pay_item_name": pay_item_name, "pay_qty": pay_qty,
                "timestamp": datetime.utcnow().isoformat()
            }
            if self.context.redis and self.request_channel:
                pub_success = await self.context.redis.publish(self.request_channel, request_data)
                if pub_success:
                    redis_pub_status = "成功"; response_lines.append(f"✅ 交易请求 (ID: {request_id[:8]}...) 已发布。")
                    self.info(f"成功发布交易请求 (ID: {request_id}) 到频道 '{self.request_channel}'")
                else:
                    redis_pub_status = "失败"; response_lines.append(f"⚠️ 发布交易请求到 Redis 失败！请检查 Redis 连接。")
                    self.error(f"发布交易请求 (ID: {request_id}) 到频道 '{self.request_channel}' 失败！")
            else:
                 redis_pub_status = "失败 (Redis不可用/频道未配置)"; response_lines.append(f"⚠️ 无法发布交易请求：Redis 不可用或未配置频道！")
                 self.error("无法发布交易请求：Redis 不可用或未配置请求频道。")
        else:
            response_lines.append(f"❌ 将上架指令 '{post_command}' 加入队列失败！")
            self.error(f"将上架指令 '{post_command}' 加入队列失败！")

        final_response_msg = "\n".join(response_lines)
        try: await reply_target.reply_text(final_response_msg, quote=True)
        except Exception:
            if self.context.telegram_client: await self.context.telegram_client.send_admin_reply(final_response_msg, original_message=message)


    # --- 管理员决策逻辑 (handle_transfer_request) ---
    async def handle_transfer_request(self, channel: str, data: Any):
        """处理从 Redis Pub/Sub 收到的交易请求 (仅管理实例执行)"""
        # ... (代码与上一次提供的相同，保持不变，内部已使用更新后的 get_inventory_item_quantity) ...
        if not self._is_admin_instance: return
        self.info(f"收到交易请求: {data}")
        if not isinstance(data, dict) or not all(k in data for k in ["request_id", "recipient_id", "recipient_username", "receive_item_id", "receive_qty", "pay_item_id", "pay_qty"]):
            self.error(f"收到的交易请求格式无效: {data}"); return
        request_id = data["request_id"]; recipient_id = data["recipient_id"]
        receive_item_id = data["receive_item_id"]; receive_qty = data["receive_qty"]
        receive_item_name = data.get("receive_item_name", await get_item_name_by_id(self.context, receive_item_id) or "未知物品")
        suitable_seller_id: Optional[int] = None
        redis_client = self.context.redis.get_client()
        if not redis_client: self.error("无法查找卖家：Redis 未连接。"); return
        try:
            async for inv_key in redis_client.scan_iter(match=f"{CHAR_INVENTORY_KEY.format('*')}"):
                try:
                    seller_id_str = inv_key.split(':')[-1]
                    if not seller_id_str.isdigit(): continue
                    seller_id = int(seller_id_str)
                    if seller_id == recipient_id: continue
                    seller_qty = await get_inventory_item_quantity(self.context, seller_id, receive_item_id)
                    self.debug(f"检查潜在卖家 {seller_id} 库存: 有 {seller_qty} / 需要 {receive_qty} 个 '{receive_item_name}'")
                    if seller_qty >= receive_qty:
                        suitable_seller_id = seller_id; self.info(f"找到合适的卖家: {suitable_seller_id}"); break
                except Exception as check_e: self.error(f"检查卖家 {inv_key} 库存时出错: {check_e}")

            if suitable_seller_id:
                order_data = {"designated_seller_id": suitable_seller_id, **data}
                self.info(f"准备向共享频道 '{self.order_channel}' 指派购买任务给卖家 {suitable_seller_id} (Request ID: {request_id})...")
                if self.context.redis and self.order_channel:
                    pub_success = await self.context.redis.publish(self.order_channel, order_data)
                    if pub_success:
                        self.info(f"已成功指派购买任务给卖家 {suitable_seller_id} (Request ID: {request_id})")
                        admin_notify_text = f"交易任务 (ID: {request_id[:8]}...)：已指派卖家 {suitable_seller_id} 向 {data['recipient_username']} 出售 {receive_item_name} x{receive_qty}。"
                        if self.context.telegram_client: await self.context.telegram_client.send_system_notification(admin_notify_text)
                    else:
                        self.error(f"向频道 '{self.order_channel}' 指派购买任务失败！")
                        if self.context.telegram_client: await self.context.telegram_client.send_system_notification(f"⚠️ 交易任务 (ID: {request_id[:8]}...) 指派给卖家 {suitable_seller_id} 失败！(Redis 发布失败)")
                else:
                    self.error("无法指派购买任务：Redis 不可用或未配置指派频道。")
                    if self.context.telegram_client: await self.context.telegram_client.send_system_notification(f"⚠️ 交易任务 (ID: {request_id[:8]}...) 指派给卖家 {suitable_seller_id} 失败！(Redis 不可用)")
            else:
                self.warning(f"未能为交易请求 (ID: {request_id}) 找到拥有足够 '{receive_item_name}' x{receive_qty} 的卖家。")
                if self.context.telegram_client: await self.context.telegram_client.send_system_notification(f"⚠️ 交易任务 (ID: {request_id[:8]}...)：未能找到拥有足够 {receive_item_name} x{receive_qty} 的卖家。")
        except Exception as scan_e:
            self.error(f"扫描 Redis 库存键时出错: {scan_e}")
            if self.context.telegram_client: await self.context.telegram_client.send_system_notification(f"⚠️ 交易任务 (ID: {request_id[:8]}...)：扫描 Redis 查找卖家失败！")
            return


    # --- 卖家执行逻辑 (handle_assigned_order & _find_and_buy_listing) ---
    async def handle_assigned_order(self, channel: str, data: Any):
        """处理分配的购买订单 (所有实例监听，但只有匹配 ID 的会执行)"""
        # ... (代码与上一次提供的相同，保持不变) ...
        if not self._my_id: return
        if (not isinstance(data, dict) or "designated_seller_id" not in data or
            data["designated_seller_id"] != self._my_id or
            not all(k in data for k in ["request_id", "recipient_username", "receive_item_id", "receive_qty", "pay_item_id", "pay_qty"])):
            return
        self.info(f"收到指派给我的购买订单: {data}")
        request_id = data["request_id"]; lock_key = f"{REDIS_ORDER_EXEC_LOCK_PREFIX}{request_id}:{self._my_id}"
        redis_client = self.context.redis.get_client(); lock_acquired = False
        if redis_client:
            try:
                lock_acquired = await redis_client.set(lock_key, "1", ex=120, nx=True)
                if not lock_acquired: self.info(f"未能获取订单执行锁 '{lock_key}'，跳过。"); return
                self.info(f"成功获取订单执行锁 '{lock_key}'。")
            except Exception as e: self.error(f"检查或设置订单执行锁时出错: {e}，跳过。"); return
        else: self.error("无法检查订单执行锁：Redis 未连接。"); return

        recipient_username = data["recipient_username"]; receive_item_id = data["receive_item_id"]
        receive_qty = data["receive_qty"]; pay_item_id = data["pay_item_id"]; pay_qty = data["pay_qty"]
        receive_item_name = data.get("receive_item_name", await get_item_name_by_id(self.context, receive_item_id) or "未知")
        pay_item_name = data.get("pay_item_name", await get_item_name_by_id(self.context, pay_item_id) or "未知")

        current_qty = await get_inventory_item_quantity(self.context, self._my_id, receive_item_id)
        if current_qty < receive_qty:
            self.error(f"执行订单 (ID: {request_id}) 时发现库存不足 ({current_qty} < {receive_qty})！取消购买。")
            if self.context.redis and self.result_channel:
                 await self.context.redis.publish(self.result_channel, {"request_id": request_id, "seller_id": self._my_id, "seller_username": self._my_username, "status": "failed", "reason": "库存不足 (执行前检查)"})
            if lock_acquired and redis_client:
                try: await redis_client.delete(lock_key)
                except: pass
            return

        if request_id in self._active_buy_tasks and not self._active_buy_tasks[request_id].done():
             self.info(f"已有一个针对请求 {request_id} 的购买任务在运行，跳过。")
             if lock_acquired and redis_client:
                 try: await redis_client.delete(lock_key)
                 except: pass
             return

        task = asyncio.create_task(self._find_and_buy_listing(
            request_id, recipient_username, receive_item_id, receive_item_name, receive_qty,
            pay_item_id, pay_item_name, pay_qty, lock_key
        ))
        self._active_buy_tasks[request_id] = task
        task.add_done_callback(lambda t: self._active_buy_tasks.pop(request_id, None))


    async def _find_and_buy_listing(self, request_id: str, recipient_username: str,
                                   sell_item_id: str, sell_item_name: str, sell_qty: int,
                                   buy_item_id: str, buy_item_name: str, buy_qty: int,
                                   lock_key: str):
        """后台任务：查找匹配的挂单并执行购买 (卖家执行)"""
        # ... (代码与上一次提供的相同，保持不变) ...
        listing_id_to_buy: Optional[int] = None; found_listing_details = ""; status = "failed"; reason = "未知错误"
        try:
            for attempt in range(FIND_LISTING_MAX_ATTEMPTS):
                self.info(f"订单 {request_id}: 第 {attempt + 1}/{FIND_LISTING_MAX_ATTEMPTS} 次尝试查找 {recipient_username} 的挂单...")
                try:
                    market_data = await self.context.http.get_marketplace_listings(search_term=recipient_username)
                    if market_data and isinstance(market_data.get("listings"), list):
                        listings = market_data["listings"]; listings.sort(key=lambda x: x.get('listing_time', ''), reverse=True)
                        for listing in listings:
                            if (listing.get("seller_username") == recipient_username and listing.get("item_id") == buy_item_id and
                                listing.get("quantity") == buy_qty and not listing.get("is_bundle")):
                                price_json = listing.get("price_json")
                                if (isinstance(price_json, dict) and len(price_json) == 1 and
                                    sell_item_id in price_json and price_json[sell_item_id] == sell_qty):
                                    listing_id_to_buy = listing.get("id")
                                    found_listing_details = f"挂单ID {listing_id_to_buy} ({buy_item_name} x{buy_qty} 换 {sell_item_name} x{sell_qty})"
                                    self.info(f"订单 {request_id}: 成功找到匹配的挂单: {found_listing_details}"); break
                        if listing_id_to_buy: break
                except Exception as e: self.error(f"订单 {request_id}: 查找挂单时出错: {e}", exc_info=True)
                if not listing_id_to_buy:
                    self.info(f"订单 {request_id}: 未找到匹配挂单，将在 {FIND_LISTING_RETRY_DELAY} 秒后重试...")
                    await asyncio.sleep(FIND_LISTING_RETRY_DELAY)

            if listing_id_to_buy:
                buy_command = f".购买 {listing_id_to_buy}"
                self.info(f"订单 {request_id}: 准备将购买指令 '{buy_command}' 加入队列...")
                success = await self.context.telegram_client.send_game_command(buy_command)
                if success:
                    self.info(f"订单 {request_id}: 购买指令 '{buy_command}' 已成功加入队列。"); status = "success"; reason = f"购买指令已加入队列 (挂单 {listing_id_to_buy})"
                    await asyncio.sleep(1); self.info(f"订单 {request_id}: 购买指令已入队，触发角色数据同步...")
                    try: await self.context.event_bus.emit("trigger_character_sync_now")
                    except Exception as sync_e: self.error(f"订单 {request_id}: 尝试在发送购买指令后触发同步时出错: {sync_e}", exc_info=True)
                else:
                    self.error(f"订单 {request_id}: 将购买指令 '{buy_command}' 加入队列失败！"); status = "failed"; reason = f"将购买指令加入队列失败 (挂单 {listing_id_to_buy})"
            else:
                self.warning(f"订单 {request_id}: 在 {FIND_LISTING_MAX_ATTEMPTS} 次尝试后仍未找到 {recipient_username} 的挂单。"); status = "failed"; reason = "查找超时，未找到匹配挂单"
        except Exception as task_e:
             self.error(f"执行购买任务 (ID: {request_id}) 时发生意外错误: {task_e}", exc_info=True); status = "failed"; reason = f"执行购买任务时发生意外错误: {str(task_e)[:100]}"
        finally:
            if self.context.redis and self.result_channel:
                 result_data = {
                     "request_id": request_id, "seller_id": self._my_id, "seller_username": self._my_username,
                     "recipient_username": recipient_username, "status": status, "reason": reason,
                     "details": found_listing_details if listing_id_to_buy else "", "timestamp": datetime.utcnow().isoformat()
                 }
                 pub_res_success = await self.context.redis.publish(self.result_channel, result_data)
                 if pub_res_success: self.info(f"订单 {request_id}: 已发送最终结果到频道 '{self.result_channel}'")
                 else: self.error(f"订单 {request_id}: 发送结果到频道 '{self.result_channel}' 失败！")
            redis_client = self.context.redis.get_client()
            if redis_client:
                try:
                    deleted = await redis_client.delete(lock_key)
                    if deleted: self.info(f"已释放订单执行锁 '{lock_key}'。")
                except Exception as e: self.error(f"释放订单执行锁 '{lock_key}' 时出错: {e}")


    # --- (可选) 管理实例处理结果 (handle_transfer_result) ---
    async def handle_transfer_result(self, channel: str, data: Any):
        """处理从 Redis Pub/Sub 收到的交易结果 (仅管理实例执行)"""
        # ... (代码与上一次提供的相同，保持不变) ...
        if not self._is_admin_instance: return
        self.info(f"收到交易结果: {data}")
        if isinstance(data, dict):
            status = data.get("status", "unknown"); request_id = data.get("request_id", "未知")
            seller = data.get("seller_username", data.get("seller_id", "未知卖家")); recipient = data.get("recipient_username", "未知买家")
            reason = data.get("reason", ""); details = data.get("details", "")
            if status != "success":
                notify_text = f"⚠️ **交易失败** (ID: {request_id[:8]}...)\n卖家: {seller}\n买家: {recipient}\n原因: {reason}\n"
                if details: notify_text += f"详情: {details}\n"
                if self.context.telegram_client: await self.context.telegram_client.send_system_notification(notify_text)
            else: self.info(f"交易成功记录 (ID: {request_id[:8]}...): 卖家 {seller} -> 买家 {recipient}. 原因: {reason}. 详情: {details}")


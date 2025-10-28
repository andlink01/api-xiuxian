import logging
import asyncio
import json
import re
from typing import Optional, Dict, List, Tuple, Literal
from plugins.base_plugin import BasePlugin, AppContext
from pyrogram.types import Message
# 导入常量和辅助函数
from plugins.constants import GAME_CRAFTING_RECIPES_KEY
from modules.game_data_manager import GAME_ITEMS_MASTER_KEY
from plugins.utils import edit_or_reply, get_my_id

logger = logging.getLogger(__name__)

# --- Redis Key for temporary search results ---
REDIS_QA_SEARCH_RESULT_PREFIX = "qa_search_result"
SEARCH_RESULT_TTL = 300 # 搜索结果保留 5 分钟

# --- 定义题库类型和前缀 ---
VALID_QA_TYPES = Literal["玄骨", "天机"]
QA_PREFIX_MAP: Dict[VALID_QA_TYPES, str] = {
    "玄骨": "xuangu_qa",
    "天机": "tianji_qa", # 假设天机题库的前缀
}

# --- 已学配方辅助 ---
async def get_learned_recipe_names(context: AppContext, user_id: int) -> Tuple[Optional[List[str]], Optional[str]]:
    if not context.data_manager: return None, "DataManager 未初始化"
    recipes_data, _, last_updated_str = await context.data_manager.get_cached_data_with_details('recipes', user_id)
    learned_ids = recipes_data.get("known_ids") if isinstance(recipes_data, dict) else None
    if learned_ids is None: return None, "无法获取已学配方缓存"
    if not isinstance(learned_ids, list):
         logger.error(f"已学配方缓存数据格式错误 (非列表): {type(learned_ids)}")
         return None, "缓存数据格式错误"
    item_master = await context.data_manager.get_item_master_data(use_cache=True)
    if not item_master:
        logger.warning("无法获取物品主数据，配方名称可能不准确。")
        return learned_ids, last_updated_str
    learned_names = []
    for recipe_id in learned_ids:
        item_info = item_master.get(recipe_id)
        if item_info and isinstance(item_info, dict) and item_info.get("type") == "recipe":
            learned_names.append(item_info.get("name", recipe_id))
        else:
            product_id = recipe_id.replace("recipe_", "")
            product_info = item_master.get(product_id)
            if product_info and isinstance(product_info, dict):
                 suffix_map = {"elixir": "丹方", "treasure": "图纸", "formation": "阵图"}
                 suffix = suffix_map.get(product_info.get("type", ""), "配方")
                 recipe_guess_name = product_info.get("name", product_id) + suffix
                 if item_info and item_info.get("name"): recipe_guess_name = item_info["name"]
                 learned_names.append(recipe_guess_name)
            else: learned_names.append(recipe_id)
    return sorted(list(set(learned_names))), last_updated_str

# --- 配方查询辅助 ---
async def get_recipe_details(redis_client, item_name: str) -> Optional[Dict[str, int]]:
    if not redis_client or not item_name: return None
    try:
        recipe_json = await redis_client.hget(GAME_CRAFTING_RECIPES_KEY, item_name)
        if recipe_json:
            recipe_dict = json.loads(recipe_json)
            return recipe_dict if isinstance(recipe_dict, dict) else None
        else: return None
    except Exception as e: logger.error(f"获取配方 '{item_name}' 出错: {e}"); return None

# --- 题库管理辅助 ---
def format_question_key(qa_type: VALID_QA_TYPES, question: str) -> str:
    prefix = QA_PREFIX_MAP.get(qa_type, "unknown_qa")
    return f"{prefix}:{question.strip()}"

async def list_or_search_question_bank(redis_client, user_id: int, qa_type: VALID_QA_TYPES, keyword: Optional[str]) -> Tuple[List[Tuple[int, str, str]], Optional[str]]:
    results_display = []; results_internal: Dict[str, str] = {}
    if not redis_client: return results_display, "Redis 未连接"
    prefix = QA_PREFIX_MAP.get(qa_type)
    if not prefix: return results_display, f"无效的题库类型 '{qa_type}'"
    temp_result_key = f"{REDIS_QA_SEARCH_RESULT_PREFIX}:{user_id}:{qa_type}"
    search_count = 0; max_results = 50; cursor = '0'
    try:
        scan_pattern = f"{prefix}:*"
        if keyword: scan_pattern = f"{prefix}:*{keyword}*"
        logger.info(f"开始扫描 Redis Key，模式: '{scan_pattern}'")
        keys = []
        async for key in redis_client.scan_iter(match=scan_pattern, count=200):
            keys.append(key)
        if not keys:
             await redis_client.delete(temp_result_key)
             return [], None
        answers = await redis_client.mget(keys)
        for i, key in enumerate(keys):
            if search_count >= max_results:
                 logger.warning(f"查询题库 '{qa_type}' (关键词: {keyword or '无'}) 匹配结果过多，已截断至 {max_results} 条。")
                 break
            try:
                question = key.split(':', 1)[1]
                answer = answers[i]
                if answer:
                    search_count += 1
                    results_display.append((search_count, question, answer))
                    results_internal[str(search_count)] = key
            except IndexError: logger.warning(f"解析题库 Key 格式错误: {key}")
            except Exception as get_err: logger.error(f"处理 key {key} 时出错: {get_err}")

        if results_internal:
            await redis_client.delete(temp_result_key)
            await redis_client.hset(temp_result_key, mapping=results_internal)
            await redis_client.expire(temp_result_key, SEARCH_RESULT_TTL)
            logger.info(f"为用户 {user_id} 存储了 {len(results_internal)} 条 {qa_type} 题库搜索/列出结果到 {temp_result_key}")
        else:
            await redis_client.delete(temp_result_key)
    except Exception as e:
        logger.error(f"扫描 Redis {qa_type} 题库时出错: {e}", exc_info=True)
        try: await redis_client.delete(temp_result_key)
        except: pass
        return [], f"扫描 Redis 题库时出错: {e}"
    return results_display, None

async def add_update_question(redis_client, qa_type: VALID_QA_TYPES, question: str, answer: str) -> bool:
    if not redis_client or not question or not answer: return False
    key = format_question_key(qa_type, question)
    try:
        await redis_client.set(key, answer.strip(), ex=90*24*60*60)
        logger.info(f"成功添加/更新 {qa_type} 题库: 问题='{question[:50]}...'")
        return True
    except Exception as e:
        logger.error(f"添加/更新 {qa_type} 题库失败 (Key: {key}): {e}", exc_info=True)
        return False

async def delete_question_by_id(redis_client, user_id: int, qa_type: VALID_QA_TYPES, result_id: str) -> Tuple[int, Optional[str]]:
    if not redis_client or not result_id.isdigit(): return 0, "无效的编号"
    temp_result_key = f"{REDIS_QA_SEARCH_RESULT_PREFIX}:{user_id}:{qa_type}"
    deleted_count = 0; error_msg = None; question_deleted = None
    try:
        redis_key_to_delete = await redis_client.hget(temp_result_key, result_id)
        if redis_key_to_delete:
            try: question_deleted = redis_key_to_delete.split(':', 1)[1]
            except IndexError: question_deleted = f"Key:{redis_key_to_delete}"
            deleted_count = await redis_client.delete(redis_key_to_delete)
            if deleted_count > 0:
                logger.info(f"通过编号 {result_id} 成功删除 {qa_type} 题库问题: '{question_deleted[:50]}...'")
                await redis_client.hdel(temp_result_key, result_id)
            else:
                logger.warning(f"尝试删除 Key '{redis_key_to_delete}' 时未找到。")
                error_msg = f"问题 '{question_deleted[:50]}...' 已被删除或 Key 已过期"
                await redis_client.hdel(temp_result_key, result_id)
        else:
            logger.warning(f"未找到编号 {result_id} 对应的 {qa_type} 临时搜索结果 ({temp_result_key})。")
            error_msg = f"未找到 {qa_type} 搜索结果编号 [{result_id}]，请重新搜索。"
    except Exception as e:
        logger.error(f"通过编号 {result_id} 删除 {qa_type} 题库问题时出错: {e}", exc_info=True)
        error_msg = f"删除时出错: {e}"
    return deleted_count, question_deleted if deleted_count > 0 else error_msg

# --- 插件类 ---
class Plugin(BasePlugin):
    """处理知识库相关指令：配方查询、题库管理"""
    def __init__(self, context: AppContext, plugin_name: str, cn_name: str | None = None):
        super().__init__(context, plugin_name, cn_name or "知识管理")
        self.info("插件已加载。")

    def register(self):
        self.event_bus.on("query_learned_recipes_command", self.handle_query_learned_recipes)
        self.event_bus.on("query_recipe_detail_command", self.handle_query_recipe_detail)
        self.event_bus.on("query_qa_command", self.handle_query_qa)
        self.event_bus.on("add_update_qa_command", self.handle_add_update_qa)
        self.event_bus.on("delete_qa_command", self.handle_delete_qa)
        self.info("已注册知识库相关指令事件监听器。")

    async def handle_query_learned_recipes(self, message: Message, edit_target_id: int | None):
        self.info("处理 ,已学配方 指令...")
        my_id = await get_my_id(self, message, edit_target_id)
        if not my_id: return
        # --- 修复: 移除初始 "处理中" 消息 ---
        # await edit_or_reply(self, message.chat.id, edit_target_id, "⏳ 正在查询已学配方列表...", original_message=message)
        # --- 修复结束 ---
        recipe_names, last_updated = await get_learned_recipe_names(self.context, my_id)
        if recipe_names is None: reply = f"❌ 获取已学配方失败: {last_updated or '未知错误'}"
        elif not recipe_names:
            reply = "ℹ️ 您尚未学习任何配方。"
            if last_updated: reply += f"\n(数据更新于: {last_updated})"
        else:
            reply = "📜 **您已学习的配方:**\n\n"; items_per_line = 3; lines = []
            for i in range(0, len(recipe_names), items_per_line): lines.append("`" + "` `".join(recipe_names[i:i+items_per_line]) + "`")
            reply += "\n".join(lines)
            if last_updated: reply += f"\n\n(数据更新于: {last_updated})"
        await edit_or_reply(self, message.chat.id, edit_target_id, reply, original_message=message)

    async def handle_query_recipe_detail(self, message: Message, item_name: str, edit_target_id: int | None):
        self.info(f"处理 ,查询配方 指令，物品: '{item_name}'")
        redis_client = self.redis.get_client()
        if not redis_client: await edit_or_reply(self, message.chat.id, edit_target_id, "❌ 无法连接 Redis。", original_message=message); return
        # --- 修复: 移除初始 "处理中" 消息 ---
        # await edit_or_reply(self, message.chat.id, edit_target_id, f"⏳ 正在查询 '{item_name}' 的配方...", original_message=message)
        # --- 修复结束 ---
        recipe = await get_recipe_details(redis_client, item_name)
        if recipe:
            reply = f"📜 **物品 '{item_name}' 的配方:**\n\n"; material_lines = [f"  • `{mat_name}` x{qty:,}" for mat_name, qty in recipe.items()]
            reply += "\n".join(material_lines) + "\n\n(数据来源: Redis 缓存)"
        else: reply = f"❌ 未找到 '{item_name}' 的配方。\n请确保名称完全匹配且已添加。"
        await edit_or_reply(self, message.chat.id, edit_target_id, reply, original_message=message)

    async def handle_query_qa(self, message: Message, qa_type: VALID_QA_TYPES, keyword: Optional[str], edit_target_id: int | None):
        log_keyword = f"关键词: '{keyword}'" if keyword else "列出全部"
        self.info(f"处理 ,查询题库 指令，类型: {qa_type}, {log_keyword}")
        my_id = await get_my_id(self, message, edit_target_id)
        if not my_id: return
        redis_client = self.redis.get_client()
        if not redis_client: await edit_or_reply(self, message.chat.id, edit_target_id, "❌ 无法连接 Redis。", original_message=message); return

        # --- 修复: 移除初始 "处理中" 消息 (admin_plugin 会发) ---
        # action_text = f"搜索 '{keyword}'" if keyword else "列出全部"
        # await edit_or_reply(self, message.chat.id, edit_target_id, f"⏳ 正在 {qa_type} 题库中{action_text}...", original_message=message)
        # --- 修复结束 ---
        results, error_msg = await list_or_search_question_bank(redis_client, my_id, qa_type, keyword)

        if error_msg:
            reply = f"❌ 查询题库时出错: {error_msg}"
        elif not results:
            if keyword: reply = f"ℹ️ 未在 {qa_type} 题库中找到包含 '{keyword}' 的问题。"
            else: reply = f"ℹ️ {qa_type} 题库为空或未找到任何条目。"
        else:
            list_or_search = "搜索结果" if keyword else "列表"
            reply = f"📚 **{qa_type} 题库 {list_or_search}:**\n"
            max_chars = 4000; max_results_display = 50

            for num, q, a in results[:max_results_display]:
                 line = f"\n**[{num}] 问:** {q}\n    **答:** {a}\n"
                 if len(reply) + len(line) > max_chars:
                     await edit_or_reply(self, message.chat.id, edit_target_id, reply, original_message=message)
                     reply = f"📚 **{qa_type} 题库 {list_or_search} (续):**\n" + line
                     edit_target_id = None
                 else:
                     reply += line
            
            if len(results) > max_results_display:
                 hint_too_many = f"\n_(结果过多，仅显示前 {max_results_display} 条)_"
                 if len(reply) + len(hint_too_many) <= max_chars: reply += hint_too_many
            
            final_hint = f"\n\n💡 可使用 `,删除题库 <编号>` 删除条目 (本次结果 {SEARCH_RESULT_TTL//60} 分钟内有效)。"
            if len(reply) + len(final_hint) <= max_chars:
                reply += final_hint
            else:
                await edit_or_reply(self, message.chat.id, edit_target_id, reply, original_message=message)
                reply = final_hint
                edit_target_id = None

        await edit_or_reply(self, message.chat.id, edit_target_id, reply, original_message=message)

    async def handle_add_update_qa(self, message: Message, qa_type: VALID_QA_TYPES, qa_pair: str, edit_target_id: int | None):
        self.info(f"处理 ,添加题库 指令，类型: {qa_type}")
        parts = qa_pair.split("::", 1)
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            reply = "❌ 格式错误。\n请使用: `,添加题库 [玄骨|天机] 问题::答案`"
            await edit_or_reply(self, message.chat.id, edit_target_id, reply, original_message=message); return
        question = parts[0].strip(); answer = parts[1].strip()
        redis_client = self.redis.get_client()
        if not redis_client: await edit_or_reply(self, message.chat.id, edit_target_id, "❌ 无法连接 Redis。", original_message=message); return
        
        # --- 修复: 移除初始 "处理中" 消息 (admin_plugin 会发) ---
        # await edit_or_reply(self, message.chat.id, edit_target_id, f"⏳ 正在添加/更新 {qa_type} 问题...", original_message=message)
        # --- 修复结束 ---
        success = await add_update_question(redis_client, qa_type, question, answer)
        reply = f"✅ 成功添加/更新 {qa_type} 题库：\n**问:** {question}\n**答:** {answer}" if success else f"❌ 添加/更新 {qa_type} 题库失败。"
        await edit_or_reply(self, message.chat.id, edit_target_id, reply, original_message=message)

    async def handle_delete_qa(self, message: Message, result_id_str: str, edit_target_id: int | None):
        self.info(f"处理 ,删除题库 指令，编号: {result_id_str}")
        my_id = await get_my_id(self, message, edit_target_id)
        if not my_id: return
        if not result_id_str.isdigit():
            reply = "❌ 请提供要删除的问题编号 (数字)。\n请先使用 `,查询题库` 获取编号。\n用法: `,删除题库 <编号>`"
            await edit_or_reply(self, message.chat.id, edit_target_id, reply, original_message=message); return
        redis_client = self.redis.get_client()
        if not redis_client: await edit_or_reply(self, message.chat.id, edit_target_id, "❌ 无法连接 Redis。", original_message=message); return
        
        # --- 修复: 移除初始 "处理中" 消息 (admin_plugin 会发) ---
        # await edit_or_reply(self, message.chat.id, edit_target_id, f"⏳ 正在尝试删除编号为 [{result_id_str}] 的问题...", original_message=message)
        # --- 修复结束 ---
        
        deleted_count = 0; result_info = f"未找到编号 [{result_id_str}] 对应的临时搜索结果，请重新搜索。"; qa_type_deleted: Optional[VALID_QA_TYPES] = None
        for qa_type in QA_PREFIX_MAP.keys():
            count, info = await delete_question_by_id(redis_client, my_id, qa_type, result_id_str)
            if count > 0: deleted_count = count; result_info = info; qa_type_deleted = qa_type; break
            elif "未找到" not in str(info): result_info = info; break
        reply = f"✅ 成功从 {qa_type_deleted} 题库中删除了编号为 [{result_id_str}] 的问题:\n`{result_info}`" if deleted_count > 0 else f"❌ 删除失败: {result_info}"
        await edit_or_reply(self, message.chat.id, edit_target_id, reply, original_message=message)


import logging
import json
import re
from typing import Optional, Dict, Tuple, Any
from plugins.base_plugin import BasePlugin, AppContext
from pyrogram.types import Message
# 导入 Redis Key 常量
from plugins.constants import GAME_CRAFTING_RECIPES_KEY
# 导入辅助函数
from plugins.utils import edit_or_reply

# --- 配方解析逻辑 (与之前的独立脚本类似) ---
def parse_recipe_line(line: str) -> Optional[Tuple[str, Dict[str, int]]]:
    """解析单行配方文本，返回产物名称和材料字典"""
    line = line.strip()
    if not line: return None
    product_match = re.match(r"【(.+?)】", line)
    if not product_match: return None # 忽略无法解析产物名称的行
    product_name = product_match.group(1).strip()
    materials_part_match = re.search(r"需：(.+?)(。)?$", line)
    if not materials_part_match: return None # 忽略无法解析材料部分的行
    materials_str = materials_part_match.group(1).strip()
    materials_dict: Dict[str, int] = {}
    material_pattern = re.compile(r"(.+?)\s*x\s*(\d+)\s*[,，]?")
    last_end = 0
    for match in material_pattern.finditer(materials_str):
        material_name = match.group(1).strip()
        try:
            quantity = int(match.group(2))
            materials_dict[material_name] = quantity
            last_end = match.end()
        except ValueError:
            logging.getLogger("RecipeManagerPlugin.Parse").warning(f"解析数量失败 for '{material_name}' in: {line}")
    remaining_str = materials_str[last_end:].strip()
    if remaining_str:
         last_match = material_pattern.match(remaining_str)
         if last_match:
             material_name = last_match.group(1).strip()
             try:
                 quantity = int(last_match.group(2))
                 materials_dict[material_name] = quantity
             except ValueError:
                 logging.getLogger("RecipeManagerPlugin.Parse").warning(f"解析最后一个材料数量失败 for '{material_name}' in: {line}")
         elif 'x' not in remaining_str: # 处理特殊情况如 "修为 50"
             parts = remaining_str.split()
             if len(parts) == 2 and parts[1].isdigit():
                  materials_dict[parts[0].strip()] = int(parts[1])
             else: logging.getLogger("RecipeManagerPlugin.Parse").warning(f"无法解析剩余材料部分: '{remaining_str}' in: {line}")
         else: logging.getLogger("RecipeManagerPlugin.Parse").warning(f"无法解析剩余材料部分: '{remaining_str}' in: {line}")
    return product_name, materials_dict if materials_dict else None
# --- 解析逻辑结束 ---

class Plugin(BasePlugin):
    """
    管理游戏炼制配方的插件，通过管理员指令更新 Redis 数据。
    """
    def __init__(self, context: AppContext, plugin_name: str, cn_name: str | None = None):
        super().__init__(context, plugin_name, cn_name)
        self.info("插件已加载。")

    def register(self):
        """注册更新配方的事件监听器"""
        self.event_bus.on("update_recipes_command", self.handle_update_recipes_command)
        self.info("已注册 update_recipes_command 事件监听器。")

    async def handle_update_recipes_command(self, message: Message, recipe_text: str, overwrite: bool, edit_target_id: int | None):
        """处理来自管理员插件的更新配方请求"""
        self.info(f"收到更新配方指令 (overwrite={overwrite})，开始处理...")
        if not recipe_text:
            await edit_or_reply(self, message.chat.id, edit_target_id, "❌ 错误：未提供配方文本。", original_message=message)
            return

        redis_client = self.redis.get_client()
        if not redis_client:
            self.error("无法连接到 Redis。")
            await edit_or_reply(self, message.chat.id, edit_target_id, "❌ 错误：无法连接到 Redis。", original_message=message)
            return

        parsed_recipes: Dict[str, str] = {} # 产物名 -> 材料JSON
        valid_lines = 0
        skipped_lines = 0
        error_lines = []

        self.info("开始解析配方文本...")
        for i, line in enumerate(recipe_text.strip().split('\n')):
            result = parse_recipe_line(line)
            if result:
                product_name, materials_dict = result
                if materials_dict: # 确保解析出了材料
                    try:
                        materials_json = json.dumps(materials_dict, ensure_ascii=False, sort_keys=True)
                        parsed_recipes[product_name] = materials_json
                        valid_lines += 1
                    except TypeError as e:
                        self.error(f"序列化材料字典失败 for '{product_name}': {e}")
                        skipped_lines += 1
                        error_lines.append(f"第 {i+1} 行序列化失败: {line[:50]}...")
                else:
                    skipped_lines += 1 # 解析成功但没有材料
                    error_lines.append(f"第 {i+1} 行未解析出材料: {line[:50]}...")
            elif line.strip(): # 如果行不为空但解析失败
                skipped_lines += 1
                error_lines.append(f"第 {i+1} 行解析失败: {line[:50]}...")

        self.info(f"解析完成：成功 {valid_lines} 条，跳过/失败 {skipped_lines} 行。")

        if not parsed_recipes:
            reply = f"❌ 未能从提供的文本中解析出任何有效的配方数据。"
            if error_lines:
                reply += "\n\n**解析失败/跳过行 (部分):**\n" + "\n".join(error_lines[:5]) # 最多显示 5 条错误
            await edit_or_reply(self, message.chat.id, edit_target_id, reply, original_message=message)
            return

        try:
            key_exists = await redis_client.exists(GAME_CRAFTING_RECIPES_KEY)
            if key_exists and not overwrite:
                self.warning(f"Redis Key '{GAME_CRAFTING_RECIPES_KEY}' 已存在且未指定覆盖，操作取消。")
                await edit_or_reply(self, message.chat.id, edit_target_id, f"ℹ️ 配方数据已存在于 Redis。\n如需覆盖，请在指令末尾添加 `--overwrite`。", original_message=message)
                return

            action = "覆盖写入" if key_exists and overwrite else "写入"
            self.info(f"准备将 {len(parsed_recipes)} 条配方数据 {action} Redis Key '{GAME_CRAFTING_RECIPES_KEY}'...")

            # 使用 HSET 批量写入 (会覆盖同名字段)
            # 如果是首次写入或确认覆盖，可以先 DEL 再 HSET，确保是全新的数据
            if overwrite or not key_exists:
                 await redis_client.delete(GAME_CRAFTING_RECIPES_KEY) # 先删除旧 Key (如果需要完全覆盖)

            result = await redis_client.hset(GAME_CRAFTING_RECIPES_KEY, mapping=parsed_recipes)

            # HSET 返回成功添加的新字段数量
            # if isinstance(result, int):
            reply = f"✅ 成功将 {valid_lines} 条配方数据 {action} Redis。\n(跳过/失败 {skipped_lines} 行)"
            self.info(f"成功 {action} {valid_lines} 条配方数据到 Redis。")
            if error_lines:
                reply += "\n\n**解析失败/跳过行 (部分):**\n" + "\n".join(error_lines[:5])
            await edit_or_reply(self, message.chat.id, edit_target_id, reply, original_message=message)

        except Exception as e:
            self.error(f"写入 Redis 时发生错误: {e}", exc_info=True)
            await edit_or_reply(self, message.chat.id, edit_target_id, f"❌ 写入 Redis 时发生错误: {e}", original_message=message)


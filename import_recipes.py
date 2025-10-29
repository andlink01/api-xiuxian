import asyncio
import json
import re
import yaml
import redis.asyncio as aioredis
from typing import Optional, Tuple, Dict

# --- 从 recipe_manager_plugin.py 复制过来的解析函数 ---
def parse_recipe_line(line: str) -> Optional[Tuple[str, Dict[str, int]]]:
    """解析单行配方文本，返回产物名称和材料字典"""
    line = line.strip()
    if not line: return None
    # 修正：确保匹配以【开头，以】结尾，中间是非】的字符
    product_match = re.match(r"【([^】]+?)】", line)
    if not product_match: return None # 忽略无法解析产物名称的行
    product_name = product_match.group(1).strip()
    # 修正：确保“需：”后面可以跟任意字符直到行尾或句号
    materials_part_match = re.search(r"需：(.+?)(。)?$", line)
    if not materials_part_match: return None # 忽略无法解析材料部分的行
    materials_str = materials_part_match.group(1).strip()
    materials_dict: Dict[str, int] = {}
    # 修正：匹配更灵活的格式，允许逗号、中文逗号或空格分隔，并正确处理最后一个材料
    material_pattern = re.compile(r"【?([^】x]+?)】?\s*x\s*(\d+)\s*[,，\s]*")
    last_end = 0
    for match in material_pattern.finditer(materials_str):
        # 修正：移除材料名称末尾可能存在的逗号或空格
        material_name = match.group(1).strip().rstrip(',，')
        try:
            quantity = int(match.group(2))
            materials_dict[material_name] = quantity
            last_end = match.end()
        except ValueError:
            print(f"警告: 解析数量失败 for '{material_name}' in: {line}") # 使用 print 替代 logger

    # 尝试处理最后一个没有逗号分隔的材料
    remaining_str = materials_str[last_end:].strip()
    if remaining_str:
        # 再次尝试匹配模式 (可能带括号，也可能不带)
        final_match = re.match(r"【?([^】x]+?)】?\s*x\s*(\d+)$", remaining_str)
        if final_match:
            material_name = final_match.group(1).strip()
            try:
                quantity = int(final_match.group(2))
                materials_dict[material_name] = quantity
            except ValueError:
                 print(f"警告: 解析最后一个材料数量失败 for '{material_name}' in: {line}")
        # 处理特殊情况如 "修为 x50"
        elif 'x' in remaining_str:
             parts = remaining_str.split('x')
             if len(parts) == 2 and parts[1].strip().isdigit():
                  materials_dict[parts[0].strip()] = int(parts[1].strip())
             else:
                 print(f"警告: 无法解析剩余材料部分: '{remaining_str}' in: {line}")
        else:
             print(f"警告: 无法解析剩余材料部分（非标准格式）: '{remaining_str}' in: {line}")


    return product_name, materials_dict if materials_dict else None
# --- 解析函数结束 ---


# --- Redis Key (来自 constants.py) ---
GAME_CRAFTING_RECIPES_KEY = "game:crafting_recipes" # 使用 Hash 结构存储
# --- Redis Key 结束 ---

# --- 配方文本 (包含新增的配方) ---
recipe_text = """
【增元丹】需：凝血草x4, 灵石x10。
【凝气散】需：一阶妖丹x1, 凝血草x4, 灵石x10。
【清灵丹】需：清灵草x3, 凝血草x5。
【合气丹】需：一阶妖丹x5, 凝血草x10, 三级妖丹x5。
【黄芽丹】需：百年铁木x3, 一阶妖丹x30。
【天火液】需：养魂木x2, 二级妖丹x5, 金精矿x10, 三级妖丹x1。
【凝魂丹】需：养魂木x1, 阴魂丝x10, 清灵草x20, 三级妖丹x1。
【三转重元丹】需：天雷竹x4, 百年铁木x10, 一阶妖丹x30, 三级妖丹x1。
【九曲灵参丹】需：养魂木x15, 二级妖丹x30, 三级妖丹x5。
【风行丹】需：一截灵眼之树x1, 养魂木x3, 天雷竹x5。
【玄铁剑】需：灵石x10。
【金蚨子母刃】需：金精矿x2, 一阶妖丹x3, 灵石x50。
【乌龙幡】需：阴魂丝x5, 一阶妖丹x2, 灵石x40。
【青竹蜂云剑】需：天雷竹x12, 金精矿x10, 二级妖丹x5, 灵石x80。
【金光砖】需：金精矿x12, 二级妖丹x3, 灵石x60。
【风雷翅】需：天雷竹x10, 三级妖丹x50, 二级妖丹x10, 金精矿x8, 灵石x3000，养魂木x20，阴凝之晶x10，法则碎片·风x5，法则碎片·雷x5。
【皇鳞甲】需：一截灵眼之树x5, 二级妖丹x10, 金精矿x8, 灵石x100, 三级妖丹x40。
【青鸾天盾】需：养魂木x20, 三级妖丹x20, 元磁山核·甲x1, 元磁山核·乙x1, 元磁山核·丙x1, 元磁山核·丁x1。
【神行符】需：凝血草x5, 灵石x20。
【金刚符】需：一阶妖丹x1, 灵石x20。
【三才微尘阵】需：灵石x10, 修为x50。
【四象御法阵】需：灵石x20, 修为x100。
【五行颠倒阵】需：灵石x100, 修为x500。
【九转凝魂丹】需：【空间之核】x1, 【法则碎片·木】x5, 【法则碎片·水】x5, 【养魂木】x20。
【佑天神盾】需：【九天神雷木】x1, 【法则碎片·风】x3, 【法则碎片·雷】x3, 【天雷竹】x50。
【太虚丹】需：【太虚仙露】x1, 【法则碎片·火】x5, 【法则碎片·土】x5, 【九曲灵参丹】x5。
"""
# --- 配方文本结束 ---

async def main():
    # --- 读取 Redis 配置 ---
    config_path = "config.yaml"
    redis_config = {}
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config_data = yaml.safe_load(f)
            if isinstance(config_data, dict) and 'redis' in config_data:
                redis_config = config_data['redis']
            else:
                print(f"错误: 配置文件 {config_path} 格式不正确或缺少 'redis' 部分。")
                return
    except FileNotFoundError:
        print(f"错误: 配置文件 {config_path} 未找到。")
        return
    except Exception as e:
        print(f"读取配置文件 {config_path} 时出错: {e}")
        return

    redis_host = redis_config.get('host', 'localhost')
    redis_port = redis_config.get('port', 6379)
    redis_db = redis_config.get('db', 0)
    redis_password = redis_config.get('password')
    # --- 配置读取结束 ---

    # --- 连接 Redis ---
    redis_client = None
    try:
        print(f"正在连接到 Redis ({redis_host}:{redis_port}, DB: {redis_db})...")
        redis_client = aioredis.Redis(
            host=redis_host,
            port=redis_port,
            db=redis_db,
            password=redis_password,
            decode_responses=True # 重要：直接解码为字符串
        )
        await redis_client.ping()
        print("Redis 连接成功！")
    except Exception as e:
        print(f"连接 Redis 失败: {e}")
        if redis_client:
            await redis_client.aclose() # 使用 aclose
        return
    # --- 连接结束 ---

    # --- 解析并准备数据 ---
    parsed_recipes_for_redis: Dict[str, str] = {}
    valid_lines = 0
    skipped_lines = 0
    error_lines = []

    print("开始解析配方文本...")
    for i, line in enumerate(recipe_text.strip().split('\n')):
        result = parse_recipe_line(line)
        if result:
            product_name, materials_dict = result
            if materials_dict:
                try:
                    # 检查材料名是否包含不需要的括号
                    cleaned_materials = {k.strip('【】'): v for k, v in materials_dict.items()}
                    materials_json = json.dumps(cleaned_materials, ensure_ascii=False, sort_keys=True)
                    parsed_recipes_for_redis[product_name] = materials_json
                    valid_lines += 1
                except TypeError as e:
                    print(f"错误: 序列化材料字典失败 for '{product_name}': {e}")
                    skipped_lines += 1
                    error_lines.append(f"第 {i+1} 行序列化失败: {line[:50]}...")
            else:
                skipped_lines += 1
                error_lines.append(f"第 {i+1} 行未解析出材料: {line[:50]}...")
        elif line.strip(): # 如果行不为空但解析失败
            skipped_lines += 1
            error_lines.append(f"第 {i+1} 行解析失败: {line[:50]}...")

    print(f"解析完成：成功 {valid_lines} 条，跳过/失败 {skipped_lines} 行。")
    if error_lines:
        print("\n解析失败/跳过行 (部分):")
        for err_line in error_lines[:5]:
            print(err_line)
    # --- 解析结束 ---

    # --- 写入 Redis ---
    if parsed_recipes_for_redis:
        try:
            print(f"\n准备将 {len(parsed_recipes_for_redis)} 条配方数据写入 Redis Hash Key '{GAME_CRAFTING_RECIPES_KEY}' (将覆盖同名配方)...")
            # 使用 HSET 批量写入，它会自动处理覆盖
            result = await redis_client.hset(GAME_CRAFTING_RECIPES_KEY, mapping=parsed_recipes_for_redis)
            print(f"Redis HSET 操作完成。")
            print(f"总计 {valid_lines} 条有效配方已尝试写入/覆盖。")

        except Exception as e:
            print(f"写入 Redis 时发生错误: {e}")
    else:
        print("\n没有解析到有效的配方数据，未执行 Redis 写入操作。")
    # --- 写入结束 ---

    # --- 关闭连接 ---
    if redis_client:
        await redis_client.aclose() # 使用 aclose
        print("Redis 连接已关闭。")
    # --- 关闭结束 ---

if __name__ == "__main__":
    asyncio.run(main())


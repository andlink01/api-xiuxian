# Shared constants for plugins to avoid circular imports

# Redis Key Prefixes & Keys (保持之前的 Key 不变)
REDIS_CHAR_KEY_PREFIX = "character_info"
REDIS_INV_KEY_PREFIX = "inventory"
REDIS_ITEM_MASTER_KEY = "game:items:master" # From item_sync_plugin
REDIS_SHOP_KEY_PREFIX = "shop_items" # From shop_sync_plugin

# --- 新增: 炼制配方 Key ---
GAME_CRAFTING_RECIPES_KEY = "game:crafting_recipes" # 使用 Hash 结构存储
# --- 新增结束 ---

# Status Translations (保持不变)
STATUS_TRANSLATION = {
    "normal": "正常", "cultivating": "闭关中", "deep_seclusion": "深度闭关", "fleeing": "逃遁中",
}

# Item Type Translations (保持不变)
SHOP_ITEM_TYPE_TRANSLATION = {
    "seed": "🌱种子", "material": "🌿材料", "elixir": "💊丹药",
    "talisman": "✨符咒", "recipe": "📜配方", "formation": "☸️阵法",
    "treasure": "🗡法宝", "badge": "🏅徽章", "quest_item": "💎任务",
    "special_item": "🎁特殊", "loot_box": "💰宝箱", "special_tool":"🛠️工具",
}


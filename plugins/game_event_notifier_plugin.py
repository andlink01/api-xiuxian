import logging
import re
from typing import Optional, Dict, Any, List, Tuple
from plugins.base_plugin import BasePlugin, AppContext
from pyrogram.types import Message
import asyncio # Import asyncio for call_later

# --- 正则表达式 ---
REGEX_CULTIVATION_RESULT = re.compile(
    r"【(闭关成功|闭关失败|走火入魔)】[\s\S]*?(?:你的修为最终(?:增加|减少|倒退)了 (\d+) 点|你的修为(?:减少|倒退)了 (\d+) 点)",
    re.MULTILINE
)
REGEX_QIYU_ITEM_GAIN = re.compile(r"【奇遇】[\s\S]*?获得【(.+?)】x ?(\d+)")
REGEX_QIYU_CULTIVATION_GAIN = re.compile(r"【奇遇】[\s\S]*?修为额外增加了 (\d+) 点")
REGEX_HARVEST_SUCCESS = re.compile(r"一键采药完成！[\s\S]*?收获了：【(.+?)】x ?(\d+)")
REGEX_MAINTENANCE_SUCCESS = re.compile(r"一键(浇水|除草|除虫)完成！[\s\S]*?成功打理了 (\d+) 块灵田")
REGEX_MAINTENANCE_NO_NEED = re.compile(r"没有需要【(采药|浇水|除草|除虫)】的地块")
REGEX_SOW_SUCCESS = re.compile(r"播种成功！[\s\S]*?种下了【(.+?)】")
REGEX_SOW_FAIL_SEED = re.compile(r"你的【(.+?)】数量不足")
REGEX_BUY_SUCCESS = re.compile(r"兑换成功！[\s\S]*?获得了【(.+?)】x ?(\d+)")
REGEX_BUY_FAIL_CONTRIBUTION = re.compile(r"宗门贡献不足")
REGEX_CHECKIN_ALREADY = re.compile(r"今日已点卯")
REGEX_CHECKIN_SUCCESS = re.compile(r"点卯成功！.*?获得了 (\d+) 点宗门贡献")
REGEX_CHECKIN_BONUS = re.compile(r"额外奖励 (\d+) 点贡献")
REGEX_CHECKIN_SALARY = re.compile(r"领取了今日的俸禄 (\d+) 块【灵石】")
REGEX_CHECKIN_NO_SECT = re.compile(r"散修无需点卯")
REGEX_TEACH_NEED_REPLY = re.compile(r"此神通需回复你的一条有价值的发言")
REGEX_TEACH_NO_SECT = re.compile(r"尚未拜入宗门，无法为宗门传功")
REGEX_TEACH_SUCCESS = re.compile(r"传功玉简已记录！.*?获得了 (\d+) 点贡献.*?今日已传功 (\d+)/3 次")
REGEX_TEACH_LIMIT = re.compile(r"今日传功过于频繁|每日最多传功 3 次")
REGEX_PAGODA_ALREADY = re.compile(r"你今日已挑战失败")
REGEX_PAGODA_START = re.compile(r"【琉璃问心塔】.*?踏入了古塔的第 (\d+) 层")
REGEX_PAGODA_REPORT_FLOORS = re.compile(r"本次共闯过 (\d+) 层")
REGEX_PAGODA_REPORT_CULTIVATION = re.compile(r"-\s*修为\s*增加了 (\d+) 点")
REGEX_PAGODA_REPORT_ITEM = re.compile(r"-\s*获得了【(.+?)】x ?(\d+)")
REGEX_LEARN_RECIPE_SUCCESS = re.compile(r"消耗了【(.+?)】，成功领悟了它的炼制之法")
REGEX_LEARN_RECIPE_FAIL_NO_ITEM = re.compile(r"储物袋中没有此物可供学习")
REGEX_CRAFT_FAIL_MATERIAL = re.compile(r"炼制【(.+?)】x(\d+) 失败：材料不足！\n缺少：(.+)", re.DOTALL)
REGEX_CRAFT_FAIL_NONEXIST = re.compile(r"修仙界中并无此物可供炼制")
REGEX_CRAFT_START = re.compile(r"准备同时开炼 (\d+) 炉【(.+?)】")
REGEX_CRAFT_SUCCESS_MULTI = re.compile(r"炼制结束！[\s\S]*?成功 (\d+) 次。[\s\S]*?最终获得【(.+?)】x(\d+)", re.MULTILINE)
REGEX_TRADE_BUY_SUCCESS = re.compile(r"交易成功！\n你成功购得 【(.+?)】x ?(\d+)")
REGEX_TRADE_SOLD_NOTIFY = re.compile(r"【万宝楼快报】\n@(.+?) 道友，你上架的 【(.+?)】 已被售出 (\d+) 件！\n你获得了：【(.+?)】x ?(\d+)")
REGEX_YINDAO_SUCCESS = re.compile(r"你引动【水之道】，获得了 (\d+)点神识！")
REGEX_YINDAO_BUFF = re.compile(r"并领悟了临时增益【(.+?)】")
REGEX_YINDAO_COOLDOWN = re.compile(r"大道感悟需循序渐进，请在 (.+?) 后再次引道")
REGEX_DUEL_START = re.compile(r"⚔️ 遭遇战！ ⚔️\n@(\S+) 突然向 @(\S+) 发难！")
REGEX_DUEL_RESULT = re.compile(r"已有分晓！恭喜 @(\S+) 技高一筹！[\s\S]*?战果: @\S+ 成功夺取了 (\d+)点修为！[\s\S]*?@(\S+) 元气大伤.*?损失 (\d+) 点修为")
REGEX_NASCENT_SOUL_START = re.compile(r"感应到你的元婴已神游归来，正在清点收获")
REGEX_NASCENT_SOUL_REWARDS = re.compile(r"【元神归窍】[\s\S]*?为你带回了：([\s\S]+?)元婴成长:", re.MULTILINE)
REGEX_NASCENT_SOUL_EXP = re.compile(r"元婴成长:[\s\S]*?获得了 (\d+) 点经验")
REGEX_NASCENT_SOUL_ITEM = re.compile(r"-\s*【(.+?)】x ?(\d+)")
REGEX_DEEP_CULT_END = re.compile(r"【深度闭关结束】[\s\S]*?你的修为最终变化了 (-?\d+) 点", re.MULTILINE)
REGEX_TRADE_BUY_FAIL_NO_LISTING = re.compile(r"交易失败：挂单不存在或已被购买")
REGEX_TRADE_BUY_FAIL_NO_MONEY = re.compile(r"交易失败：你的灵石不足！\(需要: (\d+), 拥有: (\d+)\)")
REGEX_USE_ELIXIR_SUCCESS = re.compile(r"你服用了【(.+?)】，修为增加了 (\d+) 点！丹毒增加了 (\d+) 点")
# --- 正则表达式结束 ---

class Plugin(BasePlugin):
    """
    监听游戏机器人回复，解析关键事件（如资源变化）并发送通知。
    """
    def __init__(self, context: AppContext, plugin_name: str, cn_name: str | None = None):
        super().__init__(context, plugin_name, cn_name or "游戏事件通知")
        self.info("插件已加载。")
        self._my_username: Optional[str] = None
        self._duel_initiators: Dict[int, str] = {}

    def register(self):
        """注册游戏响应事件监听器"""
        self.event_bus.on("game_response_received", self.handle_game_response)
        self.event_bus.on("telegram_client_started", self._initialize_username)
        self.info("已注册 game_response_received 事件监听器。")

    async def _initialize_username(self):
        """获取并缓存自己的用户名"""
        if self.context.telegram_client:
             self._my_username = await self.context.telegram_client.get_my_username()
             self.info(f"已缓存当前用户名: {self._my_username}")

    async def handle_game_response(self, message: Message, is_reply_to_me: bool, is_mentioning_me: bool):
        """处理游戏机器人回复，解析并发送通知"""
        text = message.text or message.caption
        # --- 修改: 添加 try-except 块 ---
        try:
            preview = (text[:50] + '...') if text and len(text) > 50 else text
        except UnicodeError as e:
            self.error(f"创建日志预览时发生 Unicode 错误 (MsgID={message.id}): {e}")
            preview = "[日志预览创建失败]"
        except Exception as e:
            self.error(f"创建日志预览时发生未知错误 (MsgID={message.id}): {e}", exc_info=True)
            preview = "[日志预览创建失败]"
        # --- 修改结束 ---
        self.debug(f"收到 game_response 事件: MsgID={message.id}, ReplyToMe={is_reply_to_me}, MentionMe={is_mentioning_me}, IsEdit={message.edit_date is not None}, Preview='{preview}'")

        is_duel_result = bool(REGEX_DUEL_RESULT.search(text or "")) if text else False

        # 简化入口过滤逻辑
        if not is_reply_to_me and not is_mentioning_me and not is_duel_result:
            return

        if not text: return

        notifications = []
        is_craft_start_message = False
        is_nascent_soul_start_message = False

        # --- 解析逻辑 ---
        # 1. 闭关 和 奇遇 (省略...)
        cultivation_match = REGEX_CULTIVATION_RESULT.search(text); qiyu_item_matches = REGEX_QIYU_ITEM_GAIN.findall(text); qiyu_cultivation_match = REGEX_QIYU_CULTIVATION_GAIN.search(text); deep_cult_end_match = REGEX_DEEP_CULT_END.search(text)
        if deep_cult_end_match and message.edit_date and is_reply_to_me:
            change_str = deep_cult_end_match.group(1)
            try:
                change_points = int(change_str)
                op = "+" if change_points >= 0 else ""
                emoji = "🌌"
                notify_text = f"{emoji} 深度闭关: 修为 {op}{change_points}"
                qiyu_items_in_deep = REGEX_QIYU_ITEM_GAIN.findall(text)
                if qiyu_items_in_deep:
                    rewards = [f"{name.strip()} x{qty}" for name, qty in qiyu_items_in_deep]
                    notify_text += " | 奇遇: " + ", ".join(rewards)
                notifications.append(notify_text)
            except ValueError:
                self.warning(f"无法解析深度闭关修为变化: {change_str}")
        elif cultivation_match:
            result_type = cultivation_match.group(1)
            points_str = cultivation_match.group(2) or cultivation_match.group(3)
            points = int(points_str) if points_str and points_str.isdigit() else 0
            op = "+" if result_type == "闭关成功" else "-"
            emoji = "📈" if op == "+" else ("📉" if result_type == "闭关失败" else "💥")
            notifications.append(f"{emoji} 闭关: 修为 {op}{points}")

        if not deep_cult_end_match and not cultivation_match:
            if qiyu_item_matches:
                for item_name, quantity in qiyu_item_matches:
                    notifications.append(f"✨ 奇遇: 获得 {item_name.strip()} x{quantity}")
            if qiyu_cultivation_match:
                qiyu_points = qiyu_cultivation_match.group(1)
                notifications.append(f"✨ 奇遇: 修为 +{qiyu_points}")
        # 2. 药园事件 (省略...)
        harvest_match = REGEX_HARVEST_SUCCESS.search(text); maintenance_match = REGEX_MAINTENANCE_SUCCESS.search(text); no_need_match = REGEX_MAINTENANCE_NO_NEED.search(text); sow_success_match = REGEX_SOW_SUCCESS.search(text); sow_fail_match = REGEX_SOW_FAIL_SEED.search(text); buy_success_match = REGEX_BUY_SUCCESS.search(text); buy_fail_match = REGEX_BUY_FAIL_CONTRIBUTION.search(text)
        if harvest_match:
            item_name = harvest_match.group(1).strip()
            quantity = harvest_match.group(2)
            notifications.append(f"✅ 采药: 获得 {item_name} x{quantity}")
        elif maintenance_match:
            action = maintenance_match.group(1)
            count = maintenance_match.group(2)
            action_map = {"浇水": "💧", "除草": "🌿", "除虫": "🐛"}
            emoji = action_map.get(action, "🛠️")
            notifications.append(f"{emoji} {action}: 成功 ({count}块)")
        elif no_need_match:
            action = no_need_match.group(1)
            action_map = {"采药": "🧺", "浇水": "💧", "除草": "🌿", "除虫": "🐛"}
            emoji = action_map.get(action, "ℹ️")
            notifications.append(f"{emoji} {action}: 无需操作")
        elif sow_success_match:
            seed_name = sow_success_match.group(1).strip()
            notifications.append(f"✅ 播种: 成功 ({seed_name})")
        elif sow_fail_match:
            seed_name = sow_fail_match.group(1).strip()
            notifications.append(f"❌ 播种失败: {seed_name} 不足")
        elif buy_success_match:
            item_name = buy_success_match.group(1).strip()
            quantity = buy_success_match.group(2)
            notifications.append(f"✅ 兑换: 获得 {item_name} x{quantity}")
        elif buy_fail_match:
            notifications.append("❌ 兑换失败: 贡献不足")
        # 3. 宗门点卯 (省略...)
        checkin_success_match = REGEX_CHECKIN_SUCCESS.search(text); checkin_already_match = REGEX_CHECKIN_ALREADY.search(text); checkin_no_sect_match = REGEX_CHECKIN_NO_SECT.search(text)
        if checkin_success_match:
            contribution = checkin_success_match.group(1)
            notify_text = f"✅ 点卯: 贡献 +{contribution}"
            bonus_match = REGEX_CHECKIN_BONUS.search(text)
            salary_match = REGEX_CHECKIN_SALARY.search(text)
            notify_text += f", 额外 +{bonus_match.group(1)}" if bonus_match else ""
            notify_text += f", 俸禄 +{salary_match.group(1)} 灵石" if salary_match else ""
            notifications.append(notify_text)
        elif checkin_already_match:
            notifications.append("ℹ️ 点卯: 今日已完成")
        elif checkin_no_sect_match:
            notifications.append("⚠️ 点卯: 未加入宗门")
        # 4. 宗门传功 (省略...)
        teach_success_match = REGEX_TEACH_SUCCESS.search(text); teach_limit_match = REGEX_TEACH_LIMIT.search(text); teach_need_reply_match = REGEX_TEACH_NEED_REPLY.search(text); teach_no_sect_match = REGEX_TEACH_NO_SECT.search(text)
        if teach_success_match:
            contribution = teach_success_match.group(1)
            count = teach_success_match.group(2)
            notifications.append(f"✅ 传功: 贡献 +{contribution} ({count}/3)")
        elif teach_limit_match:
            notifications.append("ℹ️ 传功: 次数已用尽 (3/3)")
        elif teach_need_reply_match:
            notifications.append("⚠️ 传功失败: 需要回复消息")
        elif teach_no_sect_match:
            notifications.append("⚠️ 传功失败: 未加入宗门")
        # 5. 闯塔 (省略...)
        pagoda_report_floors_match = REGEX_PAGODA_REPORT_FLOORS.search(text); pagoda_already_match = REGEX_PAGODA_ALREADY.search(text)
        if pagoda_report_floors_match:
            floors = pagoda_report_floors_match.group(1)
            notify_text = f"🗼 闯塔: 通过 {floors} 层"
            cult_match = REGEX_PAGODA_REPORT_CULTIVATION.search(text)
            item_matches = REGEX_PAGODA_REPORT_ITEM.findall(text)
            rewards = []
            if cult_match:
                rewards.append(f"修为 +{cult_match.group(1)}")
            for item_name, quantity in item_matches:
                rewards.append(f"{item_name.strip()} x{quantity}")
            notify_text += " | 获得: " + ", ".join(rewards) if rewards else ""
            notifications.append(notify_text)
        elif pagoda_already_match:
            notifications.append("ℹ️ 闯塔: 今日已挑战")
        # 6. 配方学习 (省略...)
        learn_success_match = REGEX_LEARN_RECIPE_SUCCESS.search(text); learn_fail_match = REGEX_LEARN_RECIPE_FAIL_NO_ITEM.search(text)
        if learn_success_match:
            recipe_name = learn_success_match.group(1).strip()
            notifications.append(f"✅ 学习配方: {recipe_name}")
        elif learn_fail_match:
            notifications.append("❌ 学习配方失败: 背包无此物")

        # 7. 炼制
        craft_success_match = REGEX_CRAFT_SUCCESS_MULTI.search(text)
        craft_fail_material_match = REGEX_CRAFT_FAIL_MATERIAL.search(text)
        craft_fail_nonexist_match = REGEX_CRAFT_FAIL_NONEXIST.search(text)
        craft_start_match = REGEX_CRAFT_START.search(text)
        if craft_success_match and message.edit_date:
            success_count = craft_success_match.group(1)
            item_name = craft_success_match.group(2).strip()
            quantity_obtained = craft_success_match.group(3)
            notifications.append(f"✅ 炼制成功 ({success_count}次): 获得 {item_name} x{quantity_obtained}")
        elif craft_fail_material_match:
            item_name = craft_fail_material_match.group(1).strip()
            quantity_attempted = craft_fail_material_match.group(2)
            missing_materials_text = craft_fail_material_match.group(3).strip().replace("\n", ", ")
            notifications.append(f"❌ 炼制 {item_name} x{quantity_attempted} 失败: 缺少 {missing_materials_text}")
        elif craft_fail_nonexist_match:
            notifications.append("❌ 炼制失败: 物品不存在")
        elif craft_start_match:
            is_craft_start_message = True

        # 8. 交易 (省略...)
        trade_buy_success_match = REGEX_TRADE_BUY_SUCCESS.search(text); trade_sold_match = REGEX_TRADE_SOLD_NOTIFY.search(text); trade_fail_no_listing_match = REGEX_TRADE_BUY_FAIL_NO_LISTING.search(text); trade_fail_no_money_match = REGEX_TRADE_BUY_FAIL_NO_MONEY.search(text)
        if is_reply_to_me and trade_buy_success_match:
            item_name = trade_buy_success_match.group(1).strip()
            quantity = trade_buy_success_match.group(2)
            notifications.append(f"🛒 购买成功: 获得 {item_name} x{quantity}")
        elif trade_sold_match:
            target_user = trade_sold_match.group(1)
            sold_item = trade_sold_match.group(2).strip()
            sold_qty = trade_sold_match.group(3)
            got_item = trade_sold_match.group(4).strip()
            got_qty = trade_sold_match.group(5)
            if not self._my_username and self.context.telegram_client:
                self._my_username = await self.context.telegram_client.get_my_username()
            if self._my_username and target_user.lower() == self._my_username.lower():
                notifications.append(f"💰 售出 {sold_item} x{sold_qty} | 获得: {got_item} x{got_qty}")
        elif is_reply_to_me and trade_fail_no_listing_match:
            notifications.append("❌ 购买失败: 挂单不存在或已被购买")
        elif is_reply_to_me and trade_fail_no_money_match:
            needed = trade_fail_no_money_match.group(1)
            owned = trade_fail_no_money_match.group(2)
            notifications.append(f"❌ 购买失败: 灵石不足 (需{needed}, 有{owned})")
        # 9. 引道 (省略...)
        yindao_success_match = REGEX_YINDAO_SUCCESS.search(text); yindao_cooldown_match = REGEX_YINDAO_COOLDOWN.search(text)
        if is_reply_to_me and yindao_success_match:
            shenshi = yindao_success_match.group(1)
            notify_text = f"💧 引道成功: 神识 +{shenshi}"
            buff_match = REGEX_YINDAO_BUFF.search(text)
            notify_text += f" | 获得 Buff: {buff_match.group(1).strip()}" if buff_match else ""
            notifications.append(notify_text)

        # 10. 斗法 (省略...)
        duel_start_match = REGEX_DUEL_START.search(text); duel_result_match = REGEX_DUEL_RESULT.search(text); is_duel_related = False
        if duel_start_match and message.edit_date:
            is_duel_related = True
            attacker = duel_start_match.group(1)
            defender = duel_start_match.group(2)
            if not self._my_username and self.context.telegram_client:
                self._my_username = await self.context.telegram_client.get_my_username()
            if self._my_username and (attacker.lower() == self._my_username.lower() or defender.lower() == self._my_username.lower()):
                opponent = defender if attacker.lower() == self._my_username.lower() else attacker
                initiator = "未知"
                if message.reply_to_message and message.reply_to_message.text:
                    original_command_msg = message.reply_to_message
                    if original_command_msg.text.startswith(".斗法") and original_command_msg.from_user:
                        initiator = original_command_msg.from_user.first_name or f"User:{original_command_msg.from_user.id}"
                        if message.id:
                            self._duel_initiators[message.id] = initiator
                            loop = asyncio.get_event_loop()
                            loop.call_later(300, self._duel_initiators.pop, message.id, None)
                duel_start_notification = f"⚔️ 斗法开始: {attacker} vs {defender} (由 {initiator} 发起)"
                try:
                    self.info(f"解析到斗法开始，发送私聊通知: {duel_start_notification}")
                    await self.event_bus.emit("send_admin_private_notification", duel_start_notification)
                except Exception as e:
                    self.error(f"发送斗法开始私聊通知失败: {e}", exc_info=True)

        elif duel_result_match:
            is_duel_related = True
            winner = duel_result_match.group(1)
            win_gain = duel_result_match.group(2)
            loser = duel_result_match.group(3)
            lose_loss = duel_result_match.group(4)
            if not self._my_username and self.context.telegram_client:
                self._my_username = await self.context.telegram_client.get_my_username()
            if self._my_username and (winner.lower() == self._my_username.lower() or loser.lower() == self._my_username.lower()):
                opponent = loser if winner.lower() == self._my_username.lower() else winner
                result = "胜利" if winner.lower() == self._my_username.lower() else "失败"
                change = f"+{win_gain}" if result == "胜利" else f"-{lose_loss}"
                initiator = "未知"
                related_start_msg_id = message.reply_to_message_id if message.reply_to_message_id else None
                if related_start_msg_id:
                    initiator = self._duel_initiators.get(related_start_msg_id, "未知")
                duel_end_notification = f"🏁 斗法结束: 对手 @{opponent} | 结果: {result} | 修为: {change} (发起者: {initiator})"
                try:
                    self.info(f"解析到斗法结束，发送私聊通知: {duel_end_notification}")
                    await self.event_bus.emit("send_admin_private_notification", duel_end_notification)
                except Exception as e:
                    self.error(f"发送斗法结束私聊通知失败: {e}", exc_info=True)
        # 11. 元婴归窍 (省略...)
        nascent_soul_start_match = REGEX_NASCENT_SOUL_START.search(text); nascent_soul_rewards_match = REGEX_NASCENT_SOUL_REWARDS.search(text); is_nascent_soul_related = False
        if nascent_soul_rewards_match and message.edit_date:
            is_nascent_soul_related = True
            if is_reply_to_me:
                rewards_text = nascent_soul_rewards_match.group(1)
                exp_match = REGEX_NASCENT_SOUL_EXP.search(text)
                item_matches = REGEX_NASCENT_SOUL_ITEM.findall(rewards_text)
                rewards = []
                for item_name, quantity in item_matches:
                    rewards.append(f"{item_name.strip()} x{quantity}")
                if exp_match:
                    rewards.append(f"经验 +{exp_match.group(1)}")
                if rewards:
                    notifications.append(f"👶 元婴归窍: 获得 " + ", ".join(rewards))
                else:
                    notifications.append("👶 元婴归窍 (未解析到奖励)")
        elif nascent_soul_start_match:
            is_nascent_soul_start_message = True
            is_nascent_soul_related = True

        # 12. 使用物品结果 (丹药)
        use_elixir_match = REGEX_USE_ELIXIR_SUCCESS.search(text)
        if is_reply_to_me and use_elixir_match:
            item_name = use_elixir_match.group(1).strip()
            cult_gain = use_elixir_match.group(2)
            poison_gain = use_elixir_match.group(3)
            notifications.append(f"💊 使用 {item_name}: 修为 +{cult_gain}, 丹毒 +{poison_gain}")
        # --- Parsing Logic End ---

        # --- Send Notifications ---
        if notifications and not is_craft_start_message and not is_nascent_soul_start_message:
            if not self._my_username and self.context.telegram_client:
                self._my_username = await self.context.telegram_client.get_my_username()

            prefix = f"[{self._my_username or '助手'}] "
            final_message = prefix + " | ".join(notifications)

            try:
                if is_duel_related:
                    pass  # Duels already sent via PM
                elif (is_nascent_soul_related or deep_cult_end_match) and is_reply_to_me:  # 元婴归窍 或 深度闭关 (回复我们的)
                    self.info(f"解析到 {'元婴归窍' if is_nascent_soul_related else '深度闭关'} 事件 (MsgID={message.id})，发送通知: {final_message}")
                    await self.event_bus.emit("send_system_notification", final_message)
                elif is_reply_to_me or is_mentioning_me:  # 其他普通通知
                    self.info(f"解析到事件 (MsgID={message.id}, ReplyToMe={is_reply_to_me}, MentionMe={is_mentioning_me})，发送通知: {final_message}")
                    await self.event_bus.emit("send_system_notification", final_message)

            except Exception as e:
                self.error(f"发送游戏事件通知失败: {e}", exc_info=True)


import logging
import re
import asyncio
import random
from typing import Optional
from plugins.base_plugin import BasePlugin, AppContext
from pyrogram.types import Message

# --- 常量 ---
HIGH_RISK_CMD = ".献上魂魄"
LOW_RISK_CMD = ".收敛气息"
HIGH_RISK_PROBABILITY = 0.2 # 高风险选项的概率 (20%)

# 匹配魔君降临消息的正则表达式 (确保匹配 @提及 和关键短语)
# 使用 re.DOTALL 允许多行匹配
REGEX_DEMON_LORD_EVENT = re.compile(
    # r"@\S+！你感到一股无法抗拒的意志.*?脑海中响起.*?做出抉择.*?1\.\s*回复.*?\.献上魂魄.*?2\.\s*回复.*?\.收敛气息",
    # 更宽松一点，只匹配关键部分
    r"你感到一股无法抗拒的意志锁定了你的神魂.*?小辈，让老夫看看你的成色.*?做出抉择.*?\.献上魂魄.*?\收敛气息",
    re.DOTALL
)

class Plugin(BasePlugin):
    """
    处理“魔君降临”事件，根据概率自动回复。
    """
    def __init__(self, context: AppContext, plugin_name: str, cn_name: str | None = None):
        super().__init__(context, plugin_name, cn_name or "魔君降临")
        self.load_config()

        if self.auto_enabled:
            self.info(f"插件已加载并启用。高风险概率: {self.high_risk_prob * 100:.0f}%。")
        else:
            self.info("插件已加载但未启用。")

    def load_config(self):
        """加载配置"""
        self.auto_enabled = self.config.get("demon_lord.auto_enabled", True) # 默认启用
        # 允许从配置覆盖概率，但保持默认值 0.2
        prob_config = self.config.get("demon_lord.high_risk_probability", HIGH_RISK_PROBABILITY)
        try:
             self.high_risk_prob = float(prob_config)
             if not (0 <= self.high_risk_prob <= 1):
                 self.warning(f"配置的 high_risk_probability ({prob_config}) 无效，使用默认值 {HIGH_RISK_PROBABILITY}")
                 self.high_risk_prob = HIGH_RISK_PROBABILITY
        except ValueError:
             self.warning(f"无法解析配置的 high_risk_probability ({prob_config})，使用默认值 {HIGH_RISK_PROBABILITY}")
             self.high_risk_prob = HIGH_RISK_PROBABILITY
        
        # 响应延迟范围 (秒)
        delay_range = self.config.get("demon_lord.response_delay_seconds", [5, 15])
        if isinstance(delay_range, list) and len(delay_range) == 2 and all(isinstance(x, (int, float)) for x in delay_range) and 0 <= delay_range[0] <= delay_range[1]:
            self.min_delay = delay_range[0]
            self.max_delay = delay_range[1]
        else:
             self.warning(f"配置的 response_delay_seconds ({delay_range}) 格式无效，使用默认值 [5, 15]")
             self.min_delay = 5
             self.max_delay = 15

    def register(self):
        """注册游戏响应事件监听器"""
        if self.auto_enabled:
            self.event_bus.on("game_response_received", self.handle_game_response)
            self.info("已注册 game_response_received 事件监听器。")

    async def handle_game_response(self, message: Message, is_reply_to_me: bool, is_mentioning_me: bool):
        """处理游戏机器人回复，检查是否为魔君降临事件"""
        # 必须是提及我们的消息
        if not is_mentioning_me:
            return

        text = message.text or message.caption
        if not text:
            return

        # 检查是否匹配魔君降临的文本模式
        if REGEX_DEMON_LORD_EVENT.search(text):
            self.info(f"检测到魔君降临事件 (MsgID: {message.id})！准备做出抉择...")

            # 根据概率选择指令
            chosen_command = ""
            if random.random() < self.high_risk_prob:
                chosen_command = HIGH_RISK_CMD
                self.info(f"选择高风险选项: {chosen_command}")
            else:
                chosen_command = LOW_RISK_CMD
                self.info(f"选择低风险选项: {chosen_command}")

            # 构造带回复标记的完整指令
            command_to_send = f"{chosen_command} --reply_to {message.id}"

            # 计算随机延迟
            delay = random.uniform(self.min_delay, self.max_delay)
            self.info(f"将在 {delay:.1f} 秒后回复...")
            await asyncio.sleep(delay)

            # 发送指令到队列
            if self.context.telegram_client:
                success = await self.context.telegram_client.send_game_command(command_to_send)
                if success:
                    self.info(f"魔君降临回复指令 '{command_to_send}' 已成功加入队列。")
                else:
                    self.error(f"将魔君降临回复指令 '{command_to_send}' 加入队列失败！")
            else:
                self.error("无法发送魔君降临回复：TelegramClient 不可用。")


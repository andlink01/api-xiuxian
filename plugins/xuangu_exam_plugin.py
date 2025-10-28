import logging
import re
import asyncio
import random
from typing import Tuple, Dict, Optional
from plugins.base_plugin import BasePlugin, AppContext # 保持导入 BasePlugin
from pyrogram.types import Message, ReplyParameters, LinkPreviewOptions

REDIS_XUANGU_QA_PREFIX = "xuangu_qa"

class Plugin(BasePlugin):
    """
    处理玄骨考校自动答题与题目收集的插件。
    """
    def __init__(self, context: AppContext, plugin_name: str, cn_name: str | None = None):
        super().__init__(context, plugin_name, cn_name)
        self.config_enabled = self.config.get("xuangu_exam.enabled", False)
        self.auto_answer = self.config.get("xuangu_exam.auto_answer", False)
        self.use_ai = self.config.get("xuangu_exam.use_ai_fallback", False)
        self.delay = self.config.get("xuangu_exam.answer_delay_seconds", 5)
        self.notify_unknown = self.config.get("xuangu_exam.notify_on_unknown_question", True)
        self.admin_chat_id = self.config.get("telegram.control_chat_id") # 用于发送通知

        if self.config_enabled:
            self.info(f"插件已加载并启用。自动答题: {'是' if self.auto_answer else '否'}, AI备选: {'是' if self.use_ai else '否'}")
        else:
            self.info("插件已加载但未启用。")

    def register(self):
        """注册游戏响应监听器"""
        if self.config_enabled:
            self.event_bus.on("game_response_received", self.handle_game_response)
            self.info("已注册 game_response_received 事件监听器。")

    async def handle_game_response(self, message: Message, is_reply_to_me: bool, is_mentioning_me: bool):
        """处理来自游戏机器人的消息，检查是否为玄骨考校题目"""
        text = message.text or message.caption
        if not text:
            return

        parsed_data = self.parse_exam_message(text)
        if not parsed_data:
            return # 不是考校消息

        question, options, target_username = parsed_data
        self.info(f"检测到考校题目: @{target_username} - {question[:30]}...")

        my_username = None
        if self.context.telegram_client and self.context.telegram_client.app.is_connected:
             my_username = self.context.telegram_client._my_username
        if not my_username:
             self.error("无法获取当前用户名，无法判断题目目标。")
             return

        is_for_me = is_mentioning_me or (target_username.lower() == my_username.lower())
        self.debug(f"题目目标: @{target_username}, 我的用户名: @{my_username}, 是否@我: {is_for_me}")

        answer_text = await self.get_answer_from_db(question)
        source = "题库"

        if not answer_text and self.use_ai:
            self.info("题库未命中，尝试使用 AI 获取答案...")
            source = "AI"
            answer_text = await self.get_answer_from_ai(question, options)
            if answer_text:
                await self.save_answer_to_db(question, answer_text)
            else:
                 self.warning("AI未能提供有效答案。")

        if answer_text:
            self.info(f"找到答案 ({source}): {answer_text}")
            correct_option_letter = None
            for letter, option_text in options.items():
                if option_text.strip().lower() == answer_text.strip().lower():
                    correct_option_letter = letter
                    break

            if correct_option_letter:
                self.info(f"匹配到选项: {correct_option_letter}")
                if is_for_me and self.auto_answer:
                    delay_seconds = random.uniform(self.delay * 0.8, self.delay * 1.2)
                    self.info(f"题目是 @自己 的，将在 {delay_seconds:.1f} 秒后自动作答...")
                    await asyncio.sleep(delay_seconds)
                    await self.reply_answer(message, correct_option_letter)
                elif is_for_me:
                     self.info("题目是 @自己 的，但未开启自动答题。")
            else:
                self.error(f"找到了答案文本 '{answer_text}'，但在当前选项 {options} 中匹配失败！")
                if is_for_me and self.notify_unknown:
                     await self.notify_admin(f"【玄骨考校】：找到答案 '{answer_text}' 但无法匹配当前选项！\n题目: {question}\n选项: {options}")

        else:
            self.warning("未能从题库或 AI 获取答案。")
            if is_for_me and self.notify_unknown:
                 await self.notify_admin(f"【玄骨考校】：题库和 AI 均未找到答案！\n题目: {question}\n选项: {options}")

    def parse_exam_message(self, text: str) -> Optional[Tuple[str, Dict[str, str], str]]:
        """
        解析玄骨考校消息文本。
        返回 (问题, 选项字典, @目标用户名) 或 None。
        """
        mention_match = re.search(r"向 @(\w+) 提问", text)
        if not mention_match:
            return None
        target_username = mention_match.group(1)

        # --- (修改: 修正问题提取的正则表达式以处理内部引号) ---
        # 匹配从第一个 “ 开始，到第一个选项（如 A.）出现之前的最后一个 ” 之间的所有内容
        question_match = re.search(r"“(.+?)”(?=\s*\n\s*[ABCD][.\uff0e])", text, re.DOTALL)
        # --- (修改结束) ---

        if not question_match:
            # 尝试另一种可能：如果问题后直接跟选项，没有明显换行
            question_match_alt = re.search(r"“(.+?)”\s*[ABCD][.\uff0e]", text, re.DOTALL)
            if not question_match_alt:
                self.debug("未找到 “...” 包围的问题或问题后紧跟选项的格式。")
                return None
            else:
                question = question_match_alt.group(1).strip()
        else:
            question = question_match.group(1).strip()


        options = {}
        # 选项正则保持不变
        option_pattern = re.compile(r"([ABCD])[.\uff0e]\s*(.*?)(?=\n[ABCD][.\uff0e]|\n\n小辈|$)", re.DOTALL)
        # 从问题匹配结束的位置开始查找选项，避免误匹配问题内的 ABCD.
        search_start_pos = question_match.end() if question_match else (question_match_alt.end() if question_match_alt else 0)
        matches = option_pattern.findall(text, pos=search_start_pos)


        if not matches:
             # 如果上面的正则没找到（比如选项不在新行），尝试简单匹配
             simple_option_matches = re.findall(r"([ABCD])[.\uff0e]\s*([^\n]+)", text)
             if not simple_option_matches:
                  self.warning(f"无法从文本中提取有效的选项: {text[search_start_pos:search_start_pos+100]}...")
                  return None
             else:
                  matches = simple_option_matches # 使用简单匹配的结果

        for letter, option_text in matches:
            options[letter] = option_text.strip()

        if 'A' not in options or 'B' not in options:
             self.warning(f"提取到的选项不完整 (缺少A或B): {options}")
             return None

        if "你有" not in text or "作答 <选项>" not in text:
            return None

        # self.debug(f"成功解析考校题: Q='{question[:20]}...', Opts={options}, Target='{target_username}'")
        return question, options, target_username

    async def get_answer_from_db(self, question: str) -> Optional[str]:
        """从 Redis 查询答案"""
        if not self.context.redis: return None
        redis_client = self.context.redis.get_client()
        if not redis_client: return None
        redis_key = ""
        try:
            normalized_question = question.strip()
            redis_key = f"{REDIS_XUANGU_QA_PREFIX}:{normalized_question}"
            answer = await redis_client.get(redis_key)
            if answer:
                 self.debug(f"Redis 命中: {normalized_question[:20]}... -> {answer}")
                 return answer
            else:
                 self.debug(f"Redis 未命中: {normalized_question[:20]}...")
                 return None
        except Exception as e:
            self.error(f"查询 Redis 出错 (Key: {redis_key or '未知'}): {e}", exc_info=True)
            return None

    async def save_answer_to_db(self, question: str, answer: str):
        """将答案存入 Redis"""
        if not self.context.redis: return
        redis_client = self.context.redis.get_client()
        if not redis_client: return
        redis_key = ""
        try:
            normalized_question = question.strip()
            redis_key = f"{REDIS_XUANGU_QA_PREFIX}:{normalized_question}"
            await redis_client.set(redis_key, answer.strip(), ex=30*24*60*60)
            self.info(f"答案已存入 Redis: {normalized_question[:20]}... -> {answer.strip()}")
        except Exception as e:
            self.error(f"保存答案到 Redis 出错 (Key: {redis_key or '未知'}): {e}", exc_info=True)

    async def get_answer_from_ai(self, question: str, options: Dict[str, str]) -> Optional[str]:
        """使用 Gemini AI 获取答案"""
        if not self.context.gemini:
            self.warning("Gemini 客户端未初始化。")
            return None

        prompt = f"""请根据以下问题和选项，选择最正确的答案选项的 **文本内容**:

问题：
“{question}”

选项：
"""
        option_lines = []
        for letter in sorted(options.keys()):
            option_lines.append(f"{letter}. {options[letter]}")
        prompt += "\n".join(option_lines)
        prompt += "\n\n重要提示：请只输出你认为最正确选项的 **完整文本内容**，例如直接输出“催熟灵草灵药”，不要包含选项字母（如 B.）、引号或任何其他解释性文字。"

        try:
            self.info("向 Gemini AI 请求答案...")
            ai_response = await self.context.gemini.generate_text(prompt)
            self.info(f"AI Raw Response received: {ai_response!r}")
            if ai_response:
                ai_response_text = ai_response.strip().replace('"', '').replace("'", "")
                self.info(f"AI 返回初步结果 (已清理): '{ai_response_text}'")
                for option_text in options.values():
                    if option_text.strip().lower() == ai_response_text.lower():
                        self.info(f"AI 成功回答并精确匹配选项: {option_text.strip()}")
                        return option_text.strip()
                self.warning(f"AI 回答 '{ai_response_text}' 无法精确匹配，尝试模糊匹配 (AI in Option)...")
                possible_matches = []
                for letter, option_text in options.items():
                    opt_strip_lower = option_text.strip().lower()
                    ai_resp_lower = ai_response_text.lower()
                    if ai_resp_lower and opt_strip_lower and ai_resp_lower in opt_strip_lower:
                         possible_matches.append(option_text.strip())
                if not possible_matches:
                    self.warning(f"模糊匹配 (AI in Option) 失败，尝试反向模糊匹配 (Option in AI)...")
                    for letter, option_text in options.items():
                        opt_strip_lower = option_text.strip().lower()
                        ai_resp_lower = ai_response_text.lower()
                        if ai_resp_lower and opt_strip_lower and opt_strip_lower in ai_resp_lower:
                             possible_matches.append(option_text.strip())
                if len(possible_matches) == 1:
                    matched_text = possible_matches[0]
                    self.info(f"AI 回答通过模糊匹配成功确定唯一选项: {matched_text}")
                    return matched_text
                elif len(possible_matches) > 1:
                     self.warning(f"AI 回答 '{ai_response_text}' 模糊匹配到多个选项: {possible_matches}，无法确定唯一答案。")
                     return None
                else:
                     self.warning(f"AI 回答 '{ai_response_text}' 无法通过任何方式匹配任何选项。")
                     return None
            else:
                self.warning("AI 返回了空响应或 None。")
                return None
        except Exception as e:
            self.error(f"请求 Gemini AI 或处理其响应时出错: {e}", exc_info=True)
            return None

    async def reply_answer(self, original_message: Message, option_letter: str):
        """回复游戏机器人的考校消息 (将指令加入队列)"""
        if not self.context.telegram_client:
            self.error("无法回复答案：TelegramClient 未初始化。")
            return
        command = f".作答 {option_letter}"
        try:
            self.info(f"准备将考校答案指令 '{command}' 加入发送队列...")
            success = await self.context.telegram_client.send_game_command(command)
            if success:
                self.info(f"已将考校答案指令 '{command}' 加入队列。")
            else:
                self.error(f"将考校答案指令 '{command}' 加入队列失败！")
        except Exception as e:
            self.error(f"将考校答案指令 '{command}' 加入队列时出错: {e}", exc_info=True)

    async def notify_admin(self, text: str):
        """向管理员控制群发送通知"""
        if not self.admin_chat_id:
             self.warning("未配置控制群 ID (telegram.control_chat_id)，无法发送通知。")
             return
        if not self.context.telegram_client:
             self.error("无法发送通知：TelegramClient 未初始化。")
             return
        try:
            link_preview_options = LinkPreviewOptions(is_disabled=True)
            await self.context.telegram_client.app.send_message(
                self.admin_chat_id,
                text,
                link_preview_options=link_preview_options
            )
            self.info("已向管理员发送考校相关通知。")
        except Exception as e:
            self.error(f"向管理员发送通知失败: {e}", exc_info=True)


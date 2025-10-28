import google.generativeai as genai
from google.generativeai.types import generation_types # 导入 generation_types 用于更精细的错误处理
from core.config import Config
from core.logger import logger
from itertools import cycle
import asyncio
import logging # 导入 logging

# --- (修改: 使用用户指定的模型优先级列表) ---
MODEL_PREFERENCE = ['gemini-2.5-pro', 'gemini-2.5-flash', 'gemini-2.5-flash-lite']
# --- (修改结束) ---

async def verify_gemini_key(api_key: str) -> bool:
    """尝试使用一个 key 配置并简单查询，验证其有效性"""
    temp_logger = logging.getLogger("GeminiKeyVerify")
    try:
        genai.configure(api_key=api_key)
        # 尝试列出模型，这是一个轻量级的验证调用
        models = await asyncio.to_thread(genai.list_models)
        # 简单的检查，确保返回了模型列表并且至少有一个支持 generateContent
        return any('generateContent' in m.supported_generation_methods for m in models)
    except Exception as e:
        temp_logger.warning(f"验证 Gemini Key (...{api_key[-4:]}) 失败: {e}")
        return False

class GeminiClient:
    def __init__(self, config: Config):
        self.config = config
        self.all_api_keys = self.config.get("gemini.api_keys", [])
        self.valid_api_keys = [] # 存储验证有效的 key
        self.key_cycler = None
        self.current_key_index = -1 # 跟踪当前使用的 key 索引
        self._initialized = False # 标记是否已完成首次初始化和 Key 验证

        if not self.all_api_keys:
            logger.warning("Gemini API 密钥未配置。Gemini 模块将不可用。")
        else:
            logger.info(f"Gemini 模块已加载 {len(self.all_api_keys)} 个 API 密钥 (将在首次使用时验证)。")

    async def _initialize_if_needed(self):
        """首次使用时验证 key"""
        if self._initialized:
            return True

        logger.info("首次使用 Gemini，开始验证 API 密钥...")
        self.valid_api_keys = []
        for key in self.all_api_keys:
            if await verify_gemini_key(key):
                self.valid_api_keys.append(key)
            else:
                logger.warning(f"已移除无效的 Gemini Key (...{key[-4:]})。")

        if not self.valid_api_keys:
            logger.error("所有提供的 Gemini API 密钥均无效！Gemini 模块将不可用。")
            self.key_cycler = None
            self._initialized = True
            return False

        logger.info(f"找到 {len(self.valid_api_keys)} 个有效的 Gemini API 密钥。")
        self.key_cycler = cycle(self.valid_api_keys)
        self.current_key_index = 0
        self._initialized = True
        logger.info("Gemini 初始化完成。")
        return True

    def _configure_genai_with_current_key(self):
        """使用当前有效的密钥配置 genai"""
        if not self.valid_api_keys or self.current_key_index == -1:
             logger.error("无法配置 genai：没有有效的密钥或索引无效。")
             raise ValueError("No valid API keys available.")

        current_key = self.valid_api_keys[self.current_key_index]
        logger.debug(f"配置 Gemini genai，使用有效 Key #{self.current_key_index + 1} (...{current_key[-4:]})")
        try:
            genai.configure(api_key=current_key)
        except Exception as e:
            logger.error(f"使用 Key (...{current_key[-4:]}) 配置 genai 时出错: {e}")
            raise e

    def _rotate_to_next_key(self):
        """切换到下一个有效的密钥索引"""
        if not self.valid_api_keys or len(self.valid_api_keys) <= 1:
            return False

        self.current_key_index = (self.current_key_index + 1) % len(self.valid_api_keys)
        logger.info(f"轮换到下一个 Gemini Key (索引: {self.current_key_index + 1}/{len(self.valid_api_keys)})")
        return True


    async def generate_text(self, prompt: str) -> str | None:
        """异步生成文本，处理模型降级、API 错误和密钥轮换"""
        if not await self._initialize_if_needed():
            logger.error("无法生成文本：Gemini 初始化失败或无有效密钥。")
            return None

        attempts = len(self.valid_api_keys) if self.valid_api_keys else 0
        if attempts == 0:
             logger.error("无法生成文本：没有有效的 Gemini API 密钥。")
             return None

        start_key_index = self.current_key_index
        last_exception = None

        for i in range(attempts): # 遍历所有有效的 key
            current_attempt_key_index = (start_key_index + i) % attempts
            if self.current_key_index != current_attempt_key_index:
                 self.current_key_index = current_attempt_key_index

            try:
                # 配置当前 Key
                self._configure_genai_with_current_key()

                # 尝试不同的模型
                for model_name in MODEL_PREFERENCE:
                    try:
                        logger.debug(f"尝试使用模型 '{model_name}' (Key #{self.current_key_index + 1})...")
                        model = genai.GenerativeModel(model_name)
                        response = await model.generate_content_async(prompt)

                        # 处理成功响应
                        if response.parts and hasattr(response.parts[0], 'text'):
                            logger.info(f"Gemini 请求成功 (模型: '{model_name}', Key #{self.current_key_index + 1})")
                            return response.text

                        # 处理被阻止或空响应
                        else:
                            finish_reason = getattr(response, 'finish_reason', generation_types.FinishReason.UNKNOWN)
                            safety_ratings = getattr(response.prompt_feedback, 'safety_ratings', '无安全反馈')
                            block_reason = getattr(response.prompt_feedback, 'block_reason', '未知原因')
                            logger.warning(f"Gemini 响应被阻止或为空 (模型: '{model_name}', Key #{self.current_key_index + 1})。完成原因: {finish_reason}, 阻止原因: {block_reason}, 安全评级: {safety_ratings}。")
                            last_exception = Exception(f"Response blocked or empty. Reason: {block_reason or finish_reason}")
                            # 继续尝试下一个 model

                    # 更精细的异常处理
                    except generation_types.StopCandidateException as sce:
                         logger.warning(f"Gemini 请求被停止 (模型: '{model_name}', Key #{self.current_key_index + 1}): {sce}")
                         last_exception = sce
                         # 继续尝试下一个 model
                    except google.api_core.exceptions.ResourceExhausted as ree:
                         logger.warning(f"Gemini API 资源耗尽/速率限制 (模型: '{model_name}', Key #{self.current_key_index + 1}): {ree}。尝试下一个模型...")
                         last_exception = ree
                         # 继续尝试下一个 model
                    except google.api_core.exceptions.InvalidArgument as iae:
                         logger.warning(f"Gemini API 无效参数 (模型: '{model_name}', Key #{self.current_key_index + 1}): {iae}。可能是模型不可用，尝试下一个模型...")
                         last_exception = iae
                         # 继续尝试下一个 model
                    except google.api_core.exceptions.GoogleAPIError as api_err:
                         logger.warning(f"Gemini API 错误 (模型: '{model_name}', Key #{self.current_key_index + 1}): {api_err.__class__.__name__}: {api_err}。尝试下一个 Key...")
                         last_exception = api_err
                         break # 跳出内层模型循环，尝试下一个 Key
                    except Exception as e:
                        logger.error(f"Gemini 未知错误 (模型: '{model_name}', Key #{self.current_key_index + 1}): {e.__class__.__name__}: {e}", exc_info=True)
                        last_exception = e
                        break # 假设是 Key 问题，退出 model 循环

                # 如果内层模型循环正常结束或 break

            except ValueError as ve: # genai 配置失败
                 logger.error(f"配置 genai 失败 (Key #{self.current_key_index + 1}): {ve}。跳过此 Key。")
                 last_exception = ve
            except Exception as conf_e: # 配置 genai 时发生其他异常
                 logger.error(f"配置 genai 时发生未知错误 (Key #{self.current_key_index + 1}): {conf_e}", exc_info=True)
                 last_exception = conf_e

            # 如果当前 key 的尝试失败，且还有下一个 key 可试，则轮换
            if i < attempts - 1:
                logger.info("当前 Key 的所有模型尝试失败或遇到 Key 相关错误，轮换到下一个 Key...")
                self._rotate_to_next_key()
            else:
                 logger.error("所有有效的 Gemini API Key 和模型组合尝试均失败。")
                 if self.valid_api_keys:
                      self.current_key_index = 0 # 重置以便下次调用

        # 所有循环结束仍未成功
        logger.error(f"无法使用 Gemini 生成文本。最后错误: {last_exception}")
        return None

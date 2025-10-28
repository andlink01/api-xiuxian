import aiohttp
import json
import asyncio
from core.config import Config
from core.logger import logger

class HTTPClient:
    def __init__(self, config: Config):
        self.config = config
        self.session: aiohttp.ClientSession | None = None
        self.cookie_str = self.config.get("api_services.shared_cookie", "")

    async def _get_headers(self) -> dict:
        """动态获取 headers，确保 cookie 是最新的 (如果需要的话)"""
        headers = {
            **({"Cookie": self.cookie_str} if self.cookie_str else {}),
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0.1 Mobile/15E148 Safari/604.1",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh-Hans;q=0.9",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
        return headers

    async def create_session(self):
        if self.session and not self.session.closed:
             return
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            self.session = aiohttp.ClientSession(timeout=timeout)
            logger.info("【HTTP客户端】aiohttp.ClientSession 已创建 (超时 30 秒)。")
        except Exception as e:
            logger.error(f"【HTTP客户端】创建 aiohttp.ClientSession 失败: {e}")
            self.session = None

    async def close_session(self):
        if self.session and not self.session.closed:
            try:
                await self.session.close()
                logger.info("【HTTP客户端】aiohttp.ClientSession 已关闭。")
            except Exception as e:
                 logger.warning(f"【HTTP客户端】关闭 aiohttp.ClientSession 时出错: {e}")
            finally:
                self.session = None
        elif self.session and self.session.closed:
             self.session = None

    def get_session(self) -> aiohttp.ClientSession | None:
        if self.session and not self.session.closed:
            return self.session
        else:
            # 尝试自动创建 session (如果尚未创建)
            # logger.warning("【HTTP客户端】HTTP Session 不可用或已关闭，尝试自动创建...")
            # asyncio.create_task(self.create_session()) # 在后台创建
            return None # 暂时返回 None，让调用者处理或重试

    async def _handle_response(self, response: aiohttp.ClientResponse, url: str) -> dict | list | None:
        """【新增】统一处理响应，记录更多信息"""
        logger.debug(f"【HTTP客户端】GET {url} Status: {response.status}")
        raw_text = await response.text() # 先获取原始文本
        log_preview = (raw_text[:200] + '...') if len(raw_text) > 200 else raw_text

        try:
            response.raise_for_status() # 检查 HTTP 错误状态码
            # 尝试解析 JSON
            # content_type=None 允许解析非标准 application/json 类型
            data = json.loads(raw_text)
            logger.debug(f"【HTTP客户端】GET {url} 响应解析成功 (类型: {type(data).__name__})。预览: {log_preview}")
            return data
        except aiohttp.ClientResponseError as e:
            logger.error(f"【HTTP客户端】请求 API ({url}) 失败 (状态码: {e.status}): {e.message}")
            logger.error(f"【HTTP客户端】错误响应体预览: {log_preview}")
            return None
        except json.JSONDecodeError as json_err:
             logger.error(f"【HTTP客户端】解析 API ({url}) 的 JSON 响应时出错: {json_err}.")
             logger.error(f"【HTTP客户端】原始响应文本预览: {log_preview}")
             return None
        except Exception as e:
             logger.error(f"【HTTP客户端】处理 API ({url}) 响应时发生意外错误: {e}", exc_info=True)
             logger.error(f"【HTTP客户端】原始响应文本预览 (可能不完整): {log_preview}")
             return None

    async def get_cultivator_data(self, username: str) -> dict | None:
        """请求角色和储物袋 API"""
        if not username:
            logger.error("【HTTP客户端】无法获取角色数据：用户名为空。")
            return None

        url = f"https://asc.aiopenai.app/api/cultivator/{username}"
        session = self.get_session()
        if not session:
             logger.warning(f"【HTTP客户端】HTTP Session 不可用，尝试为 GET {url} 创建新 session...")
             await self.create_session()
             session = self.get_session()
             if not session:
                  logger.error(f"【HTTP客户端】无法执行 GET 请求 {url}: 创建 HTTP Session 失败。")
                  return None

        try:
            headers = await self._get_headers()
            logger.debug(f"【HTTP客户端】GET {url} Headers: {headers}")

            async with session.get(url, headers=headers) as response:
                data = await self._handle_response(response, url)
                if isinstance(data, dict):
                     return data
                elif data is not None: # 解析成功但类型不对
                     logger.error(f"【HTTP客户端】请求 {url} 返回的数据格式不是字典: {type(data)}")
                     return None
                else: # 解析失败或HTTP错误
                     return None # _handle_response 已记录错误

        except asyncio.TimeoutError:
             logger.error(f"【HTTP客户端】请求角色 API ({url}) 超时")
             return None
        except Exception as e:
             # _handle_response 应该已处理大部分异常，这里捕获其他意外错误
             logger.error(f"【HTTP客户端】请求角色 API ({url}) 时发生最外层意外错误: {e}", exc_info=True)
             return None

    async def get_all_items(self) -> list | None:
        """请求游戏物品 API"""
        url = "https://asc.aiopenai.app/api/all_items"
        session = self.get_session()
        if not session:
             logger.warning(f"【HTTP客户端】HTTP Session 不可用，尝试为 GET {url} 创建新 session...")
             await self.create_session()
             session = self.get_session()
             if not session:
                  logger.error(f"【HTTP客户端】无法执行 GET 请求 {url}: 创建 HTTP Session 失败。")
                  return None

        try:
            headers = await self._get_headers()
            async with session.get(url, headers=headers) as response:
                data = await self._handle_response(response, url)
                if isinstance(data, list):
                     return data
                elif data is not None:
                     logger.error(f"【HTTP客户端】请求 {url} 返回的数据格式不是列表: {type(data)}")
                     return None
                else:
                     return None
        except asyncio.TimeoutError:
             logger.error(f"【HTTP客户端】请求物品 API ({url}) 超时")
             return None
        except Exception as e:
             logger.error(f"【HTTP客户端】请求物品 API ({url}) 时发生最外层意外错误: {e}", exc_info=True)
             return None

    async def get_shop_items(self) -> list | None:
        """请求游戏商店物品 API"""
        url = "https://asc.aiopenai.app/api/shop_items"
        session = self.get_session()
        if not session:
             logger.warning(f"【HTTP客户端】HTTP Session 不可用，尝试为 GET {url} 创建新 session...")
             await self.create_session()
             session = self.get_session()
             if not session:
                  logger.error(f"【HTTP客户端】无法执行 GET 请求 {url}: 创建 HTTP Session 失败。")
                  return None

        try:
            headers = await self._get_headers()
            async with session.get(url, headers=headers) as response:
                data = await self._handle_response(response, url)
                if isinstance(data, list):
                     return data
                elif data is not None:
                     logger.error(f"【HTTP客户端】请求商店 API ({url}) 返回的数据格式不是列表: {type(data)}")
                     return None
                else:
                     return None
        except asyncio.TimeoutError:
             logger.error(f"【HTTP客户端】请求商店 API ({url}) 超时")
             return None
        except Exception as e:
             logger.error(f"【HTTP客户端】请求商店 API ({url}) 时发生最外层意外错误: {e}", exc_info=True)
             return None

    async def get_marketplace_listings(self, search_term: str | None = None, page: int = 1) -> dict | None:
        """请求万宝楼物品列表 API"""
        # 构建基础 URL
        url = "https://asc.aiopenai.app/api/marketplace_listings"
        params = {"page": str(page)}
        if search_term:
            params["search"] = search_term
            logger.debug(f"【HTTP客户端】查询万宝楼，搜索词: '{search_term}', 页码: {page}")
        else:
            logger.debug(f"【HTTP客户端】查询万宝楼，页码: {page}")

        session = self.get_session()
        if not session:
             logger.warning(f"【HTTP客户端】HTTP Session 不可用，尝试为 GET {url} 创建新 session...")
             await self.create_session()
             session = self.get_session()
             if not session:
                  logger.error(f"【HTTP客户端】无法执行 GET 请求 {url}: 创建 HTTP Session 失败。")
                  return None

        try:
            headers = await self._get_headers()
            # 使用 params 参数传递查询字符串
            async with session.get(url, headers=headers, params=params) as response:
                # 使用统一的响应处理函数
                data = await self._handle_response(response, f"{url}?{params}")
                if isinstance(data, dict):
                     # API 返回的是字典，符合预期
                     return data
                elif data is not None:
                     logger.error(f"【HTTP客户端】请求万宝楼 API ({url}) 返回的数据格式不是字典: {type(data)}")
                     return None
                else:
                     return None # _handle_response 已记录错误
        except asyncio.TimeoutError:
             logger.error(f"【HTTP客户端】请求万宝楼 API ({url}) 超时")
             return None
        except Exception as e:
             logger.error(f"【HTTP客户端】请求万宝楼 API ({url}) 时发生最外层意外错误: {e}", exc_info=True)
             return None


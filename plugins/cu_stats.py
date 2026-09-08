# -*- coding:UTF-8 -*- #
# Created at 2026/9/6, Project "hiklqqbot"
# Creator: Unauthorized HOPE


import logging
# 移除 httpx 导入，改用 curl_cffi
from curl_cffi import requests as curl_requests
from plugins.base_plugin import BasePlugin
from reply import Reply
from ui_builder import make_command_button, make_button_row, make_keyboard
from stats_manager import stats_manager

# 你可以在这里修改默认的 MediaWiki API 地址
DEFAULT_API_URL = "https://casualtiesunknown.huijiwiki.com/api.php"
# USER_AGENT = "CUWikiBot/1.0 (UnauthHOPE@outlook.com) HiklQQBot/2.0"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36"
WIKI_NAME = "未知伤亡"
# 短页面阈值（字节），可自行调整
SHORT_PAGE_THRESHOLD = 170
# 样本数量（用于估算短页面数，设为 0 则遍历全部，谨慎！）
SAMPLE_LIMIT = 500


class WikiStatsPlugin(BasePlugin):
    """
    查询 MediaWiki 站点统计信息，包括总页面、编辑数、待巡查（若支持）、短页面样本估算
    """

    def __init__(self):
        super().__init__(
            command="wiki统计",  # 主命令
            description=f"查询 {WIKI_NAME} Wiki 的统计数据",  # 描述
            category="",  # 多级分类，会在 /help 中显示
            display_name="Wiki统计",  # 菜单显示名称（2-4字）
            is_builtin=False,
            hidden=False,
        )
        self.logger = logging.getLogger("plugin.wiki_stats")
        self.api_url = DEFAULT_API_URL
        # 新增：统一 User-Agent
        self.user_agent = USER_AGENT
        # 新增：curl_cffi 模拟浏览器指纹（可选 chrome120, firefox120 等）
        self.impersonate = "chrome120"

    # ---------- 新增统一请求方法 ----------
    async def _api_request(self, params: dict, timeout: int = 30) -> dict | None:
        """
        使用 curl_cffi 发送 API 请求，返回 JSON 数据，失败返回 None
        :param params: API 参数字典
        :param timeout: 超时秒数，默认 30 秒（Cloudflare 验证可能需要时间）
        """
        headers = {'User-Agent': self.user_agent}
        try:
            # 使用异步会话，模拟浏览器指纹
            async with curl_requests.AsyncSession() as session:
                resp = await session.get(
                    self.api_url,
                    params=params,
                    headers=headers,
                    impersonate=self.impersonate,
                    timeout=timeout
                )
                # 记录状态码和响应预览（调试用）
                self.logger.info(f"状态码: {resp.status_code}")
                self.logger.info(f"响应前200字符: {resp.text[:200]}")

                if resp.status_code != 200:
                    self.logger.error(f"API 返回非200状态: {resp.status_code}")
                    return None

                # 尝试解析 JSON
                return resp.json()
        except Exception as e:
            self.logger.error(f"API 请求失败: {e}")
            return None

    async def handle(self, params: str, user_id: str = None, group_openid: str = None, **kwargs) -> Reply:
        """
        处理 wiki统计 命令
        如果用户带了参数，可当作新的 API 地址（可选功能）
        """
        # 如果用户传入了自定义 API 地址（如 "wiki统计 https://xxx.com/api.php"）
        if params and params.strip().startswith("http"):
            self.api_url = params.strip()
            self.logger.info(f"用户 {user_id} 切换 API 至 {self.api_url}")

        # 1. 获取基础统计数据
        base_stats = await self._fetch_base_stats()
        if base_stats is None:
            return Reply(text="❌ 无法连接 MediaWiki API，请检查地址或网络。")

        # 2. 尝试获取待巡查页面数（如果 Wiki 启用了 FlaggedRevs）
        pending_count = await self._fetch_pending_count()
        if pending_count is None:
            pending_text = "未启用或无法获取"
        else:
            pending_text = str(pending_count)

        # 3. 估算短页面数（基于样本）
        short_count = await self._fetch_short_pages()
        if short_count is None:
            short_text = "样本获取失败"
        else:
            short_text = f"{short_count}（样本 {SAMPLE_LIMIT} 页，阈值 {SHORT_PAGE_THRESHOLD} 字节）"

        # 4. 构造 Markdown 回复
        md = (
            f"# 📊 Wiki 统计信息\n\n"
            f"• **总页面数**：{base_stats.get('pages', 0)}\n"
            f"• **总编辑数**：{base_stats.get('edits', 0)}\n"
            f"• **注册用户**：{base_stats.get('users', 0)}\n"
            f"• **活跃用户**：{base_stats.get('activeusers', 0)}\n"
            f"• **待巡查页面**：{pending_text}\n"
            f"• **短页面数**（< {SHORT_PAGE_THRESHOLD} 字节）：{short_text}\n\n"
            f"> 数据来源：{self.api_url}"
        )

        # 5. 添加一个简单的按钮：刷新（点击重新执行命令）
        keyboard = make_keyboard([
            make_button_row([
                make_command_button(
                    action_type=2,  # 发送型（群聊会填入输入框，用户需手动发送）
                    label="🔄 刷新",
                    command="/wiki统计",  # 注意框架会自动补 /，所以这里写命令名即可
                    style=1,
                    permission_user_ids=[user_id]  # 仅触发者可以点
                )
            ])
        ])

        return Reply(markdown=md, keyboard=keyboard)

    # ---------- 私有方法 ----------
    async def _fetch_base_stats(self) -> dict | None:
        """获取核心统计 (pages, edits, users, activeusers)"""
        params = {
            'action': 'query',
            'meta': 'siteinfo',
            'siprop': 'statistics',
            'format': 'json'
        }
        data = await self._api_request(params)
        if data is None:
            return None
        return data.get('query', {}).get('statistics', {})

    async def _fetch_pending_count(self) -> int | None:
        """
        尝试获取待巡查页面数。
        优先检查 statistics 中是否已有 'flaggedpages' 字段（某些 FlaggedRevs 版本会提供）
        否则尝试调用 list=flaggedpages 并统计总数（可能费时，但这里只取第一页以判断是否存在）
        如果既没有字段，调用 flaggedpages 也失败，则返回 None 表示不支持。
        """
        # 先检查 statistics 是否包含 flaggedpages 计数
        params = {
            'action': 'query',
            'meta': 'siteinfo',
            'siprop': 'statistics',
            'format': 'json'
        }
        data = await self._api_request(params)
        if data:
            stats = data.get('query', {}).get('statistics', {})
            if 'flaggedpages' in stats:
                return stats['flaggedpages']

        # 尝试探测 flaggedpages 扩展是否存在
        test_params = {
            'action': 'query',
            'list': 'flaggedpages',
            'fpstate': '0',
            'fplimit': '1',
            'format': 'json'
        }
        test_data = await self._api_request(test_params)
        if test_data and 'query' in test_data and 'flaggedpages' in test_data['query']:
            # 扩展存在，但未在 statistics 提供计数，暂不实现分页累加
            self.logger.info("FlaggedRevs 扩展存在，但未在 statistics 提供计数，需分页获取，暂不实现")
            return None
        return None

    async def count_short_pages(self, threshold: int, max_pages: int = None) -> tuple[int, int] | None:
        """
        统计短页面数量（长度 < threshold 字节），使用自定义阈值遍历 allpages
        :param threshold: 自定义阈值（字节）
        :param max_pages: 最多遍历的页面总数，None 表示全部（可能很慢）
        :return: (短页面数量, 检查过的页面总数) 或 None（失败）
        """
        short_count = 0
        total_checked = 0
        offset = 0
        limit = 500  # 每批最大

        params = {
            'action': 'query',
            'list': 'allpages',
            'apfilterredir': 'nonredirects',  # 排除重定向
            'prop': 'info',
            'inprop': 'length',
            'aplimit': limit,
            'format': 'json'
        }

        while True:
            if max_pages and total_checked >= max_pages:
                break

            params['apoffset'] = offset
            data = await self._api_request(params, timeout=30)
            if data is None:
                return None  # 某次请求失败则整体失败

            pages = data.get('query', {}).get('allpages', [])
            if not pages:
                break

            for page in pages:
                length = page.get('length', 0)
                if length < threshold:
                    short_count += 1
                total_checked += 1

            # 检查是否有更多页面
            if 'continue' in data:
                offset = data['continue'].get('apoffset')
                if not offset:
                    break
            else:
                break

        return short_count, total_checked

    async def _fetch_short_pages(self) -> int | None:
        """
        通过 MediaWiki 内置的 querypage=Shortpages 获取短页面总数（使用系统阈值）
        注意：系统阈值由 $wgShortPagesThreshold 定义，与自定义阈值不同。
        这里仅用于演示，实际你可能想用 count_short_pages 自定义阈值。
        本方法保留作为备选。
        """
        params = {
            'action': 'query',
            'list': 'querypage',
            'qppage': 'Shortpages',
            'qplimit': 500,
            'format': 'json'
        }

        total = 0
        offset = 0

        while True:
            params['qpoffset'] = offset
            data = await self._api_request(params, timeout=30)
            if data is None:
                return None

            results = data.get('query', {}).get('querypage', {}).get('results', [])
            total += len(results)

            # 检查是否还有更多数据
            if 'continue' not in data:
                break
            offset = data['continue'].get('qpoffset')
            if not offset:
                break

        return total
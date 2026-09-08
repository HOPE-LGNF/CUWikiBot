# -*- coding:UTF-8 -*- #
# Created at 2026/9/6, Project "hiklqqbot"
# Creator: Unauthorized HOPE

"""Wiki 统计：顶部配置、只读查询、结果格式化和 QQ 命令适配。"""

import logging
from dataclasses import dataclass

from curl_cffi import requests as curl_requests

from plugins.base_plugin import BasePlugin
from reply import Reply
from ui_builder import make_button_row, make_command_button, make_keyboard

# 小范围自用，直接在此修改配置；聊天命令不接受任意网络地址。
DEFAULT_API_URL = "https://casualtiesunknown.huijiwiki.com/api.php"
WIKI_NAME = "未知伤亡"
# 使用指纹自带的浏览器头，避免手写 User-Agent 与 TLS 指纹版本不一致。
CHROME_FINGERPRINT = "chrome131"
REQUEST_TIMEOUT = 15  # 每次请求秒数；一次命令最多两次请求，不自动重试。
SHORT_PAGE_LIMIT = 500  # API 单批上限为 500；不是随机样本，也不用于估算全站总数。

logger = logging.getLogger("plugin.wiki_stats")


@dataclass(frozen=True)
class WikiStats:
    """一次查询的结果，不携带 QQ 用户或回复对象。"""

    statistics: dict[str, int]
    short_pages: int | None
    short_has_more: bool = False
    short_cached: bool = False


async def _api_request(session: curl_requests.AsyncSession, **params) -> dict | None:
    """返回 API 响应，保留 continuation；HTTP 成功但 API 报错也算失败。"""
    try:
        response = await session.post(
            DEFAULT_API_URL,
            data={"action": "query", "format": "json", **params},
            timeout=REQUEST_TIMEOUT,
            allow_redirects=False,
        )
        if response.status_code != 200:
            logger.warning("Wiki API HTTP %s", response.status_code)
            return None
    except curl_requests.RequestsError as exc:
        logger.warning("Wiki API 请求失败：%s", exc)
        return None
    try:
        data = response.json()
    except ValueError:
        logger.warning("Wiki API 返回非 JSON，HTTP %s；可能是验证页面", response.status_code)
        return None

    if not isinstance(data, dict) or "error" in data or "errors" in data:
        logger.warning("Wiki API 返回错误或非对象 JSON")
        return None
    query = data.get("query")
    if not isinstance(query, dict):
        logger.warning("Wiki API 缺少 query 对象")
        return None
    return data


async def fetch_wiki_stats() -> WikiStats | None:
    """一次命令共用一个会话；基础统计失败终止，短页面失败降级。"""
    # Cookie Jar 贯穿本次统计任务，退出时关闭；指纹模拟不会执行 JS challenge。
    async with curl_requests.AsyncSession(impersonate=CHROME_FINGERPRINT) as session:
        data = await _api_request(session, meta="siteinfo", siprop="statistics")
        statistics = data["query"].get("statistics") if data is not None else None
        if not isinstance(statistics, dict) or not statistics:
            logger.warning("Wiki API 缺少有效的 statistics 对象")
            return None
        # 缺失或异常字段不能显示成 0，以免把未知数据当作真实统计。
        statistics = {
            key: value for key, value in statistics.items()
            if key in {"pages", "edits", "users", "activeusers"}
            and type(value) is int and value >= 0
        }
        if not statistics:
            return None

        # ponytail: 仅取首批，超过 500 条时标注未读完；需要精确列表数再增加有界分页。
        data = await _api_request(
            session, list="querypage", qppage="Shortpages", qplimit=SHORT_PAGE_LIMIT,
        )
        short = data["query"].get("querypage") if data is not None else None
        if not isinstance(short, dict) or not isinstance(short.get("results"), list):
            return WikiStats(statistics, None)
        results = short["results"]
        if any(not isinstance(page, dict) or not isinstance(page.get("title"), str)
               for page in results):
            logger.warning("Wiki API 短页面列表条目无效")
            return WikiStats(statistics, None)
        return WikiStats(
            statistics, len(results),
            "continue" in data or "query-continue" in data,
            "cached" in short,
        )


def format_wiki_stats(stats: WikiStats) -> str:
    """展示 API 的实际口径；Shortpages 列表不能当作自定义阈值的全站统计。"""
    if stats.short_pages is None:
        short_text = "获取失败"
    else:
        short_text = f"已读取 {stats.short_pages} 条"
        if stats.short_has_more:
            short_text += "（列表还有后续，本次未继续读取）"
        if stats.short_cached:
            short_text += "（站点缓存）"
    base = stats.statistics
    return (
        f"# 📊 {WIKI_NAME} Wiki 统计信息\n\n"
        f"• **总页面数**：{base.get('pages', '无法获取')}\n"
        f"• **总编辑数**：{base.get('edits', '无法获取')}\n"
        f"• **注册用户**：{base.get('users', '无法获取')}\n"
        f"• **活跃用户**：{base.get('activeusers', '无法获取')}\n"
        # TODO: 确认目标站巡查机制和权限；FlaggedRevs 审核不等于 MediaWiki 巡查。
        f"• **待巡查页面**：暂不可用（统计口径待确认）\n"
        f"• **短页面列表**：{short_text}\n\n"
        f"> 短页面按站点 Shortpages 列表展示，不代表全站短页面总数。\n"
        f"> 数据来源：{DEFAULT_API_URL}"
    )


class WikiStatsPlugin(BasePlugin):
    """将 /wiki统计 适配为一次查询和 QQ 回复。"""

    def __init__(self):
        super().__init__(
            command="wiki统计",
            description=f"查询 {WIKI_NAME} Wiki 的统计数据",
            display_name="Wiki统计",
        )

    async def handle(self, params: str, user_id: str = None,
                     group_openid: str = None, **kwargs) -> Reply:
        if params and params.strip():
            return Reply(text="请使用 /wiki统计；数据源由维护者在源码顶部配置。")

        stats = await fetch_wiki_stats()
        if stats is None:
            return Reply(text="❌ 无法获取 Wiki 统计，请检查 API 地址、网络或站点访问限制。")

        keyboard = None
        # 当前框架可能取不到群用户 ID；此时省略按钮，避免生成 [None] 权限列表。
        if user_id:
            keyboard = make_keyboard([make_button_row([make_command_button(
                button_id="wiki_stats_refresh",
                label="🔄 刷新",
                command="/wiki统计",
                style=1,
                permission_user_ids=[user_id],
            )])])
        return Reply(markdown=format_wiki_stats(stats), keyboard=keyboard)

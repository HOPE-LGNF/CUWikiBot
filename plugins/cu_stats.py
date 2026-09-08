# -*- coding:UTF-8 -*- #
# Created at 2026/9/6, Project "hiklqqbot"
# Creator: Unauthorized HOPE

"""Wiki 统计：顶部配置、只读查询、结果格式化和 QQ 命令适配。"""

import logging
import re
from dataclasses import dataclass

from curl_cffi import requests as curl_requests
from curl_cffi.requests.exceptions import Timeout

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


class WikiApiError(Exception):
    """仅携带可安全展示的分类信息，不携带响应、Cookie 或请求参数。"""

    def __init__(self, kind: str, message: str, *, code: str = "", status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.code = code if re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", code) else ""
        self.status = status


def _api_error(error: object, status: int) -> WikiApiError:
    code = error.get("code", "") if isinstance(error, dict) else ""
    code = code if isinstance(code, str) else ""
    if code in {"permissiondenied", "rcpermissiondenied", "readapidenied"}:
        return WikiApiError(
            "permission", "当前身份无权执行该查询或站点限制了此能力", code=code, status=status
        )
    if code in {"assertuserfailed", "assertnameduserfailed", "notloggedin"}:
        return WikiApiError(
            "authentication", "Wiki 登录身份已失效或不匹配", code=code, status=status
        )
    if code in {
        "unknown_list",
        "unknown_meta",
        "unknown_action",
        "badvalue",
        "paramvalidator-badvalue",
    }:
        return WikiApiError("unsupported", "API 模块或参数不受支持", code=code, status=status)
    return WikiApiError("api", "Wiki API 拒绝了请求，具体原因尚未确认", code=code, status=status)


def _query(data: dict) -> dict:
    query = data.get("query")
    if not isinstance(query, dict):
        raise WikiApiError("response", "Wiki API 响应缺少有效 query 对象")
    return query


@dataclass(frozen=True)
class WikiStats:
    """一次查询的结果，不携带 QQ 用户或回复对象。"""

    statistics: dict[str, int]
    short_pages: int | None
    short_has_more: bool = False
    short_cached: bool = False
    short_error: WikiApiError | None = None


async def _api_request(session: curl_requests.AsyncSession, **params) -> dict:
    """返回 API 响应，保留 continuation；HTTP 成功但 API 报错也算失败。"""
    try:
        response = await session.post(
            DEFAULT_API_URL,
            data={"action": "query", "format": "json", **params},
            timeout=REQUEST_TIMEOUT,
            allow_redirects=False,
        )
        if response.status_code != 200:
            raise WikiApiError(
                "http",
                f"Wiki HTTP 访问异常（{response.status_code}），不等同于账号权限不足",
                status=response.status_code,
            )
    except Timeout:
        raise WikiApiError("timeout", "Wiki 请求超时") from None
    except curl_requests.RequestsError:
        raise WikiApiError("network", "无法连接 Wiki，请检查网络") from None
    try:
        data = response.json()
    except ValueError:
        raise WikiApiError(
            "non_json", "Wiki 返回非 JSON，可能遇到访问验证", status=response.status_code
        ) from None

    if not isinstance(data, dict):
        raise WikiApiError("response", "Wiki API 返回的 JSON 不是对象")
    if "error" in data:
        raise _api_error(data["error"], response.status_code)
    if "errors" in data:
        errors = data["errors"]
        raise _api_error(
            errors[0] if isinstance(errors, list) and errors else None, response.status_code
        )
    # 当前请求没有预期警告；宁可标为不可确认，也不能把忽略了过滤条件的结果当作统计。
    if data.get("warnings"):
        raise WikiApiError(
            "warning", "API 返回警告，无法确认查询条件是否生效", status=response.status_code
        )
    return data


async def fetch_wiki_stats() -> WikiStats:
    """一次命令共用一个会话；基础统计失败终止，短页面失败降级。"""
    # Cookie Jar 贯穿本次统计任务，退出时关闭；指纹模拟不会执行 JS challenge。
    async with curl_requests.AsyncSession(impersonate=CHROME_FINGERPRINT) as session:
        data = await _api_request(session, meta="siteinfo", siprop="statistics")
        statistics = _query(data).get("statistics")
        if not isinstance(statistics, dict) or not statistics:
            raise WikiApiError("response", "Wiki API 缺少有效的基础统计")
        # 缺失或异常字段不能显示成 0，以免把未知数据当作真实统计。
        statistics = {
            key: value
            for key, value in statistics.items()
            if key in {"pages", "edits", "users", "activeusers"}
            and type(value) is int
            and value >= 0
        }
        if not statistics:
            raise WikiApiError("response", "Wiki API 基础统计没有有效字段")

        try:
            short_data = await _api_request(
                session,
                list="querypage",
                qppage="Shortpages",
                qplimit=SHORT_PAGE_LIMIT,
            )
            short = _query(short_data).get("querypage")
            if not isinstance(short, dict) or not isinstance(short.get("results"), list):
                raise WikiApiError("response", "Wiki API 短页面列表结构无效")
            results = short["results"]
            if any(
                not isinstance(page, dict) or not isinstance(page.get("title"), str)
                for page in results
            ):
                raise WikiApiError("response", "Wiki API 短页面列表条目无效")
        except WikiApiError as error:
            return WikiStats(statistics, None, short_error=error)
        return WikiStats(
            statistics,
            len(results),
            "continue" in short_data or "query-continue" in short_data,
            "cached" in short,
        )


def format_wiki_stats(stats: WikiStats) -> str:
    """展示 API 的实际口径；Shortpages 列表不能当作自定义阈值的全站统计。"""
    if stats.short_pages is None:
        short_text = f"不可用：{stats.short_error or '原因尚未确认'}"
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

    async def handle(
        self, params: str, user_id: str | None = None, group_openid: str | None = None, **kwargs
    ) -> Reply:
        if params and params.strip():
            return Reply(text="请使用 /wiki统计；数据源由维护者在源码顶部配置。")

        try:
            stats = await fetch_wiki_stats()
        except WikiApiError as error:
            logger.warning(
                "Wiki 查询失败 kind=%s code=%s status=%s", error.kind, error.code, error.status
            )
            return Reply(text=f"❌ 无法获取 Wiki 统计：{error}")

        keyboard = None
        # 当前框架可能取不到群用户 ID；此时省略按钮，避免生成 [None] 权限列表。
        if user_id:
            keyboard = make_keyboard(
                [
                    make_button_row(
                        [
                            make_command_button(
                                button_id="wiki_stats_refresh",
                                label="🔄 刷新",
                                command="/wiki统计",
                                style=1,
                                permission_user_ids=[user_id],
                            )
                        ]
                    )
                ]
            )
        return Reply(markdown=format_wiki_stats(stats), keyboard=keyboard)

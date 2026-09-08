# -*- coding:UTF-8 -*- #
# Created at 2026/9/6, Project "hiklqqbot"
# Creator: Unauthorized HOPE

"""Wiki 统计：顶部配置、只读查询、结果格式化和 QQ 命令适配。"""

import asyncio
import logging
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from time import monotonic
from typing import Literal

from curl_cffi import requests as curl_requests
from curl_cffi.requests.exceptions import Timeout
from dotenv import dotenv_values

from plugins.base_plugin import BasePlugin
from reply import Reply
from ui_builder import make_button_row, make_command_button, make_keyboard

# 小范围自用，直接在此修改配置；聊天命令不接受任意网络地址。
DEFAULT_API_URL = "https://casualtiesunknown.huijiwiki.com/api.php"
WIKI_NAME = "未知伤亡"
# 使用指纹自带的浏览器头，避免手写 User-Agent 与 TLS 指纹版本不一致。
CHROME_FINGERPRINT = "chrome131"
REQUEST_TIMEOUT = 15  # 单请求超时秒数，必须大于 0；不自动重试。
TASK_TIMEOUT = 60  # 整次网络任务秒数，必须大于 0；耗尽后保留已取得的数据。
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
class WikiIdentity:
    """仅描述本次会话；不把 userinfo 查询失败推断成匿名。"""

    state: Literal["anonymous", "authenticated", "unknown"] = "unknown"
    name: str | None = field(default=None, repr=False)
    rights: frozenset[str] | None = None


@dataclass(frozen=True)
class WikiStats:
    """一次查询的结果，不携带 QQ 用户或回复对象。"""

    statistics: dict[str, int]
    short_pages: int | None
    short_has_more: bool = False
    short_cached: bool = False
    short_error: WikiApiError | None = None
    identity: WikiIdentity = WikiIdentity()
    auth_error: WikiApiError | None = None


async def _api_request(
    session: curl_requests.AsyncSession, *, deadline: float | None = None, **params
) -> dict:
    """返回 API 响应，保留 continuation；HTTP 成功但 API 报错也算失败。"""
    remaining = REQUEST_TIMEOUT if deadline is None else deadline - monotonic()
    if remaining <= 0:
        raise WikiApiError("timeout", "本次 Wiki 查询时间预算已耗尽")
    timeout = min(REQUEST_TIMEOUT, remaining)
    try:
        response = await asyncio.wait_for(
            session.post(
                DEFAULT_API_URL,
                data={"action": "query", "format": "json", **params},
                timeout=timeout,
                allow_redirects=False,
            ),
            timeout=timeout,
        )
        if response.status_code != 200:
            raise WikiApiError(
                "http",
                f"Wiki HTTP 访问异常（{response.status_code}），不等同于账号权限不足",
                status=response.status_code,
            )
    except (Timeout, asyncio.TimeoutError):
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


def _credentials() -> tuple[str, str]:
    # 与现有 .env 约定一致，系统环境变量优先；单独调用业务函数也无需导入 QQ 配置。
    values = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
    return (
        os.environ.get("WIKI_USERNAME", values.get("WIKI_USERNAME") or ""),
        os.environ.get("WIKI_PASSWORD", values.get("WIKI_PASSWORD") or ""),
    )


def _identity(query: dict) -> WikiIdentity:
    user = query.get("userinfo")
    if not isinstance(user, dict) or type(user.get("id")) is not int:
        return WikiIdentity()
    rights = user.get("rights")
    known_rights = (
        frozenset(rights)
        if isinstance(rights, list) and all(isinstance(right, str) for right in rights)
        else None
    )
    if user["id"] == 0 and "anon" in user:
        # 匿名 name 是 IP，不保存在业务对象中。
        return WikiIdentity("anonymous", rights=known_rights)
    name = user.get("name")
    if user["id"] > 0 and "anon" not in user and isinstance(name, str) and name:
        return WikiIdentity("authenticated", name, known_rights)
    return WikiIdentity()


async def _login(
    session: curl_requests.AsyncSession, username: str, password: str, deadline: float
) -> str:
    """最多四次请求；不处理验证码、2FA、外部跳转，也不重试 NeedToken。"""
    data = await _api_request(session, deadline=deadline, meta="tokens", type="login")
    tokens = _query(data).get("tokens")
    token = tokens.get("logintoken") if isinstance(tokens, dict) else None
    if not isinstance(token, str) or not token:
        raise WikiApiError("response", "Wiki 未返回有效的登录 token")
    data = await _api_request(
        session,
        deadline=deadline,
        action="login",
        lgname=username,
        lgpassword=password,
        lgtoken=token,
    )
    login = data.get("login")
    if not isinstance(login, dict):
        raise WikiApiError("response", "Wiki 登录响应结构无效")
    name = login.get("lgusername")
    if login.get("result") == "Success":
        if isinstance(name, str) and name:
            return name
        raise WikiApiError("response", "Wiki 登录响应缺少规范用户名")
    if not (
        login.get("result") == "Aborted" and "clientlogin" in str(login.get("reason", "")).lower()
    ):
        raise WikiApiError("authentication", "Wiki 登录被拒绝，请检查凭据、账号状态或站点登录限制")

    return_url = DEFAULT_API_URL.rsplit("/", 1)[0] + "/"
    data = await _api_request(
        session,
        deadline=deadline,
        action="clientlogin",
        username=username,
        password=password,
        logintoken=token,
        loginreturnurl=return_url,
    )
    client = data.get("clientlogin")
    if not isinstance(client, dict):
        raise WikiApiError("response", "Wiki clientlogin 响应结构无效")
    if client.get("status") == "UI":
        requests = client.get("requests")
        # 同时包含其他认证要求时不能替用户自动继续，例如 skipReset 与二次验证并存。
        if not (
            isinstance(requests, list)
            and len(requests) == 1
            and isinstance(requests[0], dict)
            and str(requests[0].get("id", "")).endswith(":skipReset")
        ):
            raise WikiApiError("interaction", "Wiki 登录需要人工验证，请先在站点处理")
        data = await _api_request(
            session,
            deadline=deadline,
            action="clientlogin",
            password=password,
            logintoken=token,
            loginreturnurl=return_url,
            logincontinue="1",
            loginpreservestate="1",
            skipReset="1",
        )
        client = data.get("clientlogin")
        if not isinstance(client, dict):
            raise WikiApiError("response", "Wiki clientlogin 响应结构无效")
    if client.get("status") in {"UI", "REDIRECT", "RESTART"}:
        raise WikiApiError("interaction", "Wiki 登录需要人工验证，请先在站点处理")
    if client.get("status") != "PASS":
        raise WikiApiError("authentication", "Wiki 登录被拒绝，请检查凭据、账号状态或站点登录限制")
    name = client.get("username")
    if not isinstance(name, str) or not name:
        raise WikiApiError("response", "Wiki 登录响应缺少规范用户名")
    return name


async def fetch_wiki_stats() -> WikiStats:
    """一次命令共用一个会话；基础统计失败终止，短页面失败降级。"""
    if any(
        type(value) not in {int, float} or not 0 < value < float("inf")
        for value in (REQUEST_TIMEOUT, TASK_TIMEOUT)
    ):
        raise WikiApiError("configuration", "请求和任务超时必须是大于 0 的有限秒数")
    username, password = _credentials()
    deadline = monotonic() + TASK_TIMEOUT
    auth_error = None
    guards = {}
    # Cookie Jar 贯穿登录和本次查询；不在初始化时联网，不跨命令保存状态。
    async with curl_requests.AsyncSession(impersonate=CHROME_FINGERPRINT) as session:
        if username or password:
            try:
                if not username or not password:
                    raise WikiApiError("configuration", "Wiki 用户名与密码必须同时配置")
                name = await _login(session, username, password, deadline)
                guards = {"assert": "user", "assertuser": name}
            except WikiApiError as error:
                auth_error = error
        data = await _api_request(
            session,
            deadline=deadline,
            meta="siteinfo|userinfo",
            siprop="statistics",
            uiprop="rights",
            **guards,
        )
        query = _query(data)
        identity = _identity(query)
        if guards and (identity.state != "authenticated" or identity.name != guards["assertuser"]):
            raise WikiApiError("authentication", "无法确认 Wiki 登录身份，已停止本次查询")
        statistics = query.get("statistics")
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

        stats = WikiStats(statistics, None, identity=identity, auth_error=auth_error)
        try:
            short_data = await _api_request(
                session,
                deadline=deadline,
                list="querypage",
                qppage="Shortpages",
                qplimit=SHORT_PAGE_LIMIT,
                **guards,
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
            return replace(stats, short_error=error)
        return replace(
            stats,
            short_pages=len(results),
            short_has_more="continue" in short_data or "query-continue" in short_data,
            short_cached="cached" in short,
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
    identity_text = {"anonymous": "匿名", "authenticated": "已认证", "unknown": "未知"}[
        stats.identity.state
    ]
    login_text = (
        f"\n> Wiki 登录未完成：{stats.auth_error}；本次仅尝试公开统计。" if stats.auth_error else ""
    )
    return (
        f"# 📊 {WIKI_NAME} Wiki 统计信息\n\n"
        f"• **Wiki 身份**：{identity_text}\n"
        f"• **总页面数**：{base.get('pages', '无法获取')}\n"
        f"• **总编辑数**：{base.get('edits', '无法获取')}\n"
        f"• **注册用户**：{base.get('users', '无法获取')}\n"
        f"• **活跃用户**：{base.get('activeusers', '无法获取')}\n"
        # TODO: 确认目标站巡查机制和权限；FlaggedRevs 审核不等于 MediaWiki 巡查。
        f"• **待巡查页面**：暂不可用（统计口径待确认）\n"
        f"• **短页面列表**：{short_text}\n\n"
        f"> 短页面按站点 Shortpages 列表展示，不代表全站短页面总数。\n"
        f"{login_text}\n"
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

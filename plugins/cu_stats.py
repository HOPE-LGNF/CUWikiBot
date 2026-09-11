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
# 巡查只取首批：整数 1–500。调小可减少返回数据量；调大不会自动分页。
# 存在 continuation 时，两项巡查数量都只代表已读部分，不是整个保留窗口的总数。
PATROL_LIMIT = 500
# None 表示所有当前身份可见的命名空间；非空整数元组指定范围，如 (0,) 仅主空间。
# 值须为目标 Wiki 的非负命名空间编号；可用 API siteinfo/namespaces 手工核对。
# 该配置只影响巡查，不改变基础统计或 Shortpages。未知编号会由 API 校验并明确降级。
PATROL_NAMESPACES: tuple[int, ...] | None = None

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
class PatrolStats:
    """RecentChanges 同一首批中的更改条数及其中的新建记录条数。"""

    changes: int
    new_pages: int
    has_more: bool


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
    patrol: PatrolStats | None = None
    patrol_error: WikiApiError | None = None
    patrol_namespaces: tuple[int, ...] | None = None


def _is_login_advisory(warnings: object) -> bool:
    """仅接受实测的旧式登录弃用提示；仍由 _login 检查实际登录结果。"""
    known = {
        "main": (
            "Subscribe to the mediawiki-api-announce mailing list at "
            "<https://lists.wikimedia.org/postorius/lists/mediawiki-api-announce.lists.wikimedia.org/> "
            "for notice of API deprecations and breaking changes."
        ),
        "login": (
            'Main-account login via "action=login" is deprecated and may stop working without '
            'warning. To continue login with "action=login", see [[Special:BotPasswords]]. '
            'To safely continue using main-account login, see "action=clientlogin".'
        ),
    }
    return (
        isinstance(warnings, dict)
        and bool(warnings)
        and all(
            module in known and warning == {"*": known[module]}
            for module, warning in warnings.items()
        )
    )


async def _api_request(
    session: curl_requests.AsyncSession,
    *,
    deadline: float | None = None,
    allow_userinfo_warning: bool = False,
    **params,
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
    # 合并查询的 userinfo 警告只使身份未知；其他警告可能改变统计语义，不能忽略。
    warnings = data.get("warnings")
    if warnings and not (
        (allow_userinfo_warning and isinstance(warnings, dict) and set(warnings) == {"userinfo"})
        or (params.get("action") == "login" and _is_login_advisory(warnings))
    ):
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
    if client.get("status") in ("UI", "REDIRECT", "RESTART"):
        raise WikiApiError("interaction", "Wiki 登录需要人工验证，请先在站点处理")
    if client.get("status") != "PASS":
        raise WikiApiError("authentication", "Wiki 登录被拒绝，请检查凭据、账号状态或站点登录限制")
    name = client.get("username")
    if not isinstance(name, str) or not name:
        raise WikiApiError("response", "Wiki 登录响应缺少规范用户名")
    return name


def _patrol_params() -> dict:
    if type(PATROL_LIMIT) is not int or not 1 <= PATROL_LIMIT <= 500:
        raise WikiApiError("configuration", "PATROL_LIMIT 必须是 1–500 的整数；本插件不自动分页")
    params = {
        "list": "recentchanges",
        "rcshow": "!patrolled",
        "rctype": "edit|new",
        # title 同时返回 ns，下面需要用它校验命名空间范围。
        "rcprop": "ids|flags|title",
        "rclimit": PATROL_LIMIT,
    }
    if PATROL_NAMESPACES is not None:
        if (
            not isinstance(PATROL_NAMESPACES, tuple)
            or not PATROL_NAMESPACES
            or any(type(ns) is not int or ns < 0 for ns in PATROL_NAMESPACES)
        ):
            raise WikiApiError(
                "configuration", "PATROL_NAMESPACES 必须为 None 或非空的非负整数元组"
            )
        params["rcnamespace"] = "|".join(str(ns) for ns in PATROL_NAMESPACES)
    return params


async def _fetch_patrol(
    session: curl_requests.AsyncSession, stats: WikiStats, deadline: float, guards: dict
) -> PatrolStats:
    params = _patrol_params()
    if stats.auth_error is not None:
        raise WikiApiError("authentication", "Wiki 登录未完成，本次未查询巡查数据")
    if stats.short_error is not None and stats.short_error.kind == "authentication":
        raise WikiApiError("authentication", "Wiki 登录身份已失效，本次未继续查询巡查数据")
    identity = stats.identity
    if identity.state == "unknown" or identity.rights is None:
        raise WikiApiError("unknown", "无法确认本次身份或巡查权限")
    if not identity.rights.intersection({"patrol", "patrolmarks"}):
        label = "匿名身份" if identity.state == "anonymous" else "Wiki 账号"
        raise WikiApiError("permission", f"当前{label}缺少 patrol 或 patrolmarks 权限")
    data = await _api_request(session, deadline=deadline, **params, **guards)
    rows = _query(data).get("recentchanges")
    if not isinstance(rows, list) or len(rows) > PATROL_LIMIT:
        raise WikiApiError("response", "Wiki API 巡查列表结构或条数无效")
    seen = set()
    new_pages = 0
    for row in rows:
        if (
            not isinstance(row, dict)
            or row.get("type") not in ("edit", "new")
            or type(row.get("rcid")) is not int
            or row["rcid"] <= 0
            or type(row.get("ns")) is not int
            or row["ns"] < 0
        ):
            raise WikiApiError("response", "Wiki API 巡查列表包含无效记录")
        if row["rcid"] in seen or (
            PATROL_NAMESPACES is not None and row["ns"] not in PATROL_NAMESPACES
        ):
            raise WikiApiError("response", "Wiki API 巡查列表有重复记录或命名空间不符合筛选条件")
        seen.add(row["rcid"])
        new_pages += row["type"] == "new"
    return PatrolStats(len(rows), new_pages, "continue" in data or "query-continue" in data)


async def fetch_wiki_stats() -> WikiStats:
    """一次命令共用一个会话；基础统计失败终止，短页面和巡查独立降级。"""
    if any(
        type(value) not in {int, float} or not 0 < value < float("inf")
        for value in (REQUEST_TIMEOUT, TASK_TIMEOUT)
    ):
        raise WikiApiError("configuration", "请求和任务超时必须是大于 0 的有限秒数")
    username, password = _credentials()
    deadline = monotonic() + TASK_TIMEOUT
    auth_error = None
    guards: dict = {}
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
            allow_userinfo_warning=True,
            **guards,
        )
        query = _query(data)
        identity = WikiIdentity() if data.get("warnings") else _identity(query)
        if (
            guards
            and identity.state != "unknown"
            and (identity.state != "authenticated" or identity.name != guards["assertuser"])
        ):
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

        stats = WikiStats(
            statistics,
            None,
            identity=identity,
            auth_error=auth_error,
            patrol_namespaces=PATROL_NAMESPACES,
        )
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
            stats = replace(stats, short_error=error)
        else:
            stats = replace(
                stats,
                short_pages=len(results),
                short_has_more="continue" in short_data or "query-continue" in short_data,
                short_cached="cached" in short,
            )
        try:
            return replace(stats, patrol=await _fetch_patrol(session, stats, deadline, guards))
        except WikiApiError as error:
            return replace(stats, patrol_error=error)


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
    if stats.patrol is None:
        patrol_text = f"不可用：{stats.patrol_error or '原因尚未确认'}"
    else:
        patrol_text = f"已读取 {stats.patrol.changes} 条，其中新建 {stats.patrol.new_pages} 条"
        patrol_text += (
            "（还有后续，本次未继续读取；其中新建也仅限已读部分）"
            if stats.patrol.has_more
            else "（本次列表已读完）"
        )
    scope = (
        "所有可见命名空间"
        if stats.patrol_namespaces is None
        else f"命名空间 {stats.patrol_namespaces}"
    )
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
        f"• **待巡查更改**：{patrol_text}\n"
        f"• **短页面列表**：{short_text}\n\n"
        f"> 短页面按站点 Shortpages 列表展示，不代表全站短页面总数。\n"
        f"> 巡查范围：{scope}；仅当前身份可见的 RecentChanges 保留窗口内编辑／新建记录，不是历史累计。\n"
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

        for stage, error in (
            ("login", stats.auth_error),
            ("shortpages", stats.short_error),
            ("patrol", stats.patrol_error),
        ):
            if error is not None:
                logger.warning(
                    "Wiki 功能降级 stage=%s kind=%s code=%s status=%s",
                    stage,
                    error.kind,
                    error.code,
                    error.status,
                )

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

"""无需 Wiki 或 QQ 凭据的最小回归检查：uv run python -m unittest discover -s tests。"""

import asyncio
import json
import unittest
from time import monotonic
from unittest.mock import AsyncMock, Mock, patch

from plugins import cu_stats


class WikiStatsCheck(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        credentials = patch.object(cu_stats, "_credentials", return_value=("", ""))
        credentials.start()
        self.addCleanup(credentials.stop)

    async def test_query_and_reply(self):
        def response(data, status=200):
            return Mock(status_code=status, json=Mock(return_value=data))

        base = {"query": {"statistics": {"pages": 123, "edits": 0, "users": 3}}}
        short = {
            "query": {"querypage": {"results": [{"title": "测试"}], "cached": ""}},
            "continue": {"continue": "-||", "qpoffset": 1},
        }
        session = AsyncMock()
        session.post.side_effect = [response(base), response(short)]
        with patch.object(cu_stats.curl_requests, "AsyncSession") as factory:
            factory.return_value.__aenter__.return_value = session
            stats = await cu_stats.fetch_wiki_stats()
            factory.assert_called_once_with(impersonate="chrome131")
        self.assertEqual(session.post.await_count, 2)
        self.assertEqual(session.post.call_args_list[0].kwargs["data"]["meta"], "siteinfo|userinfo")
        self.assertEqual(session.post.call_args_list[1].kwargs["data"]["qppage"], "Shortpages")
        self.assertEqual(stats.short_pages, 1)
        self.assertTrue(stats.short_has_more)
        self.assertTrue(stats.short_cached)
        markdown = cu_stats.format_wiki_stats(stats)
        self.assertIn("**总编辑数**：0", markdown)
        self.assertIn("**活跃用户**：无法获取", markdown)
        self.assertIn("列表还有后续", markdown)
        self.assertNotIn("170", markdown)

        plugin = cu_stats.WikiStatsPlugin()
        with patch.object(cu_stats, "fetch_wiki_stats", new_callable=AsyncMock, return_value=stats) as fetch:
            reply = await plugin.handle("", user_id="caller")
            button = reply.keyboard["content"]["rows"][0]["buttons"][0]
            self.assertEqual(button["id"], "wiki_stats_refresh")
            self.assertEqual(button["action"]["permission"]["specify_user_ids"], ["caller"])
            self.assertIsNone((await plugin.handle("")).keyboard)
            fetch.reset_mock()
            rejected = await plugin.handle("http://127.0.0.1/api.php")
            self.assertTrue(rejected.text)
            fetch.assert_not_awaited()

        # 故障不能变成全零统计；次要指标失败仍保留基础数据。
        with patch.object(cu_stats.curl_requests, "AsyncSession") as factory:
            factory.return_value.__aenter__.return_value = session
            session.post.side_effect = [response(base), response({"error": {"code": "unknown_list"}})]
            self.assertIsNone((await cu_stats.fetch_wiki_stats()).short_pages)
            session.post.side_effect = [response({"query": {"statistics": {}}})]
            with self.assertRaises(cu_stats.WikiApiError):
                await cu_stats.fetch_wiki_stats()

        for bad_response, kind in (
            (response({}, 403), "http"),
            (response({"errors": []}), "api"),
            (response([]), "response"),
            (response({"warnings": {"recentchanges": {"*": "ignored"}}}), "warning"),
            (response({"error": {"code": "permissiondenied"}}), "permission"),
            (Mock(status_code=200, json=Mock(side_effect=ValueError)), "non_json"),
        ):
            session.post = AsyncMock(return_value=bad_response)
            with self.assertRaises(cu_stats.WikiApiError) as caught:
                await cu_stats._api_request(session, meta="siteinfo")
            self.assertEqual(caught.exception.kind, kind)
        session.post = AsyncMock(side_effect=cu_stats.curl_requests.RequestsError("secret"))
        with self.assertRaises(cu_stats.WikiApiError) as caught:
            await cu_stats._api_request(session, meta="siteinfo")
        self.assertEqual(caught.exception.kind, "network")
        self.assertNotIn("secret", str(caught.exception))
        with self.assertRaises(cu_stats.WikiApiError):
            cu_stats._query({"query": None})
        # 登录响应不含 query，结构检查必须属于调用方。
        session.post = AsyncMock(return_value=response({"login": {"result": "Success"}}))
        self.assertIn("login", await cu_stats._api_request(session, action="login"))


class WikiLoginCheck(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        credentials = patch.object(cu_stats, "_credentials", return_value=("Bot", "secret-password"))
        credentials.start()
        self.addCleanup(credentials.stop)
        self.session = AsyncMock()
        factory = patch.object(cu_stats.curl_requests, "AsyncSession")
        factory.start().return_value.__aenter__.return_value = self.session
        self.addCleanup(factory.stop)

    def responses(self, *data):
        self.session.post.side_effect = [Mock(status_code=200, json=Mock(return_value=d)) for d in data]

    token = {"query": {"tokens": {"logintoken": "secret-token"}}}
    base = {"query": {"statistics": {"pages": 12}, "userinfo": {"id": 1, "name": "Bot", "rights": ["read"]}}}
    short = {"query": {"querypage": {"results": []}}}

    async def test_login_and_guards(self):
        self.responses(self.token, {"login": {"result": "Success", "lgusername": "Bot"}}, self.base, self.short)
        stats = await cu_stats.fetch_wiki_stats()
        self.assertEqual(stats.identity.state, "authenticated")
        self.assertEqual(self.session.post.await_count, 4)
        for call in self.session.post.call_args_list[2:]:
            self.assertEqual(call.kwargs["data"]["assert"], "user")
            self.assertEqual(call.kwargs["data"]["assertuser"], "Bot")
        self.assertNotIn("Bot", repr(stats.identity))

    async def test_fallback_and_bounded_interaction(self):
        fallback = {"login": {"result": "Aborted", "reason": "use clientlogin"}}
        reset = {"clientlogin": {"status": "UI", "requests": [{"id": "MediaWiki:skipReset"}]}}
        self.responses(self.token, fallback, reset, {"clientlogin": {"status": "PASS", "username": "Bot"}}, self.base, self.short)
        self.assertIsNone((await cu_stats.fetch_wiki_stats()).auth_error)
        self.assertEqual(self.session.post.await_count, 6)
        self.assertEqual(self.session.post.call_args_list[3].kwargs["data"]["skipReset"], "1")

        for requests in ([], [{"id": "MediaWiki:skipReset"}, {"id": "2FA"}]):
            self.responses(self.token, fallback, {"clientlogin": {"status": "UI", "requests": requests}})
            with self.assertRaises(cu_stats.WikiApiError) as caught:
                await cu_stats._login(self.session, "Bot", "secret-password", monotonic() + 60)
            self.assertEqual(caught.exception.kind, "interaction")

    async def test_failed_login_keeps_public_stats(self):
        anonymous = {"query": {"statistics": {"pages": 12}, "userinfo": {"id": 0, "anon": "", "name": "192.0.2.1", "rights": ["read"]}}}
        for result in ("Failed", "NeedToken"):
            self.responses(self.token, {"login": {"result": result, "reason": "secret-password secret-token"}}, anonymous, self.short)
            stats = await cu_stats.fetch_wiki_stats()
            self.assertEqual(stats.auth_error.kind, "authentication")
            self.assertEqual(stats.identity.state, "anonymous")
            text = cu_stats.format_wiki_stats(stats)
            self.assertIn("登录未完成", text)
            self.assertNotIn("secret", text)
            self.assertNotIn("192.0.2.1", repr(stats))
            self.assertNotIn("assert", self.session.post.call_args.kwargs["data"])

    async def test_incomplete_config_and_identity_loss(self):
        with patch.object(cu_stats, "_credentials", return_value=("Bot", "")):
            self.responses(self.base, self.short)
            self.assertEqual((await cu_stats.fetch_wiki_stats()).auth_error.kind, "configuration")
            self.assertEqual(self.session.post.await_count, 2)
        self.responses(self.token, {"login": {"result": "Success", "lgusername": "Bot"}}, {"error": {"code": "assertuserfailed"}})
        reply = await cu_stats.WikiStatsPlugin().handle("")
        self.assertIn("身份已失效", reply.text)

    async def test_deadline_stops_requests_and_preserves_base(self):
        with self.assertRaises(cu_stats.WikiApiError):
            await cu_stats._api_request(self.session, deadline=monotonic() - 1)
        self.session.post.assert_not_awaited()
        async def slow_post(*args, **kwargs):
            await asyncio.sleep(1)
        self.session.post.side_effect = slow_post
        with self.assertRaises(cu_stats.WikiApiError) as caught:
            await cu_stats._api_request(self.session, deadline=monotonic() + .02)
        self.assertEqual(caught.exception.kind, "timeout")
        with patch.object(cu_stats, "_credentials", return_value=("", "")):
            self.responses(self.base)
            # 任务开始、基础请求尚有预算、短页面请求开始前耗尽。
            with patch.object(cu_stats, "monotonic", side_effect=[0, 0, 61]):
                stats = await cu_stats.fetch_wiki_stats()
            self.assertEqual(stats.statistics["pages"], 12)
            self.assertEqual(stats.short_error.kind, "timeout")


class WikiCookieCheck(unittest.IsolatedAsyncioTestCase):
    async def test_cookie_jar_covers_login_and_queries(self):
        cookies = []
        payloads = [WikiLoginCheck.token, {"login": {"result": "Success", "lgusername": "Bot"}}, WikiLoginCheck.base, WikiLoginCheck.short]
        async def serve(reader, writer):
            headers = (await reader.readuntil(b"\r\n\r\n")).decode()
            length = next(int(line.split(":", 1)[1]) for line in headers.splitlines() if line.lower().startswith("content-length:"))
            await reader.readexactly(length)
            cookies.append(next((line for line in headers.splitlines() if line.lower().startswith("cookie:")), ""))
            body = json.dumps(payloads[len(cookies) - 1]).encode()
            cookie = "login=started" if len(cookies) == 1 else "identity=confirmed"
            writer.write(f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\nContent-Type: application/json\r\nSet-Cookie: {cookie}; Path=/\r\nConnection: close\r\n\r\n".encode() + body)
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        async with await asyncio.start_server(serve, "127.0.0.1", 0) as server:
            url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/api.php"
            with patch.object(cu_stats, "DEFAULT_API_URL", url), patch.object(cu_stats, "_credentials", return_value=("Bot", "secret")):
                self.assertEqual((await cu_stats.fetch_wiki_stats()).identity.state, "authenticated")
        self.assertEqual(len(cookies), 4)
        self.assertIn("login=started", cookies[1])
        self.assertTrue(all("identity=confirmed" in cookie for cookie in cookies[2:]))


class WikiPatrolCheck(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for target, value in (("_credentials", ("", "")), ("PATROL_LIMIT", 500), ("PATROL_NAMESPACES", None)):
            patcher = patch.object(cu_stats, target, return_value=value) if target == "_credentials" else patch.object(cu_stats, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.session = AsyncMock()
        factory = patch.object(cu_stats.curl_requests, "AsyncSession")
        factory.start().return_value.__aenter__.return_value = self.session
        self.addCleanup(factory.stop)

    base = {"query": {"statistics": {"pages": 12}, "userinfo": {"id": 0, "anon": "", "rights": ["read", "patrolmarks"]}}}
    rows = [{"type": "edit", "ns": 0, "rcid": 1}, {"type": "new", "ns": 0, "rcid": 2}]

    def responses(self, *data):
        self.session.post.reset_mock()
        self.session.post.side_effect = [Mock(status_code=200, json=Mock(return_value=d)) for d in data]

    async def test_counts_scope_and_continuation(self):
        for rows, continuation in (([], {}), (self.rows, {"continue": {"rccontinue": "next"}}), (self.rows, {"query-continue": {"recentchanges": {"rcstart": "next"}}})):
            with self.subTest(rows=rows, continuation=continuation), patch.object(cu_stats, "PATROL_NAMESPACES", (0,)), patch.object(cu_stats, "PATROL_LIMIT", 10):
                self.responses(self.base, WikiLoginCheck.short, {"query": {"recentchanges": rows}, **continuation})
                stats = await cu_stats.fetch_wiki_stats()
                self.assertEqual(stats.patrol.changes, len(rows))
                self.assertEqual(stats.patrol.new_pages, len(rows) // 2)
                self.assertEqual(stats.patrol.has_more, bool(continuation))
                self.assertEqual(self.session.post.await_count, 3)
                params = self.session.post.call_args.kwargs["data"]
                self.assertEqual((params["rcshow"], params["rctype"], params["rcnamespace"], params["rclimit"]), ("!patrolled", "edit|new", "0", 10))
                text = cu_stats.format_wiki_stats(stats)
                self.assertIn("命名空间 (0,)", text)
                self.assertIn("不是历史累计", text)
                self.assertEqual("其中新建也仅限已读部分" in text, bool(continuation))

    async def test_capabilities_and_optional_userinfo_warning(self):
        for user, kind in (({"id": 0, "anon": "", "rights": ["read"]}, "permission"), ({"id": 1, "name": "Bot", "rights": []}, "permission"), ({"id": 1, "name": "Bot"}, "unknown"), ({}, "unknown")):
            self.responses({"query": {"statistics": {"pages": 12}, "userinfo": user}}, WikiLoginCheck.short)
            stats = await cu_stats.fetch_wiki_stats()
            self.assertEqual(stats.patrol_error.kind, kind)
            self.assertEqual(self.session.post.await_count, 2)
        self.responses({**self.base, "warnings": {"userinfo": {"*": "unavailable"}}}, WikiLoginCheck.short)
        stats = await cu_stats.fetch_wiki_stats()
        self.assertEqual(stats.identity.state, "unknown")
        self.assertEqual(stats.statistics["pages"], 12)
        self.assertEqual(stats.patrol_error.kind, "unknown")

    async def test_independent_failures_and_invalid_rows(self):
        self.responses(self.base, {"error": {"code": "unknown_list"}}, {"query": {"recentchanges": self.rows}})
        stats = await cu_stats.fetch_wiki_stats()
        self.assertEqual(stats.short_error.kind, "unsupported")
        self.assertEqual(stats.patrol.changes, 2)
        for data, kind in (({"error": {"code": "permissiondenied"}}, "permission"), ({"query": {"recentchanges": []}, "warnings": {"recentchanges": {"*": "ignored filter"}}}, "warning"), ({"query": {}}, "response"), ({"query": {"recentchanges": self.rows * 2}}, "response"), ({"query": {"recentchanges": [{"type": [], "rcid": 1, "ns": 0}]}}, "response")):
            self.responses(self.base, WikiLoginCheck.short, data)
            stats = await cu_stats.fetch_wiki_stats()
            self.assertEqual(stats.short_pages, 0)
            self.assertIsNone(stats.patrol)
            self.assertEqual(stats.patrol_error.kind, kind)

    async def test_invalid_configuration_and_namespace_mismatch(self):
        for name, value in (("PATROL_LIMIT", 0), ("PATROL_LIMIT", 501), ("PATROL_LIMIT", True), ("PATROL_NAMESPACES", ()), ("PATROL_NAMESPACES", [-1]), ("PATROL_NAMESPACES", (False,))):
            with patch.object(cu_stats, name, value):
                self.responses(self.base, WikiLoginCheck.short)
                stats = await cu_stats.fetch_wiki_stats()
                self.assertEqual(stats.patrol_error.kind, "configuration")
                self.assertEqual(self.session.post.await_count, 2)
        with patch.object(cu_stats, "PATROL_NAMESPACES", (10,)):
            self.responses(self.base, WikiLoginCheck.short, {"query": {"recentchanges": self.rows}})
            self.assertEqual((await cu_stats.fetch_wiki_stats()).patrol_error.kind, "response")

    async def test_full_authenticated_budget_and_lost_session(self):
        base = {"query": {"statistics": {"pages": 12}, "userinfo": {"id": 1, "name": "Bot", "rights": ["patrol"]}}}
        auth = [WikiLoginCheck.token, {"login": {"result": "Aborted", "reason": "use clientlogin"}}, {"clientlogin": {"status": "UI", "requests": [{"id": "MediaWiki:skipReset"}]}}, {"clientlogin": {"status": "PASS", "username": "Bot"}}]
        with patch.object(cu_stats, "_credentials", return_value=("Bot", "secret")):
            self.responses(*auth, base, WikiLoginCheck.short, {"query": {"recentchanges": self.rows}})
            stats = await cu_stats.fetch_wiki_stats()
            self.assertEqual(self.session.post.await_count, 7)
            self.assertEqual(stats.patrol.changes, 2)
            self.assertEqual(self.session.post.call_args.kwargs["data"]["assertuser"], "Bot")
            self.responses(*auth, base, {"error": {"code": "assertuserfailed"}})
            stats = await cu_stats.fetch_wiki_stats()
            self.assertEqual(stats.patrol_error.kind, "authentication")
            self.assertEqual(self.session.post.await_count, 6)
            self.responses(*auth, {"query": {"statistics": {"pages": 12}}}, WikiLoginCheck.short)
            stats = await cu_stats.fetch_wiki_stats()
            self.assertEqual(stats.statistics["pages"], 12)
            self.assertEqual(stats.identity.state, "unknown")
            self.assertEqual(stats.patrol_error.kind, "unknown")
        self.responses(self.base, WikiLoginCheck.short)
        with patch.object(cu_stats, "monotonic", side_effect=[0, 0, 0, 61]):
            stats = await cu_stats.fetch_wiki_stats()
        self.assertEqual(stats.short_pages, 0)
        self.assertEqual(stats.patrol_error.kind, "timeout")
        self.assertEqual(self.session.post.await_count, 2)


if __name__ == "__main__":
    unittest.main()

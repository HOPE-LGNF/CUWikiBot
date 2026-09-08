"""无需 Wiki 或 QQ 凭据的最小回归检查：uv run python -m unittest discover -s tests。"""

import unittest
from unittest.mock import AsyncMock, Mock, patch

from plugins import cu_stats


class WikiStatsCheck(unittest.IsolatedAsyncioTestCase):
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
        self.assertEqual(session.post.call_args_list[0].kwargs["data"]["meta"], "siteinfo")
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
            self.assertIsNone(await cu_stats.fetch_wiki_stats())

        for bad_response in (
            response({}, 403), response({"errors": []}), response([]),
            response({"query": None}), Mock(status_code=200, json=Mock(side_effect=ValueError)),
        ):
            session.post = AsyncMock(return_value=bad_response)
            self.assertIsNone(await cu_stats._api_request(session, meta="siteinfo"))
        session.post = AsyncMock(side_effect=cu_stats.curl_requests.RequestsError("timeout"))
        self.assertIsNone(await cu_stats._api_request(session, meta="siteinfo"))


if __name__ == "__main__":
    unittest.main()

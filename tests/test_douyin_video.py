import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from parsers.douyin import DouyinParser, ParseError, _video_candidates


class DouyinVideoTests(unittest.IsolatedAsyncioTestCase):
    def parser(self, data):
        response = SimpleNamespace(status_code=200, json=lambda: data)
        http = SimpleNamespace(get=AsyncMock(return_value=response))
        return DouyinParser(http, SimpleNamespace(debug=lambda _: None))

    async def test_failed_share_address_uses_matching_feed_item(self):
        parser = self.parser({"aweme_list": [
            {"aweme_id": "other", "video": {"play_addr": {"url_list": ["https://cdn/wrong"]}}},
            {"aweme_id": "123", "video": {"play_addr_h264": {
                "url_list": ["https://cdn/bad", "https://cdn/good?sign=original"]}}},
        ]})
        parser._is_video_url = AsyncMock(side_effect=lambda u: "good" in u)
        result = await parser._resolve_video_url(
            {"play_addr": {"url_list": ["https://old/aweme/v1/playwm/?video_id=x"]}}, "123"
        )
        self.assertEqual(result, "https://cdn/good?sign=original")
        self.assertNotIn("https://cdn/wrong", [c.args[0] for c in parser._is_video_url.call_args_list])

    async def test_working_page_address_skips_feed(self):
        parser = self.parser({})
        parser._is_video_url = AsyncMock(return_value=True)
        self.assertEqual(await parser._resolve_video_url(
            {"play_addr": {"url_list": ["https://cdn/video"]}}, "123"), "https://cdn/video")
        parser.http.get.assert_not_awaited()

    async def test_no_matching_item_fails_without_sending_recommendation(self):
        parser = self.parser({"aweme_list": [{"aweme_id": "wrong", "video": {
            "play_addr": {"url_list": ["https://cdn/wrong"]}}}]})
        parser._is_video_url = AsyncMock(return_value=False)
        with self.assertRaises(ParseError):
            await parser._resolve_video_url({}, "123")
        parser._is_video_url.assert_not_awaited()

    def test_preserves_signed_cdn_and_deduplicates(self):
        address = {"url_list": ["https://cdn/playwm/video?sign=playwm"]}
        self.assertEqual(list(_video_candidates({"play_addr_h264": address, "play_addr": address})),
                         address["url_list"])

    async def test_probe_rejects_html_and_accepts_mp4(self):
        class Response:
            status_code = 206
            body = b"<html>not a video</html>"

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def aiter_bytes(self, chunk_size):
                yield self.body

        parser = self.parser({})
        response = Response()
        parser.http.stream = lambda *a, **k: response
        self.assertFalse(await parser._is_video_url("https://cdn/video"))
        response.body = b"\x00\x00\x00\x20ftypisom"
        self.assertTrue(await parser._is_video_url("https://cdn/video"))

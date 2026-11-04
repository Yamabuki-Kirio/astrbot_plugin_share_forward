import unittest

from parsers.bilibili import BilibiliParser
from parsers.douyin import DouyinParser
from parsers.patterns import SUPPORTED_SHARE_URL
from parsers.xiaohongshu import XiaohongshuParser


class _Logger:
    def debug(self, _message):
        pass

    def info(self, _message):
        pass


class BilibiliUrlMatchingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.parser = BilibiliParser(None, _Logger(), {})

    async def test_accepts_video_and_short_links(self):
        for url in (
            "https://www.bilibili.com/video/BV1GJ411x7h7",
            "https://m.bilibili.com/video/BV1GJ411x7h7?p=2",
            "https://bilibili.com/video/av170001",
            "https://b23.tv/hj9Stu2",
            "看看这个 https://www.bilibili.com/video/BV1GJ411x7h7 怎么样",
        ):
            self.assertTrue(await self.parser.can_parse(url), url)

    async def test_rejects_pages_that_are_not_videos(self):
        for url in (
            "https://live.bilibili.com/123",
            "https://space.bilibili.com/456",
            "https://www.bilibili.com/read/cv123",
            "https://www.bilibili.com/bangumi/play/ep123",
            "https://t.bilibili.com/789",
        ):
            self.assertFalse(await self.parser.can_parse(url), url)


class DouyinUrlMatchingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.parser = DouyinParser(None, _Logger(), {})

    async def test_accepts_work_links(self):
        for url in (
            "https://v.douyin.com/iRNBho6/",
            "https://www.iesdouyin.com/share/video/7300000000000000000/",
            "https://www.douyin.com/video/7300000000000000000",
            "https://www.douyin.com/note/7300000000000000000",
            "https://www.douyin.com/discover?modal_id=7300000000000000000",
        ):
            self.assertTrue(await self.parser.can_parse(url), url)

    async def test_rejects_profile_links(self):
        for url in (
            "https://www.douyin.com/user/MS4wLjABAAAA",
            "https://www.douyin.com/",
        ):
            self.assertFalse(await self.parser.can_parse(url), url)


class XiaohongshuUrlMatchingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.parser = XiaohongshuParser(None, _Logger(), {})

    async def test_accepts_short_and_note_links(self):
        for url in (
            "https://xhslink.com/a1b2c3",
            "https://www.xiaohongshu.com/explore/66abc123",
            "https://www.xiaohongshu.com/discovery/item/66abc123?xsec_token=ABC",
        ):
            self.assertTrue(await self.parser.can_parse(url), url)

    async def test_rejects_profile_links(self):
        self.assertFalse(await self.parser.can_parse("https://www.xiaohongshu.com/user/profile/abc"))


class SupportedShareUrlTests(unittest.TestCase):
    def test_does_not_match_lookalike_hosts(self):
        self.assertIsNone(SUPPORTED_SHARE_URL.search("https://notxhslink.com/a1b2c3"))
        self.assertIsNone(SUPPORTED_SHARE_URL.search("https://live.bilibili.com/123"))

    def test_matches_supported_platforms(self):
        self.assertIsNotNone(SUPPORTED_SHARE_URL.search("https://b23.tv/hj9Stu2"))
        self.assertIsNotNone(SUPPORTED_SHARE_URL.search("https://v.douyin.com/iRNBho6/"))
        self.assertIsNotNone(SUPPORTED_SHARE_URL.search("https://xhslink.com/a1b2c3"))


if __name__ == "__main__":
    unittest.main()

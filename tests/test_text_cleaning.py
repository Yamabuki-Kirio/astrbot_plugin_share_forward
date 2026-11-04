import unittest

from parsers.base import clean_text
from parsers.bilibili import BilibiliParser


class _Logger:
    def debug(self, _message):
        pass

    def info(self, _message):
        pass


class _BilibiliStub(BilibiliParser):
    """只返回 view 数据；画质探测直接给空结果，避免网络依赖。"""

    def __init__(self, desc):
        super().__init__(None, _Logger(), {"include_top_comments": False})
        self._view = {
            "code": 0,
            "data": {
                "bvid": "BV1GJ411x7h7",
                "aid": 1,
                "cid": 2,
                "title": "标题",
                "desc": desc,
                "owner": {"name": "UP主"},
                "pic": "",
                "duration": 10,
                "stat": {"view": 1},
            },
        }

    async def _get_json(self, _url, *, params, api_name):
        return self._view

    async def _fetch_play_url(self, _info):
        return "", "", None


class CleanTextTests(unittest.TestCase):
    def test_placeholders_become_empty(self):
        for value in ("-", "--", " — ", "—", "――", "暂无", "暂无简介", "", "   "):
            self.assertEqual(clean_text(value), "", repr(value))

    def test_real_text_is_stripped_but_kept(self):
        self.assertEqual(clean_text("  正常简介  "), "正常简介")

    def test_non_string_values_are_empty(self):
        self.assertEqual(clean_text(None), "")
        self.assertEqual(clean_text(123), "")


class BilibiliDescCleaningTests(unittest.IsolatedAsyncioTestCase):
    async def test_placeholder_desc_is_stored_as_empty(self):
        item = await _BilibiliStub("-").parse(
            "https://www.bilibili.com/video/BV1GJ411x7h7"
        )

        self.assertEqual(item.desc, "")
        self.assertEqual(item.title, "标题")

    async def test_real_desc_is_kept(self):
        item = await _BilibiliStub("正常简介").parse(
            "https://www.bilibili.com/video/BV1GJ411x7h7"
        )

        self.assertEqual(item.desc, "正常简介")


if __name__ == "__main__":
    unittest.main()

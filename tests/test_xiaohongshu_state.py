import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from parsers.xiaohongshu import XiaohongshuParser, _load_initial_state, ParseError


class InitialStateTests(unittest.TestCase):
    def test_undefined_values_with_whitespace_and_in_arrays(self):
        self.assertEqual(
            _load_initial_state('{"a":undefined,"b": undefined,"c":[undefined,\nundefined,{"d":\tundefined}]}'),
            {"a": None, "b": None, "c": [None, None, {"d": None}]},
        )

    def test_string_content_and_escapes_are_preserved(self):
        data = {"undefined": '正文 :undefined, undefined, "引号" \\ 路径',
                "url": "https://example.com/undefined?value=:undefined"}
        self.assertEqual(_load_initial_state(json.dumps(data)), data)

    def test_other_javascript_is_rejected(self):
        with self.assertRaises(json.JSONDecodeError):
            _load_initial_state('{"a":someFunction()}')

    def test_serialized_constructors(self):
        self.assertEqual(_load_initial_state(
            '{"date":new Date("2026-11-16T00:00:00.000Z"),'
            '"map":new Map([["a",undefined],["b",new Set([1,2])]]),'
            '"empty":new Set(),"timestamp":new Date(123)}'),
            {"date": "2026-11-16T00:00:00.000Z", "map": {"a": None, "b": [1, 2]},
             "empty": [], "timestamp": 123})

    def test_constructor_text_in_strings_is_unchanged(self):
        data = {"desc": 'new Date("example") and new Map([]) :undefined'}
        self.assertEqual(_load_initial_state(json.dumps(data)), data)

    def test_executable_and_unknown_constructors_are_rejected(self):
        for raw in ('{"a":new Function("return 1")}',
                    '{"a":new Date(run())}', '{"a":new Map("wrong")}',
                    '{"a":new Date(2026,11,16)}'):
            with self.subTest(raw=raw), self.assertRaises(json.JSONDecodeError):
                _load_initial_state(raw)


class XiaohongshuStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_parse_page_with_optional_semicolon(self):
        for terminator in ("", ";"):
            with self.subTest(terminator=terminator):
                html = ('<script>window.__INITIAL_STATE__={"unused":[undefined],'
                        '"date":new Date("2026-11-16T00:00:00.000Z"),'
                        '"cache":new Map(),"note":{"noteDetailMap":{"abc":{"note":'
                        '{"title":"正文 :undefined","desc":"描述","type":"normal",'
                        '"imageList":[{"urlDefault":"https://cdn/image"}]}}}}}'
                        + terminator + '</script>')
                parser = self.parser(html)
                item = await parser.parse("https://www.xiaohongshu.com/explore/abc")
                self.assertEqual(item.title, "正文 :undefined")
                self.assertEqual(item.images, ["https://cdn/image"])

    async def test_malformed_state_keeps_parse_error(self):
        parser = self.parser('<script>window.__INITIAL_STATE__={"a":broken}</script>')
        with self.assertRaisesRegex(ParseError, r"INITIAL_STATE 解析失败 xhs-state-v5 token=identifier unknown length=6 first=U\+0062"):
            await parser.parse("https://www.xiaohongshu.com/explore/abc")

    async def test_known_invalid_token_is_identified(self):
        parser = self.parser('<script>window.__INITIAL_STATE__={"a":void 0}</script>')
        with self.assertRaisesRegex(ParseError, r"token=identifier value=void"):
            await parser.parse("https://www.xiaohongshu.com/explore/abc")

    def parser(self, html):
        http = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(text=html)))
        return XiaohongshuParser(http, SimpleNamespace(debug=lambda _: None),
                                {"xiaohongshu_cookie": "a1=test"})

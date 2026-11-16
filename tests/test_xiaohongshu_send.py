import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from parsers.base import ParsedItem, ParseError, clean_text


def load_methods():
    path = Path(__file__).parents[1] / "main.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    plugin = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ShareForwardPlugin")
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_desc_text"]
    nodes += [n for n in plugin.body if isinstance(n, ast.AsyncFunctionDef) and n.name in {"_dispatch_send", "_send_xiaohongshu_cover_text"}]
    ns = {"ParsedItem": ParsedItem, "ParseError": ParseError, "clean_text": clean_text,
          "AstrMessageEvent": object, "MessageChain": list,
          "Comp": SimpleNamespace(Image=SimpleNamespace(fromURL=lambda url: ("image", url)),
                                  Plain=lambda text: ("text", text))}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), ns)
    return ns


NS = load_methods()


class XiaohongshuSendTests(unittest.IsolatedAsyncioTestCase):
    async def send(self, **values):
        event = SimpleNamespace(send=AsyncMock())
        item = ParsedItem(platform="xiaohongshu", raw_url="https://example.com", **values)
        await NS["_send_xiaohongshu_cover_text"](object(), event, item)
        return event.send.call_args.args[0]

    async def test_cover_and_complete_body_in_one_message(self):
        parts = await self.send(cover="cover", images=["first", "second"], title="标题", desc="正文\n#话题")
        self.assertEqual(parts, [("image", "cover"), ("text", "标题\n\n正文\n#话题")])

    async def test_first_image_fallback_and_duplicate_title(self):
        self.assertEqual(await self.send(images=["first", "second"], title="正文", desc="正文"),
                         [("image", "first"), ("text", "正文")])

    async def test_missing_cover_still_sends_body(self):
        self.assertEqual(await self.send(desc="正文"), [("text", "正文")])

    async def test_missing_text_still_sends_cover(self):
        self.assertEqual(await self.send(cover="cover"), [("image", "cover")])

    async def test_route_and_opt_out(self):
        for platform, config, expected in (("xiaohongshu", {}, "direct"),
                                           ("xiaohongshu", {"xiaohongshu_cover_text": False}, "forward"),
                                           ("bilibili", {}, "forward")):
            output = []
            async def forward(*args):
                yield "forward"
            plugin = SimpleNamespace(config=config, _send_xiaohongshu_cover_text=AsyncMock(),
                                     _send_forward=forward)
            event = SimpleNamespace(get_platform_name=lambda: "aiocqhttp")
            item = ParsedItem(platform=platform, raw_url="url")
            async for value in NS["_dispatch_send"](plugin, event, item):
                output.append(value)
            self.assertEqual(output, [] if expected == "direct" else ["forward"])
            self.assertEqual(plugin._send_xiaohongshu_cover_text.await_count, int(expected == "direct"))

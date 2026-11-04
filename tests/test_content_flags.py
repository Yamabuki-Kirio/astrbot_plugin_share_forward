import ast
import unittest
from pathlib import Path
from types import SimpleNamespace

from parsers.base import clean_text


MAIN = Path(__file__).parents[1] / "main.py"


def _load_content_flags():
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    plugin = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ShareForwardPlugin"
    )
    method = next(
        node for node in plugin.body
        if isinstance(node, ast.FunctionDef) and node.name == "_content_flags"
    )
    desc_fn = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_desc_text"
    )
    namespace = {"ParsedItem": object, "clean_text": clean_text}
    exec(
        compile(ast.Module(body=[desc_fn, method], type_ignores=[]), str(MAIN), "exec"),
        namespace,
    )
    return namespace["_content_flags"]


content_flags = _load_content_flags()


class _Plugin:
    def __init__(self, **content):
        self.config = {"forward_content": content}


def _item(desc="", title="标题", platform="bilibili"):
    return SimpleNamespace(platform=platform, desc=desc, title=title)


class ContentFlagTests(unittest.TestCase):
    def test_default_keeps_both_nodes(self):
        plugin = _Plugin(include_text_desc=True, include_cover=True)

        self.assertEqual(content_flags(plugin, _item("简介")), (True, True))

    def test_exclusive_prefers_desc_when_present(self):
        plugin = _Plugin(
            include_text_desc=True, include_cover=True, exclusive_desc_cover=True
        )

        self.assertEqual(content_flags(plugin, _item("简介")), (True, False))

    def test_exclusive_falls_back_to_cover_without_desc(self):
        plugin = _Plugin(
            include_text_desc=True, include_cover=True, exclusive_desc_cover=True
        )

        self.assertEqual(content_flags(plugin, _item("")), (False, True))

    def test_exclusive_treats_placeholder_desc_as_empty(self):
        plugin = _Plugin(
            include_text_desc=True, include_cover=True, exclusive_desc_cover=True
        )

        self.assertEqual(content_flags(plugin, _item("-")), (False, True))

    def test_desc_equal_to_title_does_not_block_cover(self):
        # 抖音纯文字动态 title 就是 desc，此时不会发简介节点，应当继续发封面。
        plugin = _Plugin(
            include_text_desc=True, include_cover=True, exclusive_desc_cover=True
        )

        self.assertEqual(content_flags(plugin, _item("同一段文字", title="同一段文字")), (False, True))

    def test_exclusive_only_applies_when_both_enabled(self):
        plugin = _Plugin(
            include_text_desc=True, include_cover=False, exclusive_desc_cover=True
        )

        self.assertEqual(content_flags(plugin, _item("简介")), (True, False))


if __name__ == "__main__":
    unittest.main()

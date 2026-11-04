import ast
import unittest
from pathlib import Path
from types import SimpleNamespace

from parsers.base import clean_text


MAIN = Path(__file__).parents[1] / "main.py"


def _desc_text(item):
    return clean_text(getattr(item, "desc", ""))


def _load_formatter():
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    plugin = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ShareForwardPlugin"
    )
    method = next(
        node for node in plugin.body
        if isinstance(node, ast.FunctionDef) and node.name == "_format_desc_block"
    )
    ast.fix_missing_locations(method)
    namespace = {"ParsedItem": object, "_desc_text": _desc_text}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(MAIN), "exec"), namespace)
    return namespace["_format_desc_block"]


format_desc = _load_formatter()


class DescriptionFormatTests(unittest.TestCase):
    def test_bilibili_description_is_labeled_as_intro(self):
        item = SimpleNamespace(platform="bilibili", desc="视频简介内容")
        self.assertEqual(format_desc(item), "📝 简介：视频简介内容")

    def test_other_platform_text_is_labeled_as_body(self):
        item = SimpleNamespace(platform="xiaohongshu", desc="笔记正文")
        self.assertEqual(format_desc(item), "📝 正文：笔记正文")


if __name__ == "__main__":
    unittest.main()

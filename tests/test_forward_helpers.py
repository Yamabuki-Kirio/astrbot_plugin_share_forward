import ast
import unittest
from pathlib import Path
from types import SimpleNamespace


MAIN = Path(__file__).parents[1] / "main.py"


class _Node:
    def __init__(self, uin="", name="", content=None):
        self.uin = uin
        self.name = name
        self.content = list(content or [])

    @property
    def text(self):
        return "\n".join(getattr(part, "text", "") for part in self.content)


class _Plain:
    def __init__(self, text=""):
        self.text = text


def _load_namespace():
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    plugin = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ShareForwardPlugin"
    )
    wanted_methods = {"_format_title_block", "_format_comments_block"}
    module_helpers = {
        "_platform_tag",
        "_make_node",
        "_append_plain_node",
        "_append_section",
        "_add_text_section",
    }
    nodes = [
        node
        for node in tree.body
        if (
            isinstance(node, ast.FunctionDef)
            and node.name in module_helpers
        )
        or (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "_PLATFORM_TAGS"
                for target in node.targets
            )
        )
    ]
    nodes.extend(
        node
        for node in plugin.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted_methods
    )
    namespace = {
        "Comp": SimpleNamespace(Node=_Node, Plain=_Plain),
        "ParsedItem": object,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(MAIN), "exec"), namespace)
    return namespace


NS = _load_namespace()


def _item(**kwargs):
    base = {"platform": "douyin", "title": "", "author": "", "comments": []}
    base.update(kwargs)
    return SimpleNamespace(**base)


class TitleBlockTests(unittest.TestCase):
    def test_title_block_starts_with_platform_tag(self):
        text = NS["_format_title_block"](None, _item(platform="bilibili", title="标题", author="UP主"))
        self.assertEqual(text, "[哔哩哔哩] 📖 标题：标题\n👤 作者：UP主")

    def test_unknown_platform_falls_back_to_its_name(self):
        self.assertEqual(NS["_platform_tag"]("weibo"), "[weibo]")


class CommentsBlockTests(unittest.TestCase):
    def test_medals_are_assigned_by_output_order(self):
        item = _item(
            comments=[
                {"content": "   "},
                {"content": "第一条"},
                {"content": "第二条"},
                {"content": "第三条"},
                {"content": "第四条"},
            ]
        )

        lines = NS["_format_comments_block"](None, item).splitlines()

        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("🥇"))
        self.assertTrue(lines[1].startswith("🥈"))
        self.assertTrue(lines[2].startswith("🥉"))

    def test_all_empty_comments_produce_empty_text(self):
        item = _item(comments=[{"content": ""}, {"content": "  "}])

        self.assertEqual(NS["_format_comments_block"](None, item), "")


class SectionHelperTests(unittest.TestCase):
    def test_empty_body_does_not_emit_section_header(self):
        nodes = []

        added = NS["_add_text_section"](nodes, "10000", "分享解析", "💬 热门评论", "")

        self.assertFalse(added)
        self.assertEqual(nodes, [])

    def test_body_emits_header_then_text(self):
        nodes = []

        added = NS["_add_text_section"](nodes, "10000", "分享解析", "💬 热门评论", "🥇 很棒")

        self.assertTrue(added)
        self.assertEqual(len(nodes), 2)
        self.assertEqual(nodes[0].text, "【💬 热门评论】")
        self.assertEqual(nodes[1].text, "🥇 很棒")


if __name__ == "__main__":
    unittest.main()

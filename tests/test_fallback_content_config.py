import ast
import unittest
from pathlib import Path


MAIN = Path(__file__).parents[1] / "main.py"


class FallbackContentConfigTests(unittest.TestCase):
    def test_title_author_output_is_guarded_by_its_config_switch(self):
        tree = ast.parse(MAIN.read_text(encoding="utf-8"))
        plugin = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "ShareForwardPlugin"
        )
        fallback = next(
            node for node in plugin.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_send_fallback"
        )
        source = ast.unparse(fallback)

        self.assertIn("if fc.get('include_title_author', True):", source)
        title_pos = source.index("yield event.plain_result(self._format_title_block(item))")
        guard_pos = source.index("if fc.get('include_title_author', True):")
        self.assertLess(guard_pos, title_pos)


if __name__ == "__main__":
    unittest.main()

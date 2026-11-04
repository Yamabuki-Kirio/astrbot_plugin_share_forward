import ast
import re
import unittest
from pathlib import Path


MAIN = Path(__file__).parents[1] / "main.py"


def _load_extractor():
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    wanted = {"_RE_SUPPORTED_SHARE_URL", "_CARD_URL_KEYS"}
    nodes = [
        node
        for node in tree.body
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id in wanted for target in node.targets)
        )
        or (isinstance(node, ast.FunctionDef) and node.name == "_extract_url_from_json_payload")
    ]
    namespace = {"re": re, "json": __import__("json"), "Any": object}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(MAIN), "exec"), namespace)
    return namespace["_extract_url_from_json_payload"]


extract_url = _load_extractor()


class JsonCardUrlExtractionTests(unittest.TestCase):
    def test_qq_bilibili_card_prefers_qqdocurl_over_icon_and_preview(self):
        card = {
            "meta": {
                "detail_1": {
                    "icon": "https://open.gtimg.cn/open/app_icon/00/95/17/76/icon.png",
                    "preview": "https://qq.ugcimg.cn/v1/preview-image",
                    "url": "m.q.qq.com/a/s/016065fd8133afc2e94db47f47b256e0",
                    "qqdocurl": "https://b23.tv/hj9Stu2",
                }
            }
        }

        self.assertEqual(extract_url(card), "https://b23.tv/hj9Stu2")

    def test_serialized_card_has_the_same_priority(self):
        payload = (
            '{"meta":{"detail_1":{"icon":"https://open.gtimg.cn/icon.png",'
            '"qqdocurl":"https://b23.tv/hj9Stu2"}}}'
        )

        self.assertEqual(extract_url(payload), "https://b23.tv/hj9Stu2")


if __name__ == "__main__":
    unittest.main()

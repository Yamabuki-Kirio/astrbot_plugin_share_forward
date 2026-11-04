import ast
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from uuid import uuid4


MAIN = Path(__file__).parents[1] / "main.py"


class _Logger:
    def warning(self, _message):
        pass


def _safe_unlink(path):
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def _load_downloader():
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    plugin = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ShareForwardPlugin"
    )
    method = next(
        node for node in plugin.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_download_video_to_path"
    )
    namespace = {
        "ParsedItem": object,
        "Optional": Optional,
        "logger": _Logger(),
        "_safe_unlink": _safe_unlink,
        "_DOWNLOAD_CHUNK_SIZE": 1024 * 1024,
    }
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(MAIN), "exec"), namespace)
    return namespace["_download_video_to_path"]


download_to_path = _load_downloader()


class _Response:
    def __init__(self, chunks, status_code=200, headers=None, error=None):
        self._chunks = chunks
        self.status_code = status_code
        self.headers = headers or {}
        self._error = error
        self.requested_chunk_sizes = []

    async def aiter_bytes(self, size):
        self.requested_chunk_sizes.append(size)
        for index, chunk in enumerate(self._chunks):
            if self._error is not None and index == 1:
                raise self._error
            yield chunk


class _Stream:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Client:
    def __init__(self, response):
        self._response = response

    def stream(self, _method, _url, **_kwargs):
        return _Stream(self._response)


class _Plugin:
    def __init__(self, client):
        self._http = client
        self.config = {"video_max_size_mb": 1}

    def _dlog(self, _message):
        pass


def _item():
    return SimpleNamespace(
        platform="douyin",
        video_url="https://example.com/v.mp4",
        video_referer="",
    )


class DownloadCleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # 直接写在 tests 目录下：某些受限环境里新建子目录后无法再写入。
        self.path = str(Path(__file__).parent / f"_tmp_download_{uuid4().hex}.mp4")

    def tearDown(self):
        _safe_unlink(self.path)

    async def test_successful_download_keeps_file(self):
        response = _Response([b"a" * 10, b"b" * 10])
        client = _Client(response)
        plugin = _Plugin(client)

        result = await download_to_path(plugin, _item(), self.path, 1024)

        self.assertEqual(result, self.path)
        self.assertTrue(os.path.exists(self.path))
        with open(self.path, "rb") as fp:
            self.assertEqual(fp.read(), b"a" * 10 + b"b" * 10)

    async def test_download_reads_in_one_mib_chunks(self):
        response = _Response([b"x"])
        client = _Client(response)
        plugin = _Plugin(client)

        await download_to_path(plugin, _item(), self.path, 10 * 1024 * 1024)

        self.assertEqual(response.requested_chunk_sizes, [1024 * 1024])

    async def test_interrupted_download_removes_partial_file(self):
        client = _Client(_Response([b"a" * 10, b"b" * 10], error=RuntimeError("boom")))
        plugin = _Plugin(client)

        result = await download_to_path(plugin, _item(), self.path, 1024)

        self.assertIsNone(result)
        self.assertFalse(os.path.exists(self.path))

    async def test_oversized_download_removes_partial_file(self):
        # 没有 content-length，只能边下边判断：超限后必须把写了一半的文件删掉。
        client = _Client(_Response([b"a" * 800, b"b" * 800]))
        plugin = _Plugin(client)

        result = await download_to_path(plugin, _item(), self.path, 1000)

        self.assertIsNone(result)
        self.assertFalse(os.path.exists(self.path))

    async def test_content_length_over_limit_does_not_create_file(self):
        client = _Client(_Response([], headers={"content-length": "99999"}))
        plugin = _Plugin(client)

        result = await download_to_path(plugin, _item(), self.path, 1000)

        self.assertIsNone(result)
        self.assertFalse(os.path.exists(self.path))


if __name__ == "__main__":
    unittest.main()

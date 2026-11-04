import unittest

from parsers.bilibili import BilibiliParser, ParseError


class _Logger:
    def debug(self, _message):
        pass


class _Response:
    def __init__(self, status_code=200, data=None, json_error=None, headers=None):
        self.status_code = status_code
        self._data = data
        self._json_error = json_error
        self.headers = headers or {"content-type": "application/json"}

    def json(self):
        if self._json_error:
            raise self._json_error
        return self._data


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class BilibiliRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_bootstraps_and_retries_non_json_response(self):
        client = _Client([
            _Response(),
            _Response(json_error=ValueError("empty"), headers={"content-type": "text/html"}),
            _Response(),
            _Response(data={"code": 0, "data": {"bvid": "BV1KenZ6ZEAL"}}),
        ])
        parser = BilibiliParser(
            client,
            _Logger(),
            {"bilibili_cookie": "SESSDATA=test\n"},
        )

        result = await parser._get_json(
            "https://api.bilibili.com/x/web-interface/view",
            params={"bvid": "BV1KenZ6ZEAL"},
            api_name="view",
        )

        self.assertEqual(result["code"], 0)
        self.assertEqual([call[0] for call in client.calls], [
            "https://www.bilibili.com/",
            "https://api.bilibili.com/x/web-interface/view",
            "https://www.bilibili.com/",
            "https://api.bilibili.com/x/web-interface/view",
        ])
        api_headers = client.calls[-1][1]["headers"]
        self.assertEqual(api_headers["Origin"], "https://www.bilibili.com")
        self.assertEqual(api_headers["Referer"], "https://www.bilibili.com/")
        self.assertEqual(api_headers["Cookie"], "SESSDATA=test")

    async def test_reports_http_status_instead_of_json_decode_error(self):
        client = _Client([_Response(), _Response(status_code=403)])
        parser = BilibiliParser(client, _Logger())

        with self.assertRaisesRegex(ParseError, "HTTP 403"):
            await parser._get_json(
                "https://api.bilibili.com/x/web-interface/view",
                params={"bvid": "BV1KenZ6ZEAL"},
                api_name="view",
            )


if __name__ == "__main__":
    unittest.main()

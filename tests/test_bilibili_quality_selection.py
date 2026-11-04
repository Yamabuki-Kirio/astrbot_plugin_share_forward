import unittest

from parsers.bilibili import BilibiliParser


class _Logger:
    def debug(self, _message):
        pass

    def info(self, _message):
        pass


class _Parser(BilibiliParser):
    def __init__(self, replies, max_mb):
        super().__init__(None, _Logger(), {"video_max_size_mb": max_mb})
        self.replies = iter(replies)
        self.requested = []

    async def _get_json(self, _url, *, params, api_name):
        self.requested.append(params["qn"])
        return next(self.replies)


def _play(quality, size, url, accept_quality=None):
    data = {
        "quality": quality,
        "durl": [{"url": url, "size": size}],
    }
    if accept_quality is not None:
        data["accept_quality"] = accept_quality
    return {"code": 0, "data": data}


class BilibiliQualitySelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_selects_clearest_candidate_within_limit(self):
        parser = _Parser(
            [
                _play(120, 150 * 1024 * 1024, "4k"),
                _play(116, 110 * 1024 * 1024, "1080p60"),
                _play(80, 70 * 1024 * 1024, "1080p"),
            ],
            max_mb=80,
        )

        url, quality, size = await parser._fetch_play_url(
            {"bvid": "BV1KenZ6ZEAL", "cid": 123}
        )

        self.assertEqual((url, quality), ("1080p", "1080P"))
        self.assertEqual(size, 70 * 1024 * 1024)
        self.assertEqual(parser.requested, [120, 116, 112])

    async def test_stops_after_the_first_fitting_quality(self):
        parser = _Parser([_play(80, 70 * 1024 * 1024, "1080p")], max_mb=80)

        url, quality, _size = await parser._fetch_play_url(
            {"bvid": "BV1KenZ6ZEAL", "cid": 123}
        )

        self.assertEqual((url, quality), ("1080p", "1080P"))
        self.assertEqual(parser.requested, [120])

    async def test_accept_quality_avoids_requesting_unavailable_qualities(self):
        accept = [80, 64, 32, 16]
        parser = _Parser(
            [
                _play(80, 150 * 1024 * 1024, "1080p", accept_quality=accept),
                _play(64, 90 * 1024 * 1024, "720p", accept_quality=accept),
                _play(32, 40 * 1024 * 1024, "480p", accept_quality=accept),
            ],
            max_mb=80,
        )

        url, quality, size = await parser._fetch_play_url(
            {"bvid": "BV1KenZ6ZEAL", "cid": 123}
        )

        self.assertEqual((url, quality), ("480p", "480P"))
        self.assertEqual(size, 40 * 1024 * 1024)
        # 旧实现会依次尝试 120/116/112/80…；这里只补请求真正存在的低档位。
        self.assertEqual(parser.requested, [120, 64, 32])

    async def test_does_not_return_video_when_every_quality_is_too_large(self):
        oversized = [_play(q, 2 * 1024 * 1024, str(q)) for q in (120, 116, 112, 80, 74, 64, 32, 16)]
        parser = _Parser(oversized, max_mb=1)

        result = await parser._fetch_play_url({"bvid": "BV1KenZ6ZEAL", "cid": 123})

        self.assertEqual(result, ("", "", None))


if __name__ == "__main__":
    unittest.main()

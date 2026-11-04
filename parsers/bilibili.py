"""B 站解析器：b23.tv / bilibili.com
完全匿名可调，最稳。
"""
from __future__ import annotations

import re
from typing import Optional

from .base import BaseParser, ParsedItem, ParseError


_RE_BILIBILI = re.compile(
    r"(?:https?://)?(?:"
    r"b23\.tv/[A-Za-z0-9]+|"
    r"(?:www\.|m\.)?bilibili\.com/video/[A-Za-z0-9]+|"
    r"(?:www\.|m\.)?bilibili\.com/[a-zA-Z0-9?=&_/.\-]+"
    r")",
    re.IGNORECASE,
)
_RE_BV = re.compile(r"(BV[0-9A-Za-z]{10})")
_RE_AV = re.compile(r"av(\d+)", re.IGNORECASE)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Origin": "https://www.bilibili.com",
    "Referer": "https://www.bilibili.com/",
}


class BilibiliParser(BaseParser):
    platform = "bilibili"

    def __init__(self, http_client, logger, config: Optional[dict] = None):
        super().__init__(http_client, logger, config)
        self._bootstrapped = False

    def _request_headers(self) -> dict[str, str]:
        headers = dict(_HEADERS)
        cookie = str(self.config.get("bilibili_cookie") or "").strip()
        if cookie:
            headers["Cookie"] = cookie.replace("\r", "").replace("\n", "")
        return headers

    async def _bootstrap_session(self, force: bool = False) -> None:
        """访问主站，让客户端取得匿名风控 Cookie。"""
        if self._bootstrapped and not force:
            return
        try:
            response = await self.http.get(
                "https://www.bilibili.com/",
                headers=self._request_headers(),
                follow_redirects=True,
            )
            if response.status_code >= 400:
                self.logger.debug(
                    f"[bilibili] 主站 Cookie 初始化失败: HTTP {response.status_code}"
                )
        except Exception as exc:  # noqa: BLE001
            self.logger.debug(f"[bilibili] 主站 Cookie 初始化失败: {exc}")
        self._bootstrapped = True

    async def _get_json(self, url: str, *, params: dict, api_name: str) -> dict:
        await self._bootstrap_session()
        for attempt in range(2):
            try:
                response = await self.http.get(
                    url,
                    params=params,
                    headers=self._request_headers(),
                )
            except Exception as exc:  # noqa: BLE001
                raise ParseError(f"调用 {api_name} 接口失败: {exc}") from exc

            if response.status_code == 412 and attempt == 0:
                await self._bootstrap_session(force=True)
                continue
            if response.status_code != 200:
                raise ParseError(
                    f"调用 {api_name} 接口失败: HTTP {response.status_code}"
                )
            try:
                data = response.json()
            except Exception as exc:  # noqa: BLE001
                if attempt == 0:
                    await self._bootstrap_session(force=True)
                    continue
                content_type = response.headers.get("content-type", "未知")
                raise ParseError(
                    f"调用 {api_name} 接口失败: 返回了非 JSON 内容"
                    f"（HTTP 200，Content-Type: {content_type}）"
                ) from exc
            if data.get("code") == -412 and attempt == 0:
                await self._bootstrap_session(force=True)
                continue
            return data
        raise ParseError(f"调用 {api_name} 接口失败: Bilibili 风控拦截")

    async def can_parse(self, url: str) -> bool:
        return bool(_RE_BILIBILI.search(url))

    async def parse(self, url: str) -> ParsedItem:
        m = _RE_BILIBILI.search(url)
        if not m:
            raise ParseError("not bilibili")
        page_url = m.group(0)
        if not page_url.startswith("http"):
            page_url = "https://" + page_url

        # 1. 短链跟随
        bv = _extract_bv(page_url)
        aid: Optional[int] = None
        if not bv:
            # b23.tv 等需要跟随
            try:
                resp = await self.http.get(
                    page_url,
                    headers=self._request_headers(),
                    follow_redirects=True,
                )
                final_url = str(resp.url)
            except Exception as e:  # noqa: BLE001
                raise ParseError(f"短链跳转失败: {e}") from e
            bv = _extract_bv(final_url)
            if not bv:
                m_av = _RE_AV.search(final_url)
                if m_av:
                    aid = int(m_av.group(1))

        if not bv and not aid:
            raise ParseError("未能从链接中提取 BV/AV 号")

        # 2. 视频详情
        params = {"bvid": bv} if bv else {"aid": aid}
        data = await self._get_json(
            "https://api.bilibili.com/x/web-interface/view",
            params=params,
            api_name="view",
        )

        if data.get("code") != 0:
            raise ParseError(f"view 接口返回错误: {data.get('message')}")
        info = data["data"]

        item = ParsedItem(
            platform="bilibili",
            raw_url=url,
            item_type="video",
            title=info.get("title", ""),
            author=(info.get("owner") or {}).get("name", ""),
            desc=info.get("desc", ""),
            cover=info.get("pic", ""),
            canonical_url=f"https://www.bilibili.com/video/{info.get('bvid', bv)}",
            extra={
                "bvid": info.get("bvid", bv or ""),
                "aid": str(info.get("aid", aid or "")),
                "duration": str(info.get("duration", "")),
                "view": str((info.get("stat") or {}).get("view", "")),
            },
        )

        # 3. 原视频流地址。优先 durl（通常音视频合一），失败再兜底 dash.video。
        try:
            video_url, quality_name, video_size = await self._fetch_play_url(info)
            if video_url:
                item.video_url = video_url
                item.video_referer = item.canonical_url or "https://www.bilibili.com/"
                item.extra["video_quality"] = quality_name
                item.extra["video_size"] = str(video_size or "")
                size_text = (
                    f"，预计大小: {video_size / 1024 / 1024:.1f} MB"
                    if video_size else ""
                )
                self.logger.info(
                    f"[share_forward] Bilibili 视频选择画质: {quality_name}{size_text}"
                )
        except Exception as e:  # noqa: BLE001
            self.logger.debug(f"[bilibili] 视频流拉取失败: {e}")

        # 4. 评论 Top 3
        if self.config.get("include_top_comments", True):
            try:
                aid_for_reply = info.get("aid") or aid
                if aid_for_reply:
                    item.comments.extend(await self._fetch_top_comments(aid_for_reply))
            except Exception as e:  # noqa: BLE001
                self.logger.debug(f"[bilibili] 评论拉取失败: {e}")

        return item

    async def _fetch_top_comments(self, aid: int) -> list[dict[str, str]]:
        """拉取 B 站热评。兼容新版 main 接口和旧版 reply 接口。"""
        headers = self._request_headers()

        candidates: list[dict] = []

        # 新版评论主接口：热评可能在 top_replies，也可能在 replies。
        rc = await self.http.get(
            "https://api.bilibili.com/x/v2/reply/main",
            params={
                "type": 1,
                "oid": aid,
                "mode": 3,
                "next": 0,
                "ps": 20,
                "plat": 1,
            },
            headers=headers,
        )
        rj = rc.json()
        if rj.get("code") == 0:
            data = rj.get("data") or {}
            candidates.extend(data.get("top_replies") or [])
            candidates.extend(data.get("replies") or [])
        else:
            self.logger.debug(f"[bilibili] reply/main 返回错误: {rj.get('message')}")

        # 兜底旧接口：有些环境 main 接口为空，但旧接口还能拿到按热度排序的评论。
        if len(candidates) < 3:
            old = await self.http.get(
                "https://api.bilibili.com/x/v2/reply",
                params={
                    "type": 1,
                    "oid": aid,
                    "sort": 2,
                    "pn": 1,
                    "ps": 20,
                },
                headers=headers,
            )
            oj = old.json()
            if oj.get("code") == 0:
                data = oj.get("data") or {}
                candidates.extend(data.get("top_replies") or [])
                candidates.extend(data.get("replies") or [])
            else:
                self.logger.debug(f"[bilibili] reply 返回错误: {oj.get('message')}")

        comments: list[dict[str, str]] = []
        seen: set[str] = set()
        candidates.sort(key=lambda x: int(x.get("like", 0) or 0), reverse=True)
        for rep in candidates:
            content = ((rep.get("content") or {}).get("message") or "").strip()
            if not content or content in seen:
                continue
            seen.add(content)
            comments.append({
                "user": (rep.get("member") or {}).get("uname", ""),
                "content": content,
                "like": str(rep.get("like", 0)),
            })
            if len(comments) >= 3:
                break
        return comments

    async def _fetch_play_url(self, info: dict) -> tuple[str, str, Optional[int]]:
        bvid = info.get("bvid")
        cid = info.get("cid")
        if not bvid or not cid:
            return "", "", None

        # 与现有 bili_resolver 一致：从高到低请求音视频合一 durl，
        # 选择体积限制内的第一个候选，即可发送的最清晰版本。
        quality_names = {
            120: "4K", 116: "1080P60", 112: "1080P+", 80: "1080P",
            74: "720P60", 64: "720P", 32: "480P", 16: "360P",
        }
        max_mb = max(1, int(self.config.get("video_max_size_mb", 80) or 80))
        max_bytes = max_mb * 1024 * 1024
        seen_qualities: set[int] = set()

        for requested_quality in (120, 116, 112, 80, 74, 64, 32, 16):
            params = {
                "bvid": bvid,
                "cid": cid,
                "qn": requested_quality,
                "fnval": 0,
                "fourk": 1,
                "otype": "json",
                "platform": "html5",
                "high_quality": 1,
            }
            data = await self._get_json(
                "https://api.bilibili.com/x/player/playurl",
                params=params,
                api_name="playurl",
            )
            if data.get("code") != 0:
                continue
            play = data.get("data") or {}
            actual_quality = int(play.get("quality") or 0)
            if actual_quality in seen_qualities:
                continue
            seen_qualities.add(actual_quality)
            durl = play.get("durl") or []
            if len(durl) != 1 or not durl[0].get("url"):
                continue
            size = int(durl[0].get("size") or 0) or None
            if size and size > max_bytes:
                continue
            return (
                durl[0]["url"],
                quality_names.get(actual_quality, f"QN{actual_quality}"),
                size,
            )
        return "", "", None


def _extract_bv(url: str) -> Optional[str]:
    m = _RE_BV.search(url)
    return m.group(1) if m else None

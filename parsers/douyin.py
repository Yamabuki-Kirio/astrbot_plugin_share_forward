"""抖音解析器：v.douyin.com / iesdouyin.com / douyin.com
策略：
1. 短链跟随 → 提取 aweme_id
2. 优先抓 https://www.iesdouyin.com/share/video/{aweme_id}/  HTML，
   解析其中的 window._ROUTER_DATA / window._RENDER_DATA，
   覆盖视频、图集、纯文字三种 item_type。
3. 评论：仅当用户提供 cookie 时尝试调 detail / comment 接口（带 cookie 即可，无签名时常返回风控；失败则跳过）。
"""
from __future__ import annotations

import json
import re
from typing import Optional
from urllib.parse import unquote

from .base import BaseParser, ParsedItem, ParseError
from .patterns import DOUYIN_LINK


_RE_AWEME_ID = re.compile(r"(?:video|note)/?(\d{10,25})|modal_id=(\d{10,25})")

_HEADERS_DESKTOP = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.douyin.com/",
}

_HEADERS_MOBILE = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 8.0.0; SM-G955U Build/R16NW) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www.douyin.com/?is_from_mobile_home=1&recommend=1",
}


class DouyinParser(BaseParser):
    platform = "douyin"

    async def can_parse(self, url: str) -> bool:
        return bool(DOUYIN_LINK.search(url))

    async def parse(self, url: str) -> ParsedItem:
        m = DOUYIN_LINK.search(url)
        if not m:
            raise ParseError("not douyin")
        page_url = m.group(0)
        if not page_url.startswith("http"):
            page_url = "https://" + page_url

        # 1. 直接 GET 一次：短链会跳转到 iesdouyin 分享页，响应体里通常已经带着
        #    _ROUTER_DATA，省掉旧版“先 HEAD 再 GET 分享页”的第二次请求。
        try:
            response = await self.http.get(
                page_url, headers=_HEADERS_MOBILE, follow_redirects=True
            )
        except Exception as e:  # noqa: BLE001
            raise ParseError(f"抖音链接访问失败: {e}") from e

        final_url = str(response.url)
        html = response.text if response.status_code == 200 else ""

        aweme_id = _extract_aweme_id(final_url) or _extract_aweme_id(page_url)
        if not aweme_id:
            raise ParseError("未能从链接中提取 aweme_id")

        share_url = f"https://www.iesdouyin.com/share/video/{aweme_id}/"
        item = ParsedItem(
            platform="douyin",
            raw_url=url,
            canonical_url=share_url,
            extra={"aweme_id": aweme_id},
        )

        # 第一优先级：复刻 astrbot_plugin_douyin_bot 的 loaderData 路径。
        detail = _extract_drdon_style_detail(html) or _extract_router_detail(html)
        if not detail:
            detail = await self._fetch_detail_from(share_url)
        if not detail:
            # 兜底：图文作品走 note 路径
            detail = await self._fetch_detail_from(
                f"https://www.iesdouyin.com/share/note/{aweme_id}/"
            )
        if not detail:
            # 兜底：桌面视频页，部分环境才下发 RENDER_DATA
            detail = await self._fetch_detail_from(
                f"https://www.douyin.com/video/{aweme_id}", headers=_HEADERS_DESKTOP
            )

        if not detail:
            # 最后兜底：旧 iteminfo 接口，部分环境仍可匿名返回基础信息。
            try:
                r = await self.http.get(
                    "https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/",
                    params={"item_ids": aweme_id},
                    headers=_HEADERS_MOBILE,
                    follow_redirects=True,
                )
                data = r.json()
                items = data.get("item_list") or []
                if items:
                    detail = items[0]
            except Exception:  # noqa: BLE001
                detail = None

        if not detail:
            raise ParseError("未能在 share 页、视频页或 iteminfo 接口中找到抖音内容数据")

        item.title = detail.get("desc", "") or detail.get("title", "")
        author = detail.get("author") or {}
        item.author = author.get("nickname", "")
        item.desc = detail.get("desc", "")

        # 视频
        video = detail.get("video") or {}
        play_addr = (
            video.get("play_addr_h264") or video.get("play_addr") or {}
        )
        url_list = play_addr.get("url_list") or []
        if url_list:
            # 把 playwm 替换为 play 拿无水印（旧策略，2025 年仍部分有效）
            best = url_list[0].replace("playwm", "play")
            item.video_url = best
            item.video_referer = "https://www.douyin.com/"
        else:
            # 参考 astrbot_plugin_douyin_bot：部分页面只给 play_addr.uri。
            uri = play_addr.get("uri") or video.get("play_addr", {}).get("uri")
            if uri:
                if str(uri).startswith("http"):
                    item.video_url = str(uri)
                else:
                    item.video_url = f"https://www.douyin.com/aweme/v1/play/?video_id={uri}"
                item.video_referer = "https://www.douyin.com/"

        cover_obj = video.get("cover") or video.get("origin_cover") or {}
        cover_list = cover_obj.get("url_list") or []
        if cover_list:
            item.cover = cover_list[0]

        # 图集
        images = detail.get("images") or []
        for img in images:
            if not isinstance(img, dict):
                continue
            ulist = img.get("url_list") or []
            if ulist:
                # 优先 https，并取最后一个（一般是无水印高清）
                item.images.append(ulist[-1])

        # 类型判定
        if item.images:
            item.item_type = "images"
        elif item.video_url:
            item.item_type = "video"
        else:
            item.item_type = "text"

        # 评论：可选（需要 cookie 才有较高成功率，失败静默）
        if self.config.get("include_top_comments", True) and self.config.get("douyin_cookie"):
            try:
                ch = dict(_HEADERS_DESKTOP)
                ch["Cookie"] = self.config["douyin_cookie"]
                rc = await self.http.get(
                    "https://www.douyin.com/aweme/v1/web/comment/list/",
                    params={
                        "device_platform": "webapp",
                        "aid": "6383",
                        "aweme_id": aweme_id,
                        "cursor": 0,
                        "count": 20,
                    },
                    headers=ch,
                )
                cj = rc.json()
                comments = cj.get("comments") or []
                comments.sort(key=lambda x: x.get("digg_count", 0), reverse=True)
                for c in comments[:3]:
                    item.comments.append({
                        "user": (c.get("user") or {}).get("nickname", ""),
                        "content": c.get("text", ""),
                        "like": str(c.get("digg_count", 0)),
                    })
            except Exception as e:  # noqa: BLE001
                self.logger.debug(f"[douyin] 评论拉取失败: {e}")

        return item

    async def _fetch_detail_from(
        self, page_url: str, headers: Optional[dict] = None
    ) -> Optional[dict]:
        """抓一页 HTML 并尝试取出作品详情；失败只会返回 None，不打断兜底链。"""
        try:
            response = await self.http.get(
                page_url, headers=headers or _HEADERS_MOBILE, follow_redirects=True
            )
        except Exception as e:  # noqa: BLE001
            self.logger.debug(f"[douyin] {page_url} 抓取失败: {e}")
            return None
        return _extract_drdon_style_detail(response.text) or _extract_router_detail(
            response.text
        )


def _extract_aweme_id(url: str) -> Optional[str]:
    if not url:
        return None
    m = _RE_AWEME_ID.search(url)
    if m:
        return m.group(1) or m.group(2)
    # modal_id 在 query
    m2 = re.search(r"modal_id=(\d{10,25})", url)
    if m2:
        return m2.group(1)
    # 参考可用插件的宽松提取：跳转 URL 中只要出现 10-25 位数字就尝试作为 aweme_id。
    m3 = re.search(r"(\d{10,25})", url)
    if m3:
        return m3.group(1)
    return None


_RE_ROUTER = re.compile(
    r"<script[^>]*id=\"RENDER_DATA\"[^>]*>([\s\S]*?)</script>",
    re.IGNORECASE,
)
_RE_ROUTER2 = re.compile(
    r"window\._ROUTER_DATA\s*=\s*(\{[\s\S]*?\});", re.IGNORECASE
)


def _extract_router_detail(html: str) -> Optional[dict]:
    """从 share 页 HTML 中找到视频/图集详情对象。"""
    detail = None

    # 新版 RENDER_DATA（urlencoded JSON）
    m = _RE_ROUTER.search(html)
    if m:
        try:
            raw = unquote(m.group(1).strip())
            data = json.loads(raw)
            detail = _walk_for_detail(data)
        except Exception:  # noqa: BLE001
            detail = None

    if detail:
        return detail

    # 旧版 _ROUTER_DATA。优先使用括号配平，避免简单正则被 JSON 内部分号截断。
    json_str = _extract_json_after_flag(html, "window._ROUTER_DATA")
    if not json_str:
        m2 = _RE_ROUTER2.search(html)
        json_str = m2.group(1) if m2 else ""

    if json_str:
        try:
            data = json.loads(json_str.replace("\\u002F", "/").replace("\\/", "/"))
            detail = _walk_for_detail(data)
        except Exception:  # noqa: BLE001
            detail = None
    return detail


def _extract_drdon_style_detail(html: str) -> Optional[dict]:
    """参考 drdon1234/astrbot_plugin_douyin_bot 的解析路径。"""
    json_str = _extract_json_after_flag(html, "window._ROUTER_DATA")
    if not json_str:
        return None

    try:
        json_str = json_str.replace("\\u002F", "/").replace("\\/", "/")
        data = json.loads(json_str)
    except Exception:  # noqa: BLE001
        return None

    loader_data = data.get("loaderData", {})
    if not isinstance(loader_data, dict):
        return None

    for value in loader_data.values():
        if not isinstance(value, dict):
            continue
        video_info = value.get("videoInfoRes")
        if not isinstance(video_info, dict):
            continue
        item_list = video_info.get("item_list") or []
        if item_list:
            return item_list[0]

    return None


def _extract_json_after_flag(text: str, flag: str) -> str:
    """从 `flag = { ... }` 形式的脚本中提取完整 JSON 对象。"""
    start_idx = text.find(flag)
    if start_idx == -1:
        return ""

    brace_start = text.find("{", start_idx)
    if brace_start == -1:
        return ""

    stack = 0
    in_string = False
    quote = ""
    escaped = False

    for idx in range(brace_start, len(text)):
        ch = text[idx]

        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                in_string = False
            continue

        if ch in ("'", '"'):
            in_string = True
            quote = ch
            continue

        if ch == "{":
            stack += 1
        elif ch == "}":
            stack -= 1
            if stack == 0:
                return text[brace_start : idx + 1]

    return ""


def _walk_for_detail(obj):
    """在嵌套结构里寻找含 desc + (video|images) 的字典。"""
    if isinstance(obj, dict):
        # 常见键：aweme_detail / aweme / videoInfoRes -> item_list[0]
        if "aweme_detail" in obj and isinstance(obj["aweme_detail"], dict):
            return obj["aweme_detail"]
        if "videoInfoRes" in obj and isinstance(obj["videoInfoRes"], dict):
            items = obj["videoInfoRes"].get("item_list") or []
            if items:
                return items[0]
        if (
            ("video" in obj or "images" in obj)
            and ("desc" in obj or "author" in obj)
        ):
            return obj
        for v in obj.values():
            r = _walk_for_detail(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _walk_for_detail(v)
            if r:
                return r
    return None

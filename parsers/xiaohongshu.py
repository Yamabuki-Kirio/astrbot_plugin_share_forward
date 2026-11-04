"""小红书解析器：xhslink.com / xiaohongshu.com
策略：
1. 短链跟随，保留 xsec_token
2. 抓 https://www.xiaohongshu.com/explore/{note_id}?xsec_token=... HTML
3. 解析 window.__INITIAL_STATE__，拿到 note 详情
4. 必须带 cookie（至少含 a1），否则 461
"""
from __future__ import annotations

import json
import re
from typing import Optional, Tuple

from .base import BaseParser, ParsedItem, ParseError
from .patterns import XIAOHONGSHU_LINK


_RE_NOTE_ID = re.compile(r"/(?:explore|discovery/item)/([A-Za-z0-9]+)")
_RE_XSEC = re.compile(r"xsec_token=([^&\s]+)")
_RE_INITIAL = re.compile(
    r"window\.__INITIAL_STATE__\s*=\s*(\{[\s\S]*?\})\s*</script>", re.IGNORECASE
)


def _build_headers(cookie: str = "") -> dict:
    h = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://www.xiaohongshu.com/",
    }
    if cookie:
        h["Cookie"] = cookie
    return h


class XiaohongshuParser(BaseParser):
    platform = "xiaohongshu"

    async def can_parse(self, url: str) -> bool:
        return bool(XIAOHONGSHU_LINK.search(url))

    async def parse(self, url: str) -> ParsedItem:
        m = XIAOHONGSHU_LINK.search(url)
        if not m:
            raise ParseError("not xiaohongshu")
        page_url = m.group(0)
        if not page_url.startswith("http"):
            page_url = "https://" + page_url

        cookie = self.config.get("xiaohongshu_cookie", "") or ""
        if not cookie or "a1=" not in cookie:
            raise ParseError(
                "未配置小红书 Cookie（需包含 a1=...），请在插件设置里填写。"
            )

        headers = _build_headers(cookie)

        # 1. 跟随短链
        note_id, xsec = _extract_id_token(page_url)
        if not note_id:
            try:
                r = await self.http.get(page_url, headers=headers, follow_redirects=True)
                final_url = str(r.url)
                note_id, xsec = _extract_id_token(final_url)
            except Exception as e:  # noqa: BLE001
                raise ParseError(f"小红书短链跳转失败: {e}") from e

        if not note_id:
            raise ParseError("未能从链接中提取小红书 note_id")

        explore_url = f"https://www.xiaohongshu.com/explore/{note_id}"
        if xsec:
            explore_url += f"?xsec_token={xsec}&xsec_source=pc_feed"

        # 2. 抓 explore 页
        try:
            r = await self.http.get(explore_url, headers=headers, follow_redirects=True)
            html = r.text
        except Exception as e:  # noqa: BLE001
            raise ParseError(f"小红书页面抓取失败: {e}") from e

        if "您访问的页面不见了" in html or ("登录" in html and "captcha" in html.lower()):
            raise ParseError("小红书页面被风控/要求登录，请刷新 Cookie。")

        m2 = _RE_INITIAL.search(html)
        if not m2:
            raise ParseError("未在页面中找到 __INITIAL_STATE__，可能 Cookie 失效")

        raw = m2.group(1)
        # 小红书的 __INITIAL_STATE__ 里有 undefined，需要替换
        raw_fixed = raw.replace(":undefined", ":null")
        try:
            state = json.loads(raw_fixed)
        except Exception as e:  # noqa: BLE001
            raise ParseError(f"INITIAL_STATE 解析失败: {e}") from e

        note_detail = _find_note_detail(state, note_id)
        if not note_detail:
            raise ParseError("未在状态树中找到笔记详情")

        item = ParsedItem(
            platform="xiaohongshu",
            raw_url=url,
            canonical_url=explore_url,
            title=note_detail.get("title", ""),
            desc=note_detail.get("desc", ""),
            extra={"note_id": note_id},
        )
        user = note_detail.get("user") or {}
        item.author = user.get("nickname", "") or user.get("nick_name", "")

        note_type = note_detail.get("type") or note_detail.get("note_type") or ""
        # 图片列表
        image_list = note_detail.get("image_list") or note_detail.get("imageList") or []
        for img in image_list:
            if not isinstance(img, dict):
                continue
            # 优先 url_default，其次 url_pre / url
            u = (
                img.get("url_default")
                or img.get("urlDefault")
                or img.get("url")
                or img.get("url_pre")
            )
            if not u and img.get("info_list"):
                # 新版可能在 info_list 里
                for info in img["info_list"]:
                    if info.get("url"):
                        u = info["url"]
                        break
            if u:
                item.images.append(u)

        # 视频
        video = note_detail.get("video") or {}
        if video:
            stream = (video.get("media") or {}).get("stream") or {}
            cands = []
            for key in ("h264", "h265", "av1"):
                cands.extend(stream.get(key) or [])
            if cands:
                cands.sort(key=lambda x: x.get("size", 0), reverse=True)
                item.video_url = cands[0].get("master_url") or cands[0].get("backup_urls", [""])[0]
                item.video_referer = "https://www.xiaohongshu.com/"
            cover = (video.get("image") or {}).get("first_frame") or ""
            if cover:
                item.cover = cover

        if not item.cover and item.images:
            item.cover = item.images[0]

        if note_type == "video" or item.video_url:
            item.item_type = "video"
        elif item.images:
            item.item_type = "images"
        else:
            item.item_type = "text"

        # 高赞评论：从 INITIAL_STATE 直接拿
        if self.config.get("include_top_comments", True):
            try:
                comments = _find_comments(state)
                comments.sort(key=lambda x: int(x.get("like_count", 0) or 0), reverse=True)
                for c in comments[:3]:
                    item.comments.append({
                        "user": (c.get("user_info") or {}).get("nickname", ""),
                        "content": c.get("content", ""),
                        "like": str(c.get("like_count", 0)),
                    })
            except Exception as e:  # noqa: BLE001
                self.logger.debug(f"[xhs] 评论解析失败: {e}")

        return item


def _extract_id_token(url: str) -> Tuple[Optional[str], Optional[str]]:
    nid = None
    m = _RE_NOTE_ID.search(url)
    if m:
        nid = m.group(1)
    xsec = None
    m2 = _RE_XSEC.search(url)
    if m2:
        xsec = m2.group(1)
    return nid, xsec


def _find_note_detail(state: dict, note_id: str) -> Optional[dict]:
    """在 __INITIAL_STATE__ 里寻找笔记详情。"""
    note = state.get("note") or {}
    nd_map = note.get("noteDetailMap") or note.get("note_detail_map") or {}
    if isinstance(nd_map, dict):
        if note_id in nd_map and isinstance(nd_map[note_id], dict):
            return nd_map[note_id].get("note") or nd_map[note_id]
        # 任取一个
        for v in nd_map.values():
            if isinstance(v, dict):
                return v.get("note") or v

    # 备用：直接 note.firstNoteId
    first_id = note.get("firstNoteId") or note.get("first_note_id")
    if first_id and nd_map.get(first_id):
        return nd_map[first_id].get("note") or nd_map[first_id]

    return None


def _find_comments(state: dict) -> list:
    """尝试在状态树里找到评论列表。"""
    note = state.get("note") or {}
    cm = note.get("noteCommentMap") or note.get("note_comment_map") or {}
    for v in cm.values() if isinstance(cm, dict) else []:
        if isinstance(v, dict):
            comments = v.get("comments") or v.get("list") or []
            if comments:
                return comments
    # 平铺
    if isinstance(note.get("comments"), list):
        return note["comments"]
    return []

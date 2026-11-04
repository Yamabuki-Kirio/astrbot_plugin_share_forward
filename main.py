"""astrbot_plugin_share_forward
将抖音/B站/小红书等主流平台的分享链接，解析后打包成一条 QQ 合并转发消息。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, List, Optional
from urllib.parse import parse_qs, quote, urlparse

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp

from .parsers import (
    BaseParser,
    BilibiliParser,
    DouyinParser,
    ParseError,
    ParsedItem,
    XiaohongshuParser,
    clean_text,
)
from .parsers.patterns import SUPPORTED_SHARE_URL


_RE_URL = re.compile(
    r"(https?://[^\s\u4e00-\u9fa5\"'<>]+|"
    r"(?:v\.douyin\.com|b23\.tv|xhslink\.com)/[A-Za-z0-9_\-/]+)",
    re.IGNORECASE,
)

_CARD_URL_KEYS = ("qqdocurl", "jumpUrl", "jump_url", "sourceUrl", "source_url", "url")

# 1 MiB 一块：80MB 视频从 1280 次读写降到 80 次，既省 syscall 也少抢事件循环。
_DOWNLOAD_CHUNK_SIZE = 1024 * 1024

_PLATFORM_TAGS = {
    "douyin": "[抖音]",
    "bilibili": "[哔哩哔哩]",
    "xiaohongshu": "[小红书]",
}


def _platform_tag(platform: str) -> str:
    return _PLATFORM_TAGS.get(platform, f"[{platform}]")


def _desc_text(item: ParsedItem) -> str:
    """简介/正文实际可发送的文本；`-`、`暂无简介` 之类的占位符按空处理。"""
    return clean_text(getattr(item, "desc", ""))


def _make_node(uin: str, name: str, content: list):
    return Comp.Node(uin=uin, name=name, content=content)


def _append_plain_node(nodes: list, uin: str, name: str, text: str) -> bool:
    if not text:
        return False
    nodes.append(_make_node(uin, name, [Comp.Plain(text)]))
    return True


def _append_section(nodes: list, uin: str, name: str, title: str) -> None:
    nodes.append(_make_node(uin, name, [Comp.Plain(f"【{title}】")]))


def _add_text_section(nodes: list, uin: str, name: str, title: str, text: str) -> bool:
    """正文非空时才添加分类标题，避免只剩一个“💬 热门评论”的空节点。"""
    if not text:
        return False
    _append_section(nodes, uin, name, title)
    _append_plain_node(nodes, uin, name, text)
    return True


def _safe_unlink(path: str) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


class _ShareForwardVideoHandler(SimpleHTTPRequestHandler):
    """给 NapCat 跨容器拉取临时视频用的轻量 HTTP 文件服务。"""

    def __init__(self, *args, directory: str, token: str = "", **kwargs):
        self._token = token
        super().__init__(*args, directory=directory, **kwargs)

    def _authorized(self) -> bool:
        if not self._token:
            return True
        query = parse_qs(urlparse(self.path).query)
        return (query.get("token") or [""])[0] == self._token

    def do_GET(self):  # noqa: N802
        if not self._authorized():
            self.send_error(403, "Forbidden")
            return
        super().do_GET()

    def do_HEAD(self):  # noqa: N802
        if not self._authorized():
            self.send_error(403, "Forbidden")
            return
        super().do_HEAD()

    def log_message(self, format: str, *args):  # noqa: A002
        return


@register(
    "astrbot_plugin_share_forward",
    "TRAE / Yamabuki-Kirio",
    "把抖音/B站/小红书分享链接解析后打包成 QQ 合并转发",
    "1.1.12",
)
class ShareForwardPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config or {}
        self._http: Optional[httpx.AsyncClient] = None
        self._parsers: List[BaseParser] = []
        self._recent_links: dict[str, float] = {}
        self._file_server: Optional[ThreadingHTTPServer] = None
        self._file_server_thread: Optional[threading.Thread] = None
        # 同一个视频被多个会话同时分享时，只下载一次。
        self._download_locks: dict[str, asyncio.Lock] = {}
        self._warned_bilibili_access = False
        self._build_parsers()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def _build_parsers(self):
        platforms = self.config.get("platforms", {}) or {}
        timeout = int(self.config.get("request_timeout", 15) or 15)
        self._http = self._make_http_client(timeout)

        common_cfg = {
            "include_top_comments": (self.config.get("forward_content", {}) or {}).get(
                "include_top_comments", True
            ),
            "douyin_cookie": self.config.get("douyin_cookie", ""),
            "bilibili_cookie": self.config.get("bilibili_cookie", ""),
            "video_max_size_mb": self.config.get("video_max_size_mb", 80),
            "xiaohongshu_cookie": self.config.get("xiaohongshu_cookie", ""),
        }

        if platforms.get("douyin", True):
            self._parsers.append(DouyinParser(self._http, logger, common_cfg))
        if platforms.get("bilibili", True):
            self._parsers.append(BilibiliParser(self._http, logger, common_cfg))
        if platforms.get("xiaohongshu", True):
            self._parsers.append(XiaohongshuParser(self._http, logger, common_cfg))

    def _make_http_client(self, timeout: int) -> httpx.AsyncClient:
        proxy = (self.config.get("proxy") or "").strip()
        kwargs = {"timeout": timeout, "follow_redirects": False}
        if proxy:
            kwargs["proxy"] = proxy
        try:
            return httpx.AsyncClient(**kwargs)
        except TypeError:
            # 兼容较老 httpx 的 proxies 参数
            if proxy:
                kwargs.pop("proxy", None)
                kwargs["proxies"] = proxy
            return httpx.AsyncClient(**kwargs)

    async def terminate(self):
        if self._http:
            try:
                await self._http.aclose()
            except Exception:  # noqa: BLE001
                pass
        if self._file_server:
            try:
                self._file_server.shutdown()
                self._file_server.server_close()
            except Exception:  # noqa: BLE001
                pass
            self._file_server = None
            self._file_server_thread = None

    # ------------------------------------------------------------------ #
    # 主消息处理
    # ------------------------------------------------------------------ #
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        if not self.config.get("enabled", True):
            return

        denied = self._check_access(event)
        if denied is not None:
            if denied != "BLOCKED":
                yield denied
            return

        text = self._extract_text(event)
        if not text:
            return

        urls = _RE_URL.findall(text)
        if not urls:
            return

        # 找到第一条能解析的 URL
        for raw in urls:
            url = raw if raw.startswith("http") else "https://" + raw
            parser = await self._select_parser(url)
            if parser is None:
                continue

            if self._is_debounced(event, url):
                self._dlog(f"链接在防抖窗口内，跳过: {url}")
                # 只 return 不 stop_event 的话，插件不吭声但 LLM 会继续回复这条链接。
                if self.config.get("stop_event_after_match", True):
                    event.stop_event()
                return

            # 先落防抖记录再解析：同一链接的两条消息几乎同时到达时只解析一次；
            # 解析失败会立即撤销记录，用户重发仍能重试。
            self._mark_debounce(event, url)
            self._dlog(f"匹配到 {parser.platform} 链接: {url}")
            if self.config.get("notify_when_triggered", True):
                yield event.plain_result("解析bot为您服务 ٩( 'ω' )و")

            try:
                item = await self._parse_with_retry(parser, url)
            except ParseError as e:
                self._clear_debounce(event, url)
                logger.warning(f"[share_forward] {parser.platform} 解析失败: {e}")
                if self.config.get("reply_when_fail", True):
                    yield event.plain_result(
                        f"[分享解析] {parser.platform} 解析失败：{e}"
                    )
                if self.config.get("stop_event_after_match", True):
                    event.stop_event()
                return
            except Exception as e:  # noqa: BLE001
                self._clear_debounce(event, url)
                logger.exception(f"[share_forward] {parser.platform} 解析异常: {e}")
                if self.config.get("reply_when_fail", True):
                    yield event.plain_result(
                        f"[分享解析] {parser.platform} 解析异常：{e}"
                    )
                if self.config.get("stop_event_after_match", True):
                    event.stop_event()
                return

            # 构建合并转发或降级消息
            try:
                async for chain in self._dispatch_send(event, item):
                    yield chain
            except Exception as e:  # noqa: BLE001
                logger.exception(f"[share_forward] 发送失败: {e}")
                if self.config.get("reply_when_fail", True):
                    yield event.plain_result(f"[分享解析] 发送失败：{e}")

            if self.config.get("stop_event_after_match", True):
                event.stop_event()
            return

    async def _select_parser(self, url: str) -> Optional[BaseParser]:
        for p in self._parsers:
            try:
                if await p.can_parse(url):
                    return p
            except Exception:  # noqa: BLE001
                continue
        return None

    async def _parse_with_retry(self, parser: BaseParser, url: str) -> ParsedItem:
        retry = int(self.config.get("retry_count", 1) or 0)
        last_error: Exception | None = None
        for attempt in range(retry + 1):
            try:
                return await parser.parse(url)
            except Exception as e:  # noqa: BLE001
                last_error = e
                if attempt < retry:
                    await asyncio.sleep(0.8 * (attempt + 1))
        assert last_error is not None
        raise last_error

    # ------------------------------------------------------------------ #
    # 消息构建
    # ------------------------------------------------------------------ #
    async def _dispatch_send(self, event: AstrMessageEvent, item: ParsedItem):
        platform_name = event.get_platform_name() or ""
        use_forward = self.config.get("use_forward_message", True)
        if platform_name == "aiocqhttp" and use_forward:
            async for chain in self._send_forward(event, item):
                yield chain
        else:
            async for chain in self._send_fallback(event, item):
                yield chain

    def _content_flags(self, item: ParsedItem) -> tuple[bool, bool]:
        """返回 (是否发送正文/简介节点, 是否发送封面/图集节点)。

        打开 `forward_content.exclusive_desc_cover` 后，简介和封面/图集只发一个：
        有简介就只发简介，没有简介才发封面/图集。
        """
        fc = self.config.get("forward_content", {}) or {}
        desc = _desc_text(item)
        desc_sent = (
            bool(fc.get("include_text_desc", True))
            and bool(desc)
            and desc != item.title
        )
        cover_sent = bool(fc.get("include_cover", True))
        if cover_sent and desc_sent and fc.get("exclusive_desc_cover", False):
            cover_sent = False
        return desc_sent, cover_sent

    async def _send_forward(self, event: AstrMessageEvent, item: ParsedItem):
        """构造并发送 QQ 合并转发"""
        bot_uin = str(event.get_self_id() or "10000")
        bot_name = "分享解析"
        mode = self.config.get("forward_grouping_mode", "sectioned")
        if mode == "flat":
            async for chain in self._send_forward_flat(event, item, bot_uin, bot_name):
                yield chain
            return

        async for chain in self._send_forward_sectioned(event, item, bot_uin, bot_name):
            yield chain

    async def _send_forward_sectioned(
        self,
        event: AstrMessageEvent,
        item: ParsedItem,
        bot_uin: str,
        bot_name: str,
    ):
        """按内容类型分组的合并转发。"""
        nodes: List[Comp.Node] = []
        cleanup_paths: list[str] = []

        fc = self.config.get("forward_content", {}) or {}

        # 1) 解析信息
        if fc.get("include_title_author", True):
            _add_text_section(
                nodes, bot_uin, bot_name, "📌 解析信息", self._format_title_block(item)
            )

        desc_sent, cover_sent = self._content_flags(item)

        # 2) 正文内容
        if desc_sent:
            section_title = "📝 简介" if item.platform == "bilibili" else "📝 正文内容"
            _add_text_section(
                nodes, bot_uin, bot_name, section_title, _desc_text(item)
            )

        # 3) 图片内容
        if cover_sent:
            if item.item_type == "images" and item.images:
                _append_section(nodes, bot_uin, bot_name, "🖼️ 图片内容")
                images = self._limit_images(item.images)
                for idx, img in enumerate(images, 1):
                    nodes.append(
                        Comp.Node(
                            uin=bot_uin,
                            name=bot_name,
                            content=[
                                Comp.Plain(f"图 {idx}/{len(images)}"),
                                Comp.Image.fromURL(img),
                            ],
                        )
                    )
            elif item.cover:
                _append_section(nodes, bot_uin, bot_name, "🖼️ 图片内容")
                nodes.append(
                    Comp.Node(
                        uin=bot_uin,
                        name=bot_name,
                        content=[Comp.Image.fromURL(item.cover)],
                    )
                )

        # 4) 视频内容
        if (
            fc.get("include_video_file", True)
            and item.item_type == "video"
            and item.video_url
        ):
            video_component = await self._build_video_component(item, cleanup_paths)
            if video_component:
                _append_section(nodes, bot_uin, bot_name, "🎬 视频内容")
                nodes.append(
                    Comp.Node(
                        uin=bot_uin,
                        name=bot_name,
                        content=[video_component],
                    )
                )

        # 5) 热门评论
        if fc.get("include_top_comments", True) and item.comments:
            _add_text_section(
                nodes,
                bot_uin,
                bot_name,
                "💬 热门评论",
                self._format_comments_block(item),
            )

        # 6) 链接信息
        if fc.get("include_links", True):
            _add_text_section(
                nodes, bot_uin, bot_name, "🔗 链接信息", self._format_link_block(item)
            )

        if not nodes:
            yield event.plain_result(
                f"[分享解析] 内容已识别，但所有内容节点都被关闭了。"
            )
            return

        try:
            yield event.chain_result([Comp.Nodes(nodes)])
        finally:
            self._schedule_cleanup_downloaded_files(cleanup_paths)

    async def _send_forward_flat(
        self,
        event: AstrMessageEvent,
        item: ParsedItem,
        bot_uin: str,
        bot_name: str,
    ):
        """旧版平铺合并转发，作为兼容模式保留。"""
        nodes: List[Comp.Node] = []
        cleanup_paths: list[str] = []

        fc = self.config.get("forward_content", {}) or {}

        if fc.get("include_title_author", True):
            _append_plain_node(nodes, bot_uin, bot_name, self._format_title_block(item))

        desc_sent, cover_sent = self._content_flags(item)

        if desc_sent:
            _append_plain_node(
                nodes, bot_uin, bot_name, self._format_desc_block(item)
            )

        if cover_sent:
            if item.item_type == "images" and item.images:
                images = self._limit_images(item.images)
                for idx, img in enumerate(images, 1):
                    nodes.append(
                        Comp.Node(
                            uin=bot_uin,
                            name=bot_name,
                            content=[
                                Comp.Plain(f"图 {idx}/{len(images)}"),
                                Comp.Image.fromURL(img),
                            ],
                        )
                    )
            elif item.cover:
                nodes.append(
                    Comp.Node(
                        uin=bot_uin,
                        name=bot_name,
                        content=[Comp.Image.fromURL(item.cover)],
                    )
                )

        if (
            fc.get("include_video_file", True)
            and item.item_type == "video"
            and item.video_url
        ):
            video_component = await self._build_video_component(item, cleanup_paths)
            if video_component:
                nodes.append(
                    Comp.Node(
                        uin=bot_uin,
                        name=bot_name,
                        content=[video_component],
                    )
                )

        if fc.get("include_top_comments", True) and item.comments:
            comments_text = self._format_comments_block(item)
            if comments_text:
                _append_plain_node(
                    nodes, bot_uin, bot_name, "热门评论 Top 3:\n" + comments_text
                )

        if fc.get("include_links", True):
            _append_plain_node(nodes, bot_uin, bot_name, self._format_link_block(item))

        if not nodes:
            yield event.plain_result(
                f"[分享解析] 内容已识别，但所有内容节点都被关闭了。"
            )
            return

        try:
            yield event.chain_result([Comp.Nodes(nodes)])
        finally:
            self._schedule_cleanup_downloaded_files(cleanup_paths)

    async def _send_fallback(self, event: AstrMessageEvent, item: ParsedItem):
        """非 QQ 平台：降级为多条普通消息。"""
        fc = self.config.get("forward_content", {}) or {}
        cleanup_paths: list[str] = []
        try:
            desc_sent, cover_sent = self._content_flags(item)

            # 头部信息
            if fc.get("include_title_author", True):
                yield event.plain_result(self._format_title_block(item))
            if desc_sent:
                yield event.plain_result(self._format_desc_block(item))

            if cover_sent:
                if item.item_type == "images":
                    for img in self._limit_images(item.images):
                        await event.send(MessageChain([Comp.Image.fromURL(img)]))
                elif item.cover:
                    await event.send(MessageChain([Comp.Image.fromURL(item.cover)]))

            if (
                fc.get("include_video_file", True)
                and item.item_type == "video"
                and item.video_url
            ):
                video_component = await self._build_video_component(item, cleanup_paths)
                if video_component:
                    await event.send(MessageChain([video_component]))

            if fc.get("include_top_comments", True) and item.comments:
                comments_text = self._format_comments_block(item)
                if comments_text:
                    yield event.plain_result("热门评论 Top 3:\n" + comments_text)

            if fc.get("include_links", True):
                link_text = self._format_link_block(item)
                if link_text:
                    yield event.plain_result(link_text)
        finally:
            self._schedule_cleanup_downloaded_files(cleanup_paths)

    # ------------------------------------------------------------------ #
    # 文本块构建
    # ------------------------------------------------------------------ #
    def _format_title_block(self, item: ParsedItem) -> str:
        title = item.title or "(无标题)"
        author = item.author or "未知作者"
        return f"{_platform_tag(item.platform)} 📖 标题：{title}\n👤 作者：{author}"

    @staticmethod
    def _format_desc_block(item: ParsedItem) -> str:
        label = "简介" if item.platform == "bilibili" else "正文"
        return f"📝 {label}：{_desc_text(item)}"

    def _format_link_block(self, item: ParsedItem) -> str:
        lines = ["🌐 原始链接：" + (item.canonical_url or item.raw_url)]
        if item.item_type == "video" and item.video_url:
            label = "🎞️ 视频直链" if item.platform == "bilibili" else "🎞️ 无水印直链"
            lines.append(f"{label}：" + item.video_url)
        return "\n".join(lines)

    def _format_comments_block(self, item: ParsedItem) -> str:
        """按真正输出的顺序发奖牌，避免第一条为空时从🥈开始。"""
        medals = ["🥇", "🥈", "🥉"]
        lines: list[str] = []
        for comment in item.comments:
            if len(lines) >= 3:
                break
            content = (comment.get("content") or "").strip()
            if not content:
                continue
            lines.append(f"{medals[len(lines)]} {content}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # 视频下载
    # ------------------------------------------------------------------ #
    def _video_cache_key(self, item: ParsedItem) -> str:
        """用内容 ID 而不是带过期参数的直链做键，重复分享才能复用同一个文件。"""
        if item.platform == "bilibili":
            parts = (
                "bili",
                item.extra.get("bvid", ""),
                item.extra.get("cid", ""),
                item.extra.get("video_quality", ""),
            )
        elif item.platform == "douyin":
            parts = ("dy", item.extra.get("aweme_id", ""), "")
        else:
            parts = ("xhs", item.extra.get("note_id", ""), "")
        identity = "_".join(part for part in parts if part)
        if identity in ("bili", "dy", "xhs") or not identity:
            # 没有稳定的内容 ID 时退回直链；直链带签名，基本不会命中缓存。
            identity = item.video_url or item.canonical_url or item.raw_url
        return hashlib.sha256(
            f"{item.platform}|{identity}".encode("utf-8", errors="ignore")
        ).hexdigest()[:16]

    async def _download_video(self, item: ParsedItem) -> Optional[str]:
        """下载视频；同一内容并发请求只下一次，已下载过的文件直接复用。"""
        url = item.video_url
        if not url:
            return None

        max_mb = int(self.config.get("video_max_size_mb", 80) or 80)
        max_bytes = max_mb * 1024 * 1024
        tmp_dir = self._get_download_dir()
        os.makedirs(tmp_dir, exist_ok=True)

        key = self._video_cache_key(item)
        path = os.path.join(tmp_dir, f"share_fwd_{item.platform}_{key}.mp4")
        lock = self._download_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                # 刷新 mtime，延迟清理任务据此判断文件刚被复用，不会误删。
                try:
                    os.utime(path, None)
                except OSError:
                    pass
                self._dlog(f"复用已下载视频: {path}")
                return path
            return await self._download_video_to_path(item, path, max_bytes)

    async def _download_video_to_path(
        self, item: ParsedItem, path: str, max_bytes: int
    ) -> Optional[str]:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        }
        if item.video_referer:
            headers["Referer"] = item.video_referer
        if item.platform == "bilibili":
            cookie = str(self.config.get("bilibili_cookie") or "").strip()
            if cookie:
                headers["Cookie"] = cookie.replace("\r", "").replace("\n", "")

        ok = False
        try:
            async with self._http.stream(
                "GET", item.video_url, headers=headers, follow_redirects=True
            ) as response:
                if response.status_code != 200:
                    self._dlog(f"视频下载状态码: {response.status_code}")
                    return None
                content_length = response.headers.get("content-length")
                if content_length and content_length.isdigit():
                    if int(content_length) > max_bytes:
                        self._dlog(
                            f"视频体积 {int(content_length) / 1024 / 1024:.1f}MB 超限"
                        )
                        return None
                size = 0
                with open(path, "wb", buffering=_DOWNLOAD_CHUNK_SIZE) as fp:
                    async for chunk in response.aiter_bytes(_DOWNLOAD_CHUNK_SIZE):
                        size += len(chunk)
                        if size > max_bytes:
                            self._dlog("视频下载中超限，已中止")
                            return None
                        fp.write(chunk)
            if size <= 0:
                self._dlog("视频下载结果为空")
                return None
            ok = True
            return path
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[share_forward] 视频下载失败: {e}")
            return None
        finally:
            # 超时/断流/取消时删掉写了一半的文件，避免下载目录堆积垃圾。
            if not ok:
                _safe_unlink(path)

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _group_id(event: AstrMessageEvent) -> str:
        """不是所有平台的事件都有 message_obj.group_id，统一安全取值。"""
        message_obj = getattr(event, "message_obj", None)
        return str(getattr(message_obj, "group_id", "") or "")

    def _check_access(self, event: AstrMessageEvent):
        """检查私聊、群聊和统一会话白名单。"""
        group_id = self._group_id(event)

        if not self.config.get("enable_in_private", True) and not group_id:
            return "BLOCKED"

        group_whitelist = self.config.get("group_whitelist", []) or []
        if group_whitelist and group_id:
            allowed = {str(x).strip() for x in group_whitelist if str(x).strip()}
            if group_id not in allowed:
                return "BLOCKED"

        id_whitelist = self.config.get("id_whitelist", []) or []
        if id_whitelist:
            umo = getattr(event, "unified_msg_origin", "") or ""
            allowed = {str(x).strip() for x in id_whitelist if str(x).strip()}
            if umo not in allowed:
                reply = self.config.get("whitelist_reply", "")
                return event.plain_result(reply) if reply else "BLOCKED"

        return None

    def _extract_text(self, event: AstrMessageEvent) -> str:
        """提取纯文本和 QQ 卡片 JSON 里的链接。"""
        text = event.message_str or ""
        try:
            chain = event.get_messages()
        except Exception:  # noqa: BLE001
            chain = []
        if not chain:
            return text

        # 专门 @ 其他机器人的消息不抢解析，避免多 Bot 场景误触发。
        first = chain[0]
        if first.__class__.__name__ == "At" and str(getattr(first, "qq", "")) != str(
            event.get_self_id()
        ):
            return ""

        for seg in chain:
            if seg.__class__.__name__ != "Json":
                continue
            data = getattr(seg, "data", "") or ""
            text += "\n" + _extract_url_from_json_payload(data)
        return text

    def _debounce_key(self, event: AstrMessageEvent, url: str) -> str:
        session = getattr(event, "unified_msg_origin", "") or "global"
        return f"{session}:{url}"

    def _is_debounced(self, event: AstrMessageEvent, url: str) -> bool:
        seconds = int(self.config.get("debounce_interval", 120) or 0)
        if seconds <= 0:
            return False
        key = self._debounce_key(event, url)
        now = time.time()
        expired = [k for k, ts in self._recent_links.items() if now - ts > seconds]
        for k in expired:
            self._recent_links.pop(k, None)
        if key in self._recent_links and now - self._recent_links[key] <= seconds:
            return True
        return False

    def _mark_debounce(self, event: AstrMessageEvent, url: str):
        seconds = int(self.config.get("debounce_interval", 120) or 0)
        if seconds <= 0:
            return
        self._recent_links[self._debounce_key(event, url)] = time.time()

    def _clear_debounce(self, event: AstrMessageEvent, url: str):
        """解析失败时撤销记录，避免把用户的正常重试也防抖掉。"""
        self._recent_links.pop(self._debounce_key(event, url), None)

    def _limit_images(self, images: List[str]) -> List[str]:
        max_images = int(self.config.get("max_images_per_forward", 18) or 18)
        if max_images <= 0:
            return images
        return images[:max_images]

    async def _build_video_component(
        self,
        item: ParsedItem,
        cleanup_paths: Optional[list[str]] = None,
    ):
        if item.platform == "bilibili":
            return await self._build_bilibili_video_component(item, cleanup_paths)

        mode = self.config.get("video_send_mode", "direct_url")
        if mode == "off" or not item.video_url:
            return None
        if mode == "direct_url":
            try:
                return Comp.Video.fromURL(item.video_url)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[share_forward] URL 视频组件构造失败: {e}")
                return None
        if mode == "download":
            video_path = await self._prepare_downloaded_video(item, cleanup_paths)
            return Comp.Video.fromFileSystem(video_path) if video_path else None
        return None

    async def _prepare_downloaded_video(
        self, item: ParsedItem, cleanup_paths: Optional[list[str]] = None
    ) -> Optional[str]:
        video_path = await self._download_video(item)
        if (
            video_path
            and cleanup_paths is not None
            and self.config.get("cleanup_downloaded_video", True)
        ):
            cleanup_paths.append(video_path)
        return video_path

    def _get_download_dir(self) -> str:
        cfg_dir = (self.config.get("download_video_dir") or "").strip()
        if cfg_dir:
            return cfg_dir
        return os.path.join(tempfile.gettempdir(), "share_forward_videos")

    def _resolve_bilibili_access_mode(self) -> str:
        mode = (self.config.get("bilibili_download_access_mode") or "auto").strip()
        if mode and mode != "auto":
            return mode
        if (self.config.get("bilibili_file_server_base_url") or "").strip():
            return "http_server"
        if (self.config.get("bilibili_shared_video_dir") or "").strip():
            return "shared_dir"
        return "off"

    def _warn_bilibili_access_once(self) -> None:
        if self._warned_bilibili_access:
            return
        self._warned_bilibili_access = True
        logger.warning(
            "[share_forward] B站视频为 download 模式，但既没有配置 "
            "bilibili_file_server_base_url，也没有配置 bilibili_shared_video_dir，"
            "NapCat 用不了本地文件，已直接跳过视频节点（不会再白下载一遍）。"
            "请按 README 配置其中一种访问方式。"
        )

    async def _build_bilibili_video_component(
        self, item: ParsedItem, cleanup_paths: Optional[list[str]] = None
    ):
        if not item.video_url:
            return None
        if self.config.get("bilibili_video_send_mode", "download") == "off":
            return None

        # 先判访问方式再决定要不要下载：旧版会先下完几十 MB 再因为“没配置访问方式”把文件丢掉。
        access_mode = self._resolve_bilibili_access_mode()
        if access_mode == "off":
            self._warn_bilibili_access_once()
            return None

        video_path = await self._prepare_downloaded_video(item, cleanup_paths)
        if not video_path:
            return None

        if access_mode == "local_file":
            # 仅适合 AstrBot 与 NapCat 在同一文件系统/同一容器时使用。
            return Comp.Video.fromFileSystem(video_path)

        if access_mode == "shared_dir":
            path = self._build_shared_video_path(video_path)
            if path:
                return Comp.Video.fromFileSystem(path)
            self._dlog("B站共享目录路径不可用，已跳过视频文件节点")
            return None

        url = self._build_video_http_url(video_path)
        if url:
            return Comp.Video.fromURL(url)
        self._dlog("B站 HTTP 文件服务不可用，已跳过视频文件节点")
        return None

    def _build_video_http_url(self, video_path: str) -> Optional[str]:
        base_url = (self.config.get("bilibili_file_server_base_url") or "").strip().rstrip("/")
        if not base_url:
            return None
        if not self._ensure_file_server():
            return None

        filename = os.path.basename(video_path)
        url = f"{base_url}/{quote(filename)}"
        token = (self.config.get("bilibili_file_server_token") or "").strip()
        if token:
            url += f"?token={quote(token)}"
        return url

    def _ensure_file_server(self) -> bool:
        if self._file_server:
            return True

        download_dir = self._get_download_dir()
        os.makedirs(download_dir, exist_ok=True)
        host = (self.config.get("bilibili_file_server_host") or "0.0.0.0").strip()
        port = int(self.config.get("bilibili_file_server_port", 6186) or 6186)
        token = (self.config.get("bilibili_file_server_token") or "").strip()

        try:
            handler = partial(
                _ShareForwardVideoHandler,
                directory=download_dir,
                token=token,
            )
            server = ThreadingHTTPServer((host, port), handler)
            server.daemon_threads = True
            thread = threading.Thread(
                target=server.serve_forever,
                name="share-forward-video-server",
                daemon=True,
            )
            thread.start()
            self._file_server = server
            self._file_server_thread = thread
            self._dlog(f"B站视频 HTTP 文件服务已启动: {host}:{port}, 目录: {download_dir}")
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[share_forward] 启动 B站视频 HTTP 文件服务失败: {e}")
            return False

    def _build_shared_video_path(self, video_path: str) -> Optional[str]:
        shared_dir = (self.config.get("bilibili_shared_video_dir") or "").strip()
        if not shared_dir:
            return None
        return os.path.join(shared_dir, os.path.basename(video_path))

    def _schedule_cleanup_downloaded_files(self, paths: list[str]):
        """延迟清理下载文件，避免 AstrBot/NapCat 还没读取就被删除。"""
        if not paths or not self.config.get("cleanup_downloaded_video", True):
            return
        delay = int(self.config.get("cleanup_downloaded_video_delay", 600) or 600)
        asyncio.create_task(self._cleanup_downloaded_files_later(list(paths), delay))

    async def _cleanup_downloaded_files_later(self, paths: list[str], delay: int):
        if delay > 0:
            await asyncio.sleep(delay)
        self._cleanup_downloaded_files(paths)

    def _cleanup_downloaded_files(self, paths: list[str]):
        if not self.config.get("cleanup_downloaded_video", True):
            return
        delay = int(self.config.get("cleanup_downloaded_video_delay", 600) or 600)
        now = time.time()
        for path in paths:
            try:
                if not path or not os.path.exists(path):
                    continue
                # 文件刚被其他会话复用过（mtime 被刷新）就再等等，别把正在发送的文件删了。
                if delay > 0 and now - os.path.getmtime(path) < delay:
                    self._dlog(f"视频刚被复用，跳过本次清理: {path}")
                    continue
                os.remove(path)
                self._dlog(f"已清理下载视频: {path}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[share_forward] 清理下载视频失败: {path}, {e}")

    def _dlog(self, msg: str):
        if self.config.get("debug_log"):
            logger.info(f"[share_forward] {msg}")


def _extract_url_from_json_payload(payload: Any) -> str:
    if not payload:
        return ""
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except Exception:  # noqa: BLE001
            m = SUPPORTED_SHARE_URL.search(payload)
            return m.group(0) if m else ""
    elif isinstance(payload, (dict, list)):
        data = payload
    else:
        return ""

    # QQ cards also contain icon/preview image URLs. Prefer the semantic jump
    # fields so an image URL cannot hide the actual share target.
    def find_key(obj: Any, key: str) -> str:
        if isinstance(obj, dict):
            value = obj.get(key)
            if isinstance(value, str):
                m = SUPPORTED_SHARE_URL.search(value)
                if m:
                    return m.group(0)
            for child in obj.values():
                found = find_key(child, key)
                if found:
                    return found
        elif isinstance(obj, list):
            for child in obj:
                found = find_key(child, key)
                if found:
                    return found
        return ""

    for key in _CARD_URL_KEYS:
        found = find_key(data, key)
        if found:
            return found

    found_urls: list[str] = []

    def walk(obj: Any):
        if isinstance(obj, str):
            m = SUPPORTED_SHARE_URL.search(obj)
            if m:
                found_urls.append(m.group(0))
        elif isinstance(obj, dict):
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(data)
    return found_urls[0] if found_urls else ""

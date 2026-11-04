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
)


_RE_URL = re.compile(
    r"(https?://[^\s\u4e00-\u9fa5\"'<>]+|"
    r"(?:v\.douyin\.com|b23\.tv|xhslink\.com)/[A-Za-z0-9_\-/]+)",
    re.IGNORECASE,
)

_RE_SUPPORTED_SHARE_URL = re.compile(
    r"(?:https?://)?(?:"
    r"(?:www\.|m\.)?bilibili\.com/[^\s\u4e00-\u9fa5\"'<>]+|"
    r"b23\.tv/[A-Za-z0-9]+|"
    r"v\.douyin\.com/[A-Za-z0-9_\-/]+|"
    r"xhslink\.com/[A-Za-z0-9_\-/]+|"
    r"(?:www\.)?xiaohongshu\.com/[^\s\u4e00-\u9fa5\"'<>]+"
    r")",
    re.IGNORECASE,
)

_CARD_URL_KEYS = ("qqdocurl", "jumpUrl", "jump_url", "sourceUrl", "source_url", "url")


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
    "1.1.8",
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
                return

            self._dlog(f"匹配到 {parser.platform} 链接: {url}")
            if self.config.get("notify_when_triggered", True):
                yield event.plain_result("解析bot为您服务 ٩( 'ω' )و")

            try:
                item = await self._parse_with_retry(parser, url)
                self._mark_debounce(event, url)
            except ParseError as e:
                logger.warning(f"[share_forward] {parser.platform} 解析失败: {e}")
                if self.config.get("reply_when_fail", True):
                    yield event.plain_result(
                        f"[分享解析] {parser.platform} 解析失败：{e}"
                    )
                if self.config.get("stop_event_after_match", True):
                    event.stop_event()
                return
            except Exception as e:  # noqa: BLE001
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
            self._append_section(nodes, bot_uin, bot_name, "📌 解析信息")
            self._append_plain_node(nodes, bot_uin, bot_name, self._format_title_block(item))

        # 2) 正文内容
        if fc.get("include_text_desc", True) and item.desc and item.desc != item.title:
            section_title = "📝 简介" if item.platform == "bilibili" else "📝 正文内容"
            self._append_section(nodes, bot_uin, bot_name, section_title)
            self._append_plain_node(nodes, bot_uin, bot_name, item.desc)

        # 3) 图片内容
        if fc.get("include_cover", True):
            if item.item_type == "images" and item.images:
                self._append_section(nodes, bot_uin, bot_name, "🖼️ 图片内容")
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
                self._append_section(nodes, bot_uin, bot_name, "🖼️ 图片内容")
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
                self._append_section(nodes, bot_uin, bot_name, "🎬 视频内容")
                nodes.append(
                    Comp.Node(
                        uin=bot_uin,
                        name=bot_name,
                        content=[video_component],
                    )
                )

        # 5) 热门评论
        if fc.get("include_top_comments", True) and item.comments:
            self._append_section(nodes, bot_uin, bot_name, "💬 热门评论")
            self._append_plain_node(
                nodes, bot_uin, bot_name, self._format_comments_block(item)
            )

        # 6) 链接信息
        if fc.get("include_links", True):
            link_text = self._format_link_block(item)
            if link_text:
                self._append_section(nodes, bot_uin, bot_name, "🔗 链接信息")
                self._append_plain_node(nodes, bot_uin, bot_name, link_text)

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
            self._append_plain_node(nodes, bot_uin, bot_name, self._format_title_block(item))

        if fc.get("include_text_desc", True) and item.desc and item.desc != item.title:
            self._append_plain_node(
                nodes, bot_uin, bot_name, self._format_desc_block(item)
            )

        if fc.get("include_cover", True):
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
            self._append_plain_node(
                nodes, bot_uin, bot_name, "热门评论 Top 3:\n" + self._format_comments_block(item)
            )

        if fc.get("include_links", True):
            link_text = self._format_link_block(item)
            if link_text:
                self._append_plain_node(nodes, bot_uin, bot_name, link_text)

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
            # 头部信息
            if fc.get("include_title_author", True):
                yield event.plain_result(self._format_title_block(item))
            if fc.get("include_text_desc", True) and item.desc and item.desc != item.title:
                yield event.plain_result(self._format_desc_block(item))

            if fc.get("include_cover", True):
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
                yield event.plain_result("热门评论 Top 3:\n" + self._format_comments_block(item))

            if fc.get("include_links", True):
                yield event.plain_result(self._format_link_block(item))
        finally:
            self._schedule_cleanup_downloaded_files(cleanup_paths)

    # ------------------------------------------------------------------ #
    # 文本块构建
    # ------------------------------------------------------------------ #
    @staticmethod
    def _platform_tag(p: str) -> str:
        return {
            "douyin": "[抖音]",
            "bilibili": "[哔哩哔哩]",
            "xiaohongshu": "[小红书]",
        }.get(p, f"[{p}]")

    def _format_title_block(self, item: ParsedItem) -> str:
        title = item.title or "(无标题)"
        author = item.author or "未知作者"
        return f"📖 标题：{title}\n👤 作者：{author}"

    @staticmethod
    def _format_desc_block(item: ParsedItem) -> str:
        label = "简介" if item.platform == "bilibili" else "正文"
        return f"📝 {label}：{item.desc}"

    def _format_link_block(self, item: ParsedItem) -> str:
        lines = ["🌐 原始链接：" + (item.canonical_url or item.raw_url)]
        if item.item_type == "video" and item.video_url:
            label = "🎞️ 视频直链" if item.platform == "bilibili" else "🎞️ 无水印直链"
            lines.append(f"{label}：" + item.video_url)
        return "\n".join(lines)

    def _format_comments_block(self, item: ParsedItem) -> str:
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for idx, c in enumerate(item.comments[:3]):
            content = (c.get("content") or "").strip()
            if not content:
                continue
            lines.append(f"{medals[idx]} {content}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # 视频下载
    # ------------------------------------------------------------------ #
    async def _download_video(self, item: ParsedItem) -> Optional[str]:
        max_mb = int(self.config.get("video_max_size_mb", 80) or 80)
        max_bytes = max_mb * 1024 * 1024
        url = item.video_url
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

        tmp_dir = self._get_download_dir()
        os.makedirs(tmp_dir, exist_ok=True)
        digest = hashlib.sha256(url.encode("utf-8", errors="ignore")).hexdigest()[:16]
        path = os.path.join(tmp_dir, f"share_fwd_{item.platform}_{digest}.mp4")
        try:
            async with self._http.stream("GET", url, headers=headers, follow_redirects=True) as r:
                if r.status_code != 200:
                    self._dlog(f"视频下载状态码: {r.status_code}")
                    return None
                cl = r.headers.get("content-length")
                if cl and int(cl) > max_bytes:
                    self._dlog(f"视频体积 {int(cl) / 1024 / 1024:.1f}MB 超限")
                    return None
                size = 0
                with open(path, "wb") as fp:
                    async for chunk in r.aiter_bytes(64 * 1024):
                        size += len(chunk)
                        if size > max_bytes:
                            fp.close()
                            try:
                                os.remove(path)
                            except OSError:
                                pass
                            self._dlog("视频下载中超限，已中止")
                            return None
                        fp.write(chunk)
            return path
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[share_forward] 视频下载失败: {e}")
            return None

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    def _append_section(
        self,
        nodes: List[Comp.Node],
        bot_uin: str,
        bot_name: str,
        title: str,
    ):
        """添加分类标题节点，模拟“合并转发内的小分组”。"""
        nodes.append(
            Comp.Node(
                uin=bot_uin,
                name=bot_name,
                content=[Comp.Plain(f"【{title}】")],
            )
        )

    def _append_plain_node(
        self,
        nodes: List[Comp.Node],
        bot_uin: str,
        bot_name: str,
        text: str,
    ):
        if not text:
            return
        nodes.append(
            Comp.Node(
                uin=bot_uin,
                name=bot_name,
                content=[Comp.Plain(text)],
            )
        )

    def _check_access(self, event: AstrMessageEvent):
        """检查私聊、群聊和统一会话白名单。"""
        if not self.config.get("enable_in_private", True):
            group_id = getattr(event.message_obj, "group_id", "") or ""
            if not group_id:
                return "BLOCKED"

        group_whitelist = self.config.get("group_whitelist", []) or []
        group_id = getattr(event.message_obj, "group_id", "") or ""
        if group_whitelist and group_id:
            allowed = {str(x).strip() for x in group_whitelist if str(x).strip()}
            if str(group_id) not in allowed:
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

    def _is_debounced(self, event: AstrMessageEvent, url: str) -> bool:
        seconds = int(self.config.get("debounce_interval", 120) or 0)
        if seconds <= 0:
            return False
        session = getattr(event, "unified_msg_origin", "") or "global"
        key = f"{session}:{url}"
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
        session = getattr(event, "unified_msg_origin", "") or "global"
        key = f"{session}:{url}"
        now = time.time()
        self._recent_links[key] = now

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
            mode = self.config.get("bilibili_video_send_mode", "download")
        else:
            mode = self.config.get("video_send_mode", "direct_url")

        if mode == "off":
            return None
        if mode == "direct_url":
            try:
                return Comp.Video.fromURL(item.video_url)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[share_forward] URL 视频组件构造失败: {e}")
                return None
        if mode == "download":
            video_path = await self._download_video(item)
            if video_path and self.config.get("cleanup_downloaded_video", True):
                if cleanup_paths is not None:
                    cleanup_paths.append(video_path)
            if item.platform == "bilibili":
                return self._build_bilibili_download_video_component(video_path)
            return Comp.Video.fromFileSystem(video_path) if video_path else None
        return None

    def _get_download_dir(self) -> str:
        cfg_dir = (self.config.get("download_video_dir") or "").strip()
        if cfg_dir:
            return cfg_dir
        return os.path.join(tempfile.gettempdir(), "share_forward_videos")

    def _build_bilibili_download_video_component(self, video_path: Optional[str]):
        if not video_path:
            return None

        access_mode = self.config.get("bilibili_download_access_mode", "auto")
        if access_mode == "auto":
            if (self.config.get("bilibili_file_server_base_url") or "").strip():
                access_mode = "http_server"
            elif (self.config.get("bilibili_shared_video_dir") or "").strip():
                access_mode = "shared_dir"
            else:
                access_mode = "off"

        if access_mode == "http_server":
            url = self._build_video_http_url(video_path)
            if url:
                return Comp.Video.fromURL(url)
            self._dlog("B站 HTTP 文件服务不可用，已跳过视频文件节点")
            return None

        if access_mode == "shared_dir":
            path = self._build_shared_video_path(video_path)
            if path:
                return Comp.Video.fromFileSystem(path)
            self._dlog("B站共享目录路径不可用，已跳过视频文件节点")
            return None

        if access_mode == "local_file":
            # 仅适合 AstrBot 与 NapCat 在同一文件系统/同一容器时使用。
            return Comp.Video.fromFileSystem(video_path)

        logger.warning(
            "[share_forward] B站 download 已下载视频，但未配置跨容器访问方式；"
            "请配置 bilibili_file_server_base_url 或 bilibili_shared_video_dir，"
            "否则将只保留链接信息并跳过视频文件节点"
        )
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
        for path in paths:
            try:
                if path and os.path.exists(path):
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
            m = _RE_SUPPORTED_SHARE_URL.search(payload)
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
                m = _RE_SUPPORTED_SHARE_URL.search(value)
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
            m = _RE_SUPPORTED_SHARE_URL.search(obj)
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

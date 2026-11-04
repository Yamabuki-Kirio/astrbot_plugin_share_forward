"""解析器基类与数据结构"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Dict, Optional

# B 站等平台在"没有简介"时会下发占位符而不是空串，直接发出去就会变成"简介：-"。
EMPTY_TEXT_PLACEHOLDERS = {"-", "--", "—", "――", "暂无", "暂无简介"}


def clean_text(value: object) -> str:
    """去掉首尾空白；占位符（如 `-`）按空文本处理。"""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if text in EMPTY_TEXT_PLACEHOLDERS:
        return ""
    return text


class ParseError(Exception):
    """解析失败"""


@dataclass
class ParsedItem:
    """单条分享链接的解析结果。"""

    platform: str           # douyin / bilibili / xiaohongshu
    raw_url: str            # 用户分享的原始链接
    item_type: str = "unknown"  # video / images / text
    title: str = ""
    author: str = ""
    desc: str = ""
    cover: str = ""             # 单张封面 URL
    images: List[str] = field(default_factory=list)  # 图集
    video_url: str = ""         # 无水印视频直链
    video_referer: str = ""     # 下载视频时所需 Referer
    comments: List[Dict[str, str]] = field(default_factory=list)
    canonical_url: str = ""     # 标准化的网页链接
    extra: Dict[str, str] = field(default_factory=dict)


class BaseParser:
    """所有平台解析器的抽象基类。"""

    platform: str = "base"

    def __init__(self, http_client, logger, config: Optional[dict] = None):
        self.http = http_client
        self.logger = logger
        self.config = config or {}

    async def can_parse(self, url: str) -> bool:
        raise NotImplementedError

    async def parse(self, url: str) -> ParsedItem:
        raise NotImplementedError

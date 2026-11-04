"""各平台分享链接的统一识别规则。

main.py 从 QQ 卡片 JSON 里找链接、各解析器判断 `can_parse`，都复用这里的正则，
避免同一条规则写两份然后各自跑偏（旧版就出现过 live.bilibili.com、抖音用户主页
被当成视频链接，解析失败后又给用户回一条报错）。
"""
from __future__ import annotations

import re

# 不允许匹配到别的域名里包含的字串，例如 notxhslink.com、foo-bilibili.com。
_LEFT_BOUNDARY = r"(?<![A-Za-z0-9._\-])"

# 抖音：短视频、图文、带 modal_id 的发现页。
# 用户主页（/user/）不是作品页，故意不匹配。
DOUYIN_LINK = re.compile(
    _LEFT_BOUNDARY
    + r"(?:https?://)?(?:"
    r"v\.douyin\.com/[A-Za-z0-9_\-]+/?"
    r"|(?:www\.)?iesdouyin\.com/share/(?:video|note)/\d+"
    r"|(?:www\.)?douyin\.com/(?:video|note)/\d+"
    r"|(?:www\.)?douyin\.com/(?:discover|search)\?(?:[^\s&]*&)*modal_id=\d+"
    r")",
    re.IGNORECASE,
)

# B 站：只认视频页和短链。直播 / 专栏 / 番剧 / 空间页由解析器处理不了，
# 这里直接不匹配，交给 LLM 或用户自己处理。
BILIBILI_LINK = re.compile(
    _LEFT_BOUNDARY
    + r"(?:https?://)?(?:"
    r"b23\.tv/[A-Za-z0-9]+"
    r"|(?:www\.|m\.)?bilibili\.com/video/(?:BV[0-9A-Za-z]{10}|av\d+)"
    r")",
    re.IGNORECASE,
)

# 小红书：短链与笔记页。
XIAOHONGSHU_LINK = re.compile(
    _LEFT_BOUNDARY
    + r"(?:https?://)?(?:"
    r"xhslink\.com/[A-Za-z0-9/_\-]+"
    r"|(?:www\.)?xiaohongshu\.com/(?:explore|discovery/item)/[A-Za-z0-9]+[^\s]*"
    r")",
    re.IGNORECASE,
)

# QQ 卡片 JSON 里所有受支持平台的合集，用于从任意字段中捞真实跳转链接。
SUPPORTED_SHARE_URL = re.compile(
    "(?:" + "|".join(p.pattern for p in (DOUYIN_LINK, BILIBILI_LINK, XIAOHONGSHU_LINK)) + ")",
    re.IGNORECASE,
)

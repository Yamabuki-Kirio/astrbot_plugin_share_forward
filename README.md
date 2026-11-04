# astrbot_plugin_share_forward

> 自动识别群聊/私聊里发出的 **抖音 / 哔哩哔哩 / 小红书** 分享链接，
> 解析其内容（标题、作者、正文、视频、图集、高赞评论），并打包成一条
> **QQ 合并转发** 消息发送回去。

## ✨ 功能特性

- 支持平台：
  - 抖音（`v.douyin.com` / `iesdouyin.com` / `douyin.com`）
    - 视频（含无水印直链）
    - 图集（多张图片）
    - 纯文字动态
    - 高赞评论 Top 3（需配置 Cookie）
  - 哔哩哔哩 B 站（`b23.tv` / `bilibili.com/video/BVxxx`）
    - 视频标题、UP 主、封面、简介
    - 音视频合一视频流；先请求一次最高画质，再按接口返回的 `accept_quality` 从高到低挑体积限制内最清晰的可用版本
    - 高赞评论 Top 3（匿名可获取）
    - 画质探测与热评并行请求，常见情况 1~2 次 playurl 请求即可定档
  - 小红书（`xhslink.com` / `xiaohongshu.com/explore/...`）
    - 图文、视频笔记
    - 高赞评论 Top 3
- 合并转发节点可自定义：
  - 标题 + 作者 + 平台标签
  - 封面图 / 图集
  - 视频文件直发（受 QQ 文件大小限制）
  - 原始链接 + 无水印直链
  - 高赞评论 Top 3
  - 正文/动态描述
- 仅 QQ 平台（`aiocqhttp`）使用合并转发，其他平台自动降级为多条普通消息；两种发送路径都遵守内容节点开关。
- 支持纯 QQ JSON 卡片消息：即使消息没有文本内容，也会优先从 `qqdocurl` 等真实跳转字段提取受支持平台链接。
- 默认识别到可解析链接后先发送 `解析bot为您服务 ٩( 'ω' )و`，便于判断插件是否触发。
- 合并转发默认按内容分类展示：📌解析信息、📝正文内容、🖼️图片内容、🎬视频内容、💬热门评论、🔗链接信息。
- 支持同链接防抖、解析重试、代理、群/会话白名单、私聊开关。
- 支持视频发送模式：
  - 抖音/小红书：使用全局 `video_send_mode`，支持 `direct_url` / `download` / `off`
  - B 站：使用独立 `bilibili_video_send_mode`，只支持 `download` / `off`
  - B 站默认 `download`，插件先下载视频，再通过插件内置 HTTP 文件服务或共享目录交给 NapCat，避免跨容器 `/tmp` 路径不可见。
  - `download` 模式下载的临时视频默认会延迟清理，避免 NapCat 尚未读取文件就被删除。

## 🚀 性能与复用

- 只识别作品页：B 站只认 `bilibili.com/video/BV…`、`/video/av…` 和 `b23.tv`；直播、专栏、番剧、空间页、抖音用户主页不再触发"解析失败"。
- 抖音短链一次 GET 就完成跳转和取数据，不再先 HEAD 再抓一次分享页。
- B 站画质先发一次最高档请求，再按接口返回的 `accept_quality` 只补请求真正存在的低档位；画质探测和热评并发执行，常见 1~2 次请求即可定档。
- 同一个视频被多个群同时分享时只会下载一次；清理延迟内被重复分享时直接复用已下载文件（按 BV/aweme_id 等内容 ID 命中，不依赖会过期的直链）。
- 下载超时/断流会删掉写了一半的文件，不会在下载目录里堆积。
- 视频按 1MB 块写入并开 1MB 写缓冲：80MB 视频的读写次数从 1280 次降到 80 次，减少 syscall 和对事件循环的抢占。
- B 站没配置跨容器访问方式时直接跳过视频节点，不会先下载几十 MB 再丢弃。

## 🔧 安装

仓库地址：`https://github.com/Yamabuki-Kirio/astrbot_plugin_share_forward`

通过 AstrBot 插件管理安装此仓库，或者把仓库目录放进 `AstrBot/data/plugins/`，然后在 WebUI 的「插件管理」里点击重载。

## ⚙️ 配置项

打开 WebUI → 插件管理 → 找到 `astrbot_plugin_share_forward` → 配置：

| 配置 | 说明 |
| --- | --- |
| `enabled` | 总开关 |
| `notify_when_triggered` | 识别到可解析链接后先发送触发提示 |
| `forward_grouping_mode` | 合并转发分组模式：`sectioned` 分类模式 / `flat` 旧版平铺模式 |
| `platforms` | 启用哪些平台（抖音 / B 站 / 小红书） |
| `forward_content` | 选择标题作者、封面/图集、视频文件、链接、热门评论、正文/简介；合并转发和普通发送都生效。`exclusive_desc_cover` 打开后简介与封面/图集二选一：有简介就只发简介，没有才发封面/图集 |
| `use_forward_message` | 是否使用 QQ 合并转发，不稳定时可关闭 |
| `video_send_mode` | 抖音/小红书视频发送模式：`direct_url` / `download` / `off` |
| `bilibili_video_send_mode` | B 站视频发送模式：`download` / `off`，默认 `download` |
| `bilibili_download_access_mode` | B 站下载视频访问方式：`auto` / `http_server` / `shared_dir` / `local_file` |
| `bilibili_file_server_base_url` | NapCat 访问 AstrBot 插件内置视频文件服务的地址，例如 `http://astrbot:6186` 或 `http://172.18.0.4:6186` |
| `bilibili_file_server_host` | 插件内置视频文件服务监听地址，一般保持 `0.0.0.0` |
| `bilibili_file_server_port` | 插件内置视频文件服务端口，默认 `6186` |
| `bilibili_file_server_token` | 可选访问令牌，填写后视频 URL 会带 token 参数 |
| `download_video_dir` | download 模式的视频下载目录，留空则使用系统临时目录下的 `share_forward_videos` |
| `bilibili_shared_video_dir` | shared_dir 模式下 NapCat 容器能看到的共享目录路径 |
| `cleanup_downloaded_video` | download 模式发送后延迟自动删除临时视频文件 |
| `cleanup_downloaded_video_delay` | 下载视频延迟清理秒数，默认 600 |
| `video_max_size_mb` | 视频文件直发的最大体积（MB）；B 站会选择此限制内最清晰的音视频合一版本 |
| `max_images_per_forward` | 图集最多发送图片数量 |
| `request_timeout` | 单次请求超时（秒） |
| `retry_count` | 解析失败重试次数 |
| `debounce_interval` | 同一会话内同链接防抖秒数 |
| `proxy` | 请求代理地址 |
| `enable_in_private` | 是否允许私聊触发 |
| `group_whitelist` | 群号白名单，留空表示不限 |
| `id_whitelist` | 统一会话白名单，适合精确限制触发范围 |
| `douyin_cookie` | 抖音 Cookie（拉取评论用，可选） |
| `bilibili_cookie` | B站 Cookie（可选，用于提升热评获取成功率） |
| `xiaohongshu_cookie` | 小红书 Cookie（**必填**，至少含 `a1=...`） |
| `stop_event_after_match` | 命中后阻断后续 LLM 处理 |
| `reply_when_fail` | 解析失败时给用户反馈 |
| `debug_log` | 调试日志 |

### Cookie 怎么获取

1. 浏览器登录对应平台
2. F12 → Application / 应用 → Cookies → 选中域名
3. 复制完整 Cookie 字符串（格式：`key1=value1; key2=value2; ...`）
4. 粘贴到对应配置项

### B 站 download 视频访问配置

B 站的视频直链经常不能被 NapCat 直接拉取，所以插件默认会先把视频下载到 AstrBot 所在环境，再把视频交给 NapCat。这里最容易出问题的是“NapCat 能不能看到这个视频文件”。

如果 AstrBot 和 NapCat 不在同一个文件系统里，例如分别运行在两个 Docker 容器、两台机器、两个隔离环境中，不要使用 `local_file`。这种情况下 NapCat 看不到 AstrBot 本地的 `/tmp/xxx.mp4`，容易报 `ENOENT`。

推荐优先使用 HTTP 文件服务。插件会在 AstrBot 所在环境里临时开一个只用于视频文件的 HTTP 服务，NapCat 通过这个地址拉取视频：

```text
bilibili_video_send_mode = download
bilibili_download_access_mode = auto
bilibili_file_server_base_url = http://AstrBot地址:6186
bilibili_file_server_host = 0.0.0.0
bilibili_file_server_port = 6186
```

`bilibili_file_server_base_url` 要填 NapCat 能访问到的地址，不一定是公网地址。

常见填写方式：

```text
# Docker / Compose / 1Panel 等同网络容器
http://astrbot:6186

# 已知 AstrBot 容器内网 IP
http://172.18.0.4:6186

# NapCat 和 AstrBot 在不同机器，但 NapCat 能访问这台主机
http://192.168.1.10:6186
```

如果 NapCat 和 AstrBot 在同一个 Docker 网络里，通常不需要把 `6186` 映射到公网，也不需要开放服务器防火墙；只要 NapCat 能访问 `bilibili_file_server_base_url` 即可。如果 NapCat 在另一台机器上，才需要保证对应地址和端口能被它访问。

也可以使用共享目录模式。适合你已经把同一个宿主机目录或 NAS 目录挂载给 AstrBot 和 NapCat 的情况：

```text
download_video_dir = /data/share_forward_videos
bilibili_download_access_mode = shared_dir
bilibili_shared_video_dir = /data/share_forward_videos
```

`download_video_dir` 是 AstrBot 写入视频的位置，`bilibili_shared_video_dir` 是 NapCat 读取视频的位置。两边路径可以相同，也可以不同，但它们必须指向同一批真实文件。

只有在 AstrBot 和 NapCat 运行在同一个容器、同一台机器且能看到完全相同的文件路径时，才建议使用：

```text
bilibili_download_access_mode = local_file
```

## 🧩 工作原理

1. 监听全部消息类型，无需 at 机器人即可触发
2. 从纯文本和 QQ JSON 卡片中提取受支持平台 URL（只认作品页，正则统一放在 `parsers/patterns.py`）；卡片优先读取真实跳转字段
3. 先检查白名单、私聊开关，再落同链接防抖记录
4. 依次让各平台解析器判定能否处理，并按配置做失败重试；失败会撤销防抖记录，用户重发可重试
5. 解析器返回 `ParsedItem`（含标题/作者/视频/图集/评论等）
6. 主流程根据配置组装合并转发的 `Node` 列表
7. 通过 `event.chain_result([Comp.Nodes(...)])` 发出，或降级为普通消息

## 🧪 测试

```bash
python -m unittest discover -s tests -t .
```

单测不需要安装 AstrBot：涉及 `main.py` 的用例通过 AST 抽取纯函数/方法运行，平台解析器只依赖标准库。

## ⚠️ 风险与限制

- 三大平台的反爬策略时常变化，本插件采用尽量"无签名/弱签名"的解析路径，
  **可能在风控更新后失效**。
- 抖音评论需要 Cookie；不带 Cookie 时仅返回视频/图集/正文。
- B 站匿名请求会先访问主站取得基础 Cookie；仍被风控时需要配置 `bilibili_cookie`。
- B 站只发送 `playurl` 返回的音视频合一版本（`fnval=0`），所以画质上限受登录状态和该接口限制：未登录通常最高 720P，配置 Cookie 后可能到 1080P；4K/HDR 需要 dash 分离音轨再合轨，本插件不做，因此不会选到 4K。
- 只识别作品页链接：B 站直播 / 专栏 / 番剧、抖音用户主页等不会被插件接管，避免误报"解析失败"。
- 小红书必须配置 Cookie，否则会被风控直接拒绝。
- 视频文件直发受 QQ Bot 协议端文件大小限制（NapCat / Lagrange 一般 ≤100MB）。

## 📜 License

MIT

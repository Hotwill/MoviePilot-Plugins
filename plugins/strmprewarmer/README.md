# STRM媒体信息预热（StrmPrewarmer）

STRM 入库后立即通过 Emby 的 `PlaybackInfo` 接口触发真实媒体探测，把分辨率、编码、码率、
音轨等 MediaInfo 提前写入 Emby 媒体库，减少首次播放前因 ffprobe 探测产生的等待。

功能移植自 [emby-strm-prewarmer](https://github.com/ado-uta/emby-strm-prewarmer)，并改成
事件驱动：不再依赖 systemd 定时器，入库后即时预热。

- 不下载完整视频，不修改 STRM，不修改 NFO，不注入虚假媒体信息
- 自动跳过已有完整 MediaInfo 的条目
- 支持同名 STRM 换源检测（内容指纹变化时重新刷新并预热）
- 支持定时全量补漏扫描与手动命令触发
- 失败自动重试，结果写入插件历史并可推送通知

## 触发方式

| 触发源 | 说明 |
| --- | --- |
| 整理入库事件 | MoviePilot 整理完成（`TransferComplete`）后按目标文件路径预热 |
| 媒体库 Webhook | Emby `library.new` 事件（需在 Emby 中配置 MoviePilot Webhook），按文件路径定位分集后预热 |
| 定时扫描 | 按 Cron 周期全量扫描，补齐缺失媒体信息、识别换源 |
| 远程命令 | 交互消息发送 `/strm_prewarm` 手动触发一次全量扫描 |

入库后 Emby 需要先扫描到新文件，插件才能找到对应条目。建议同时开启 Emby 媒体库
实时监控，或启用官方「媒体库服务器刷新」插件；也可以打开本插件的
「找不到时扫描媒体库」开关。

### 配置 Emby Webhook（推荐，最快触发）

Emby 后台 →「通知 / Webhooks」→ 新增 Webhook：

```text
URL: http(s)://<MoviePilot地址>/api/v1/webhook?token=<API_TOKEN>&source=<媒体服务器名称>
请求内容类型: application/json
事件: 勾选「媒体库 → 新媒体已添加」(library.new)
```

- `API_TOKEN` 是 MoviePilot 的 `API_TOKEN` 环境变量值；
- `source` 必须与 MoviePilot 中该 Emby 服务器的名称一致，否则事件无法归属到具体服务器；
- 剧集入库时 Emby 上报的是剧集 ID，插件会按文件路径重新定位到具体分集，无需额外配置。

## 配置说明

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| 启用插件 | 关 | 总开关 |
| 发送通知 / 成功也通知 | 关 | 默认只在预热失败时通知 |
| Emby 服务器 | 空 | 留空表示使用全部已启用的 Emby 服务器 |
| 监听整理入库 | 开 | 监听 MoviePilot 整理完成事件 |
| 监听媒体库Webhook | 开 | 监听 Emby 新增入库 Webhook |
| 仅处理STRM | 开 | 关闭后所有新入库媒体都会预热 |
| 入库后延迟（秒） | 10 | 留给 Emby 扫描的缓冲时间 |
| 等待识别超时（秒） | 600 | 超时仍未在 Emby 中找到条目则放弃 |
| 查找间隔（秒） | 15 | 轮询查找条目的间隔 |
| 失败重试次数 / 重试间隔 | 2 / 10 | 首次请求 + 2 次重试 = 最多 3 次 |
| 请求超时（秒） | 300 | 网盘响应慢时可增大 |
| 定时补漏扫描 | 空 | Cron 表达式，如 `0 3 * * *`；留空不执行 |
| 单次最多处理 | 0 | 0 表示不限制，低配机器可设为 100 |
| 条目间隔（秒） | 1 | 全量扫描时每个条目之间的等待时间 |
| 找不到时扫描媒体库 | 关 | 未找到条目时请求 Emby 扫描媒体库 |
| 深度查找条目 | 开 | 老版本 Emby 不支持 `Path` 查询时，遍历媒体库按路径匹配 |
| 深度查找上限 | 20000 | 深度查找最多遍历的条目数 |
| 去重窗口（秒） | 600 | 窗口内同一条目不重复预热，避免入库事件与 Webhook 重复触发 |
| 立即运行一次 | 关 | 保存后立刻执行一次全量扫描 |
| 探测码率上限 | 200000000 | `PlaybackInfo` 的 `MaxStreamingBitrate` 参数 |
| 路径映射 | 空 | `MoviePilot 路径 => Emby 路径`，每行一条 |
| 全量扫描目录 | 空 | Emby 内部路径，留空扫描全部媒体库 |

### 路径映射

MoviePilot 与 Emby 常常通过不同的容器映射看到同一个目录。插件需要把 MoviePilot 的
入库路径转换成 Emby 内部路径才能找到条目；同时会反向转换，用于读取 STRM 文件内容
计算指纹（换源检测）。

```text
/media/strm => /data/media
/mnt/link   => /data/link
```

分隔符支持 `=>`、`|` 和 `:`（`:` 会跳过 Windows 盘符冒号）。如果两侧路径一致则无需配置。

## 工作原理

```text
STRM 入库
   ↓
（等待）Emby 扫描到新条目
   ↓
Items 查询定位条目（Path 过滤 → 文件名搜索 → 分页遍历兜底）
   ↓
POST /emby/Items/{id}/PlaybackInfo (IsPlayback=true)
   ↓
Emby 用 ffprobe 读取真实媒体信息
   ↓
回查条目校验 MediaInfo 是否完整 → 写入历史
```

媒体信息由 Emby 保存在自己的媒体库数据库（`data/library.db`）中，插件不写入任何伪造数据。

## 常见问题

**一直提示未找到对应条目**：Emby 还没扫描到新文件。开启 Emby 实时监控、启用官方
「媒体库服务器刷新」插件，或打开「找不到时扫描媒体库」，并适当增大「等待识别超时」。

**PlaybackInfo 成功但媒体信息仍不完整**：通常是 STRM 地址失效、302 服务无法返回真实
直链、pickcode 失效，或文件名中的 `#` 未编码成 `%23`。

**会下载完整视频吗**：不会。Emby 与 ffprobe 只读取探测所需的数据量。

**会重复探测吗**：不会。已有完整 MediaInfo 且 STRM 内容指纹未变化的条目直接跳过。

## 致谢

- [ado-uta/emby-strm-prewarmer](https://github.com/ado-uta/emby-strm-prewarmer)：原始实现与思路
- [jxxghp/MoviePilot](https://github.com/jxxghp/MoviePilot)：插件运行宿主

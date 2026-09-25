# MoviePilot-Plugins

Hotwill 的 MoviePilot 第三方插件仓库，目录与索引结构与官方仓库保持一致，可直接作为
MoviePilot 插件市场地址使用。

## 在 MoviePilot 中安装

1. 打开 MoviePilot「设定 → 插件 → 插件仓库」（或环境变量 `PLUGIN_MARKET`），添加仓库地址：

   ```text
   https://github.com/Hotwill/MoviePilot-Plugins
   ```

   多个仓库地址用英文逗号分隔。

2. 回到「插件」页面，刷新插件市场，搜索 **STRM媒体信息预热** 并安装。

3. 安装后在插件配置页开启插件、勾选 Emby 服务器，保存即可。

> 插件市场只读取仓库 `main` 分支。

## 插件列表

| 插件 | 说明 | 版本 |
| --- | --- | --- |
| [STRM媒体信息预热](./plugins.v3/strmprewarmer/README.md) | STRM 入库后立即让 Emby 探测真实媒体信息 | 1.0.0 |

## 仓库结构

```text
MoviePilot-Plugins/
├── package.json             # V1 索引
├── package.v2.json          # V2 索引
├── package.v3.json          # V3 索引
├── plugins/strmprewarmer/   # V1 宿主可加载的实现（与 V3 同源）
├── plugins.v2/strmprewarmer/# V2 宿主实现（与 V3 同源）
├── plugins.v3/strmprewarmer/# 插件源码，以此目录为准
├── icons/                   # 插件图标
├── scripts/sync_plugin.py   # 同步各代实现并校验索引版本
└── tests/v3/strmprewarmer/  # 单元测试
```

插件实现内部对宿主导入做了回退（优先 `app.sdk.*`，回退 V2 的 `app.core.*` / `app.helper.*`），
因此同一份源码可同时在 MoviePilot V2 与 V3 中运行；`plugins.v2/` 和 `plugins/` 由脚本镜像生成。

## 开发

```bash
# 修改 plugins.v3/<plugin>/ 后同步到其它代目录并校验索引
python3 scripts/sync_plugin.py

# 只校验（CI 用）
python3 scripts/sync_plugin.py --check

# 运行单元测试（无需 MoviePilot 宿主，测试内置宿主桩模块）
python3 -m pytest tests/v3 -q
```

## 许可证

MIT License。

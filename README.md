### MoviePilot-Plugins 本仓库地址

```
https://github.com/DecaChI/MoviePilot-Plugins
```

## 插件列表

### [OpenList Strm](./docs/openliststrm.md) · V3

> **无需挂载**，直接读取 OpenList 目录树生成 strm 文件，交由 MoviePilot 刮削入库。

不需要把 OpenList 挂载成 WebDAV / 本地盘，也不需要在 NAS 上额外跑挂载进程。
插件直接调用 OpenList HTTP API 递归遍历目录树。

- **视频生成 strm**（指向云盘直链），**字幕/NFO/封面下载为本地文件**（播放与刮削必需）
- **多任务行式配置**：一行一个任务，字段用 `|` 分隔，无需写 JSON
- **支持多个 OpenList 实例**：每个任务自带地址与凭据，互不影响
- **无关文件过滤**：内置 20 类垃圾目录 + 32 类无关文件（广告、临时文件、系统残留），可自定义
- **目录树持久化缓存**：实测 795 目录全量扫描 114s → 命中缓存 0.3s（约 361×）
- **删除操作全部需手动确认**：保存配置与定时任务不会删除任何文件，
  失效清理走「先检测 → 看清单 → 再确认」流程

需要 **MoviePilot v3.0.0 及以上**。

### [ANi Strm](./docs/anistrm.md) · V2

> 自动获取当季所有番剧，生成 strm 文件，MP 刮削入库，Emby 直接播放，免去下载。

基于 [honue/MoviePilot-Plugins](https://github.com/honue/MoviePilot-Plugins/tree/main/plugins/anistrm)
修改，增加了反代地址的替换功能。

## 安装

在 MoviePilot「设置 → 插件 → 插件市场」中添加本仓库地址：

```
https://github.com/DecaChI/MoviePilot-Plugins
```

然后搜索插件名安装即可。

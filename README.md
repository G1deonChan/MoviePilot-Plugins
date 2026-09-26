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
- 两类文件类型都可自定义
- **目录树持久化缓存**：实测 795 目录全量扫描 114s → 命中缓存 0.3s（约 361×）
- **多任务独立定时**：不同目录错峰执行，单次任务更短
- **失效 strm 检测与联动清理**：可选删除 strm / 硬链接 / 转移记录，删除前需确认
- 支持多规则扫描、包含/排除正则、手动触发、强制覆盖

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

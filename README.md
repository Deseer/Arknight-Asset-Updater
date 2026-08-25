# Arknights Resource Service

常驻同步、校验并解包明日方舟国服 Android 官方资源。项目借鉴
Haruki Sekai Asset Updater 的任务/阶段/单任务执行模型，但下载协议和解包后端均针对明日方舟实现。

资源采用内存直通：CDN 响应写入 `BytesIO`，ZIP 在内存展开，AB/BIN 字节直接进入
UnityPy + LZ4AK，只把导出物写入外置盘。USM 因 ffmpeg 需要路径，仅临时使用容器
`/dev/shm` 内存盘。不会长期保存 `.dat`、AB、BIN 或 USM。

## 数据来源与检测策略

- 路由发现：`https://ak-conf.hypergryph.com/config/prod/official/network_config`
- 版本：路由配置的 `hv` 模板，当前为
  `https://ak-conf.hypergryph.com/config/prod/official/Android/version`
- CDN：路由配置的 `hu`，不在代码中写死最终资源地址
- 默认每 5 秒条件请求版本文件；利用 `ETag`/`If-None-Match`，未变化返回 304
- 仅版本变化或本地同步未完成时获取 `hot_update_list.json`
- 网络错误指数退避，路由配置每小时刷新或失败时强制刷新

## 解包

`vendor/Ark-Unpacker` 固定在提交 `8b4101f36bc9ccb283fff272928c3f0b23583980`。
其 `ResolveAB.py` 向 UnityPy 注册明日方舟自定义 LZ4AK 解压函数，再导出图片、文本、音频、
Spine、Mesh 和 TypeTree；USM 使用其 `cu` 模式。上游 BSD-3-Clause 许可证保留在 vendor 目录。

## API

- `GET /healthz`
- `GET /v1/version`
- `POST /v1/assets/update`，body：`{"dry_run": false}`
- `GET /v1/jobs`
- `GET /v1/jobs/{id}`
- `POST /v1/jobs/{id}/cancel`
- Swagger：`http://127.0.0.1:18080/docs`

## 数据目录

容器把 `/Volumes/wd/ArkResourceService` 作为纯结果目录挂载到 `/output`。外置盘目录根部
直接是官方资源分类，如 `arts`、`audio`、`battle`、`chararts`，不再创建 `Unpacked`、
`Bundles`、`Downloads`、`State` 或 `Logs`。状态和日志保存在 Docker 命名卷
`ark-resource-state`。

导出优先使用 Unity 内部 container 语义路径。CDN 分包会合并到实际素材目录，构建标记
`[uc]`、`[ucp]`、`[pack]` 会被移除；同一路径的 Sprite 与 backing Texture2D 只导出
Sprite。命名卷中的 `State/unpacked_records.json` 记录官方路径、hash、MD5 和导出数量。

内存参数位于项目根目录 `.env`，Compose 和应用共同读取。修改后执行 `docker compose up -d`
即可重建限制。本机 16 GB、OrbStack VM 8 GB，默认应用在途预算
768 MB、下载 worker 2、无 Compose 容器内存硬上限、共享内存 768 MB。容器仍受
OrbStack VM 总内存约束。聚合包引导默认关闭，
每个官方资源 `.dat` 独立下载、内存解包并释放，以避免 Unity 解码时的叠加内存峰值。
调试用途的 TypeTree JSON 默认关闭；全量 Shader TypeTree 会在单个资源上产生超过 2 GB 的
内存峰值。图片、文本、音频、Spine 和 Mesh 仍正常导出。

```bash
docker compose up -d --build
curl http://127.0.0.1:18080/healthz
```

### Export types and output layout

`ARK_EXPORT_TYPES` is a comma-separated whitelist. Supported values are `image`,
`spine`, `text`, `audio`, `mesh`, `video`, `masterdata`, and `typetree`. The local
deployment currently enables every directly usable file export, while TypeTree stays
opt-in. Enabling it writes selected classes one-by-one below `metadata/`, controlled
by `ARK_TYPETREE_TYPES`. Shader TypeTrees remain excluded because a single Shader
tree can exceed the 2 GB container limit.

Unity objects are written using their internal container path instead of the CDN
bundle name. Leading `dyn/` and upstream build markers such as `[uc]`, `[ucp]`, and
`[pack]` are removed from the user-facing path. Shards such as
`spritepack/ui_char_avatar_0.ab` and `_17.ab` therefore merge into the semantic
`arts/charavatars/` directory. When the same path contains both a Sprite and its
backing Texture2D, only the Sprite is exported, avoiding the old `$0` duplicate.

Recognized anonymous game tables are decoded directly to `masterdata/*.json`; the
anonymous binary input is never written to the external disk.

`lipsync/voice*/*.bytes` contains small lip-animation timing data and is intentionally
kept as bytes. Spoken voice audio is separate under `audio/sound_beta_2/voice*/` and
exports as playable OGG/WAV files when those later manifest entries are processed.

This host uses two download workers without a Compose memory hard limit. The shared
768 MB `MemoryBudget` gates compressed bytes in flight, while decoded Unity objects
may use more memory and remain bounded by the OrbStack VM.
The Docker health probe runs once per 60 seconds and marks the service unhealthy only
after three consecutive failures.
Container logs emit `asset_start` and `asset_done` records with worker name, official
resource path, byte sizes, exported file count, and elapsed milliseconds.

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

容器把 `/Volumes/wd/ArkResourceService` 挂载到 `/data`。WD 只长期保留 `Unpacked`、
`State` 和 `Logs`；`Bundles`、`Downloads`、`Queue` 仅用于旧版本数据迁移，迁移完成后为空。

导出结构固定为 `Unpacked/resources/<官方清单资源路径去掉扩展名>/...`，例如
`battle/enm_pfb_24.ab` 对应 `Unpacked/resources/battle/enm_pfb_24/`。每个资源先写入
`.staging`，全部成功后整目录原子替换，避免新版与旧版文件混杂。`State/unpacked_records.json`
记录官方路径、hash、MD5、输出目录和导出数量。

内存参数位于项目根目录 `.env`，Compose 和应用共同读取。修改后执行 `docker compose up -d`
即可重建限制。本机 16 GB、OrbStack VM 8 GB，默认应用在途预算
768 MB、下载并发 2、容器硬上限 2 GB、共享内存 768 MB。

```bash
docker compose up -d --build
curl http://127.0.0.1:18080/healthz
```

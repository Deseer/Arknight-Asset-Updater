# Arknights Resource Service

常驻同步、校验并解包明日方舟国服 Android 官方资源。项目借鉴
Haruki Sekai Asset Updater 的任务/阶段/单任务执行模型，但下载协议和解包后端均针对明日方舟实现。

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

容器把 `/Volumes/wd/ArkResourceService` 挂载到 `/data`。资源原包、临时下载、解包输出、
持久化任务和日志分别位于 `Bundles`、`Downloads`、`Unpacked`、`State`、`Logs`。

```bash
docker compose up -d --build
curl http://127.0.0.1:18080/healthz
```

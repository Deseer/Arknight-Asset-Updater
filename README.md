# Arknight Asset Updater

通过游戏 CDN 自动下载并更新资源，在内存中完成解包，只保存导出结果。

## 部署

```bash
git clone --recurse-submodules https://github.com/Deseer/Arknight-Asset-Updater.git
cd Arknight-Asset-Updater
cp .env.example .env
docker compose up -d --build
```

运行参数位于 `.env` 和 `config/service.example.json`。私有配置应保存在仓库外，
并通过 `ARK_SERVICE_CONFIG_PATH` 指定。

导出结果默认保存到：

```text
/Volumes/wd/ArkResourceService
```

## 来源与许可证

本项目代码采用 MIT License。

解包部分基于 [isHarryh/Ark-Unpacker](https://github.com/isHarryh/Ark-Unpacker)，
该部分采用 BSD-3-Clause License，详情见 `THIRD_PARTY_NOTICES.md`。

## FlatBuffers schema updates

The service checks `ArknightsAssets/ArknightsFlatbuffers` every 30 minutes, including when no new game resource version is published. `ARK_SCHEMA_POLL_SECONDS` controls this interval (minimum 60 seconds). `ARK_SCHEMA_PROXY` optionally supplies a proxy for schema traffic only; game CDN traffic is unchanged.

All CN schemas are fetched at one immutable upstream commit, compiled with `flatc`, imported and checksum-recorded in `/state/Schemas`. Only a complete compiled snapshot is activated. Network/compilation failures retain the previous snapshot (or bundled schemas) and retry after five minutes. `/v1/config` exposes the revision, table count and last error type. This verifies compilation and JSON encoding; an upstream schema may still be semantically incompatible with a particular game version.

Successful resource records retain hashes of the schemas used to decode their tables. Changed table schemas invalidate those records without invalidating unrelated assets. Known schema failures are persisted as terminal skips until a schema update permits retry. Exports reject invalid UTF-8/surrogates and non-finite JSON values before replacing canonical tables. Bundled schema sources are pinned and checksum-verified for offline image builds; automatic updates are cached separately in the state volume.

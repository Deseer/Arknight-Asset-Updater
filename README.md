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

## 历史资源补档

`scripts/backfill_levels.py` 会扫描 MasterData 中有效的 `LevelId`，对比本地 `.bytes` 和
`.json`，从固定提交的 `Kengxxiao/ArknightsGameData` 下载缺失的完整关卡 JSON。
每份存档均校验 Git blob 哈希、UTF-8、地图尺寸与地块索引，并保存来源和 SHA-256。
它只下载到暂存目录；简化地图、测试标识和无完整来源的关卡会在报告中单独列出。

```bash
python scripts/backfill_levels.py --root /path/to/resources --work-dir /path/to/backfill \
  --revision <full-source-commit-sha>
python scripts/publish_backfill.py --source /path/to/backfill/staged \
  --output /path/to/resources/gamedata/levels --report /path/to/level-publication.json --publish
```

`scripts/backfill_resources.py` 在服务环境中接受多个 `--version <resVersion>`（新版本优先），
对比当前清单并去重下载已移除的包，先解包到指定 `--work-dir`。它不会改动当前版本清单、
解包记录、Schema 或生产输出。CDN 是否仍保存旧清单和旧包需要逐项验证。
历史解包应通过独立容器运行，避免占用在线更新进程的内存；旧 MasterData 保留原始
TextAsset，不使用最新 Schema 强行解码成生产表。

```bash
docker compose build ark-resource-service
docker compose run --rm --no-deps --entrypoint python ark-resource-service \
  scripts/backfill_resources.py --version <old-resVersion> \
  --work-dir /output/.historical-backfill/example --workers 1
```

清单中只有完整 32 位 MD5 才进行 MD5 比对，短校验字段会明确记录为未验证 MD5。
无法解包或没有可导出对象的包会保存在补档目录的 `raw/<version>/`，记录包 SHA-256
及解包错误类型，供后续解码；不会伪装成已恢复的图片或关卡数据。

`scripts/publish_backfill.py` 可以合并多个 `--source`，默认仅生成计划，加 `--publish`
才实际写入。已有文件一律保留，内容冲突单独记录，MasterData 和隐藏文件不合并；
新文件完整写入后原子发布并核对 SHA-256。发布报告记录每个新增文件，供核查和回退。
历史包可能已被 CDN 清理，公开存档也可能不完整，因此补档报告不代表历史版本的全量资源。

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

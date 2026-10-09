# Third-Party Notices

The MIT license at the repository root applies to the original service code in
this repository. It does not replace the licenses of third-party components.

## Ark-Unpacker

- Project: `isHarryh/Ark-Unpacker`
- Source: <https://github.com/isHarryh/Ark-Unpacker>
- Pinned revision: `8b4101f36bc9ccb283fff272928c3f0b23583980`
- License: BSD 3-Clause
- Local path: `vendor/Ark-Unpacker`

Ark-Unpacker supplies the Arknights-specific Unity bundle, Spine and
FlatBuffers decoding backend. Its original copyright notice, license
conditions and disclaimer are retained in `vendor/Ark-Unpacker/LICENSE` and
included in built images at `/licenses/Ark-Unpacker-LICENSE`.

The project name and contributor names are used only for attribution and do
not imply endorsement.

## ArknightsFlatbuffers schema snapshots

- Source: <https://github.com/ArknightsAssets/ArknightsFlatbuffers>
- Bundled revision: `7855e1ab8b7b122d44930c7d3a754a8e2fc39fdd`
- Local path: `scripts/flatbuffer_schemas`

The bundled CN schemas retain the upstream contents. Their SHA-256 checksums
are verified by `scripts/refresh_flatbuffer_schemas.py` before compilation.
These third-party schema files are not covered by this service's MIT license.
Runtime schema updates come from the same upstream repository and are stored
outside the source tree in the service state volume.

## cridecoder

- Project: `Team-Haruki/cridecoder`
- Source: <https://github.com/Team-Haruki/cridecoder>
- Version: `0.3.4`
- License: MIT

cridecoder is used through its public in-memory API to extract CRI USM streams.

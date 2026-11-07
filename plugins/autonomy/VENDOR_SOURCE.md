# Autonomy runtime vendor provenance

Imported on 2026-11-07 from the production-validated Ubuntu plugin trees on `192.168.10.222`.

Source directories:

- `/home/hermes/.hermes/profiles/xiaoai/plugins/autonomy.pre-p0-20261106T120540Z/_vendor`
- `/home/hermes/.hermes/profiles/xiaozhi/plugins/autonomy.pre-p0-20261106T122533Z/_vendor`

The two profile trees were independently copied and verified path-by-path and SHA-256-by-SHA-256 against their remote sources:

- source tree: 1,409 files, 72,148,559 bytes (includes `__pycache__` and `.pyc`)
- remote/local verification: `only_remote=0`, `only_local=0`, `content_diff=0`
- xiaoai and xiaozhi content manifests are identical
- copied local tree manifests SHA-256: `A831EE8B5CDC015DD8958AB6044A7DF2266044E8BEF324D75D79314C5C3211E2`
- xiaoai transfer archive SHA-256: `6B89AC27D498613837F583918477B89BF4D5C2F898F4EDF11CEA8395734017FD`
- xiaozhi transfer archive SHA-256: `DE94E112242777CD70829F457AC0E1BBCB4A279CA4AF69ADC9D2B880C89D45A4`

This repository and release bundles intentionally omit reproducible `__pycache__` directories and `.pyc` files. The effective tracked/released dependency tree before this provenance file is 956 files and 59,258,258 bytes. No `_vendor_ml`, `models`, `model`, `assets`, or `data` runtime dependency directory existed beside `_vendor` in either validated source plugin.

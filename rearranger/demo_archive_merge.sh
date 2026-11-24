#!/usr/bin/env bash
# 多归档固定块去重合并的端到端演示：
#   1) 造带重复块的示例归档与请求；
#   2) 合并成“镜像 + 清单”归档包并核验映射；
#   3) 核验归档包；
#   4) 破坏镜像后核验被拒、重开合并拒绝覆盖既有输出；
#   5) 干净重建后重开为幂等完成。
# 仅用系统 python3（以 -I 隔离模式运行），不依赖第三方库。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
REQ="$WORK/request.json"
PKG="$WORK/out.pkg"

echo "== 1) 造 3 个归档（块 16 字节，含跨归档重复块）并生成请求 =="
python3 -I "$HERE/make_merge_request.py" --sample --block-size 16 --blocks 6 \
    --archives 3 --seed 7 --request "$REQ" --sample-dir "$WORK/sample"
echo

echo "== 2) 合并并统计来源槽位与镜像不同块 =="
python3 -I "$HERE/archive_merge.py" merge --request "$REQ" --output "$PKG"
ls -l "$PKG"
python3 -I - "$PKG" <<'PY'
import json, sys
m = json.load(open(sys.argv[1] + "/manifest.json"))
for a in m["archives"]:
    print("  归档 %-12s 原槽位 -> 镜像槽位: %s"
          % (a["id"], a["slot_map"]))
print("  镜像共 %d 个不同块（%d 字节）"
      % (m["image"]["slots"], m["image"]["size"]))
PY
echo

echo "== 3) 按原请求全量核验归档包 =="
python3 -I "$HERE/archive_merge.py" verify --request "$REQ" --package "$PKG"
echo

echo "== 4) 破坏镜像一个字节：核验与重开都必须拒绝 =="
python3 -I - "$PKG/image.bin" <<'PY'
import sys
p = sys.argv[1]
with open(p, "r+b") as f:
    b = f.read(1)
    f.seek(0)
    f.write(b"\x00" if b != b"\x00" else b"\x01")
PY
python3 -I "$HERE/archive_merge.py" verify --request "$REQ" --package "$PKG" \
    && { echo "verify 本不该通过"; exit 1; } || echo "   verify 已拒绝（符合预期）"
python3 -I "$HERE/archive_merge.py" merge --request "$REQ" --output "$PKG" \
    && { echo "merge 本不该覆盖"; exit 1; } || echo "   merge 拒绝覆盖（符合预期）"
echo

echo "== 5) 干净重建，再开一次应报告幂等完成 =="
rm -rf "$PKG"
python3 -I "$HERE/archive_merge.py" merge --request "$REQ" --output "$PKG"
python3 -I "$HERE/archive_merge.py" merge --request "$REQ" --output "$PKG"
python3 -I "$HERE/archive_merge.py" verify --request "$REQ" --package "$PKG"
echo
echo "演示完成。"

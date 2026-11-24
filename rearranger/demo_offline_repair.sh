#!/usr/bin/env bash
# 归档包离线修复端到端演示：
#   1) 合并出一个健康归档包；
#   2) 用矩形分组表制作修复资料（两份异或校验载荷 + 描述文件），并把描述文件
#      SHA-256 留存到“介质之外”（这里是另一个文件，实际应由操作员单独保管）；
#   3) 模拟坏盘：翻转块、把镜像截断成只剩前缀、删除清单；
#   4) 不信任坏盘清单，仅凭描述文件 + 留存摘要 + 两份校验载荷离线重建；
#   5) 校验重建包与迁移前逐字节相同，且原读取系统可直接核验；
#   6) 再演示关系不足时明确失败、绝不交付半成品。
# 仅用系统 python3（以 -I 隔离模式运行），不依赖第三方库。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
REQ="$WORK/request.json"
PKG="$WORK/healthy.pkg"
GROUPS="$WORK/groups.json"
KIT="$WORK/repair-kit"
DIGEST_OFFMEDIA="$WORK/offmedia-descriptor.sha256"

echo "== 1) 合并健康归档包 =="
python3 -I "$HERE/make_merge_request.py" --sample --block-size 16 --blocks 6 \
    --archives 3 --seed 7 --request "$REQ" --sample-dir "$WORK/sample"
python3 -I "$HERE/archive_merge.py" merge --request "$REQ" --output "$PKG"
cp -r "$PKG" "$WORK/reference.pkg"
echo

echo "== 2) 生成矩形分组表（rows / columns 各自覆盖全部镜像槽且互不重叠） =="
python3 -I - "$PKG" "$GROUPS" <<'PY'
import json, sys
pkg, out = sys.argv[1], sys.argv[2]
n = json.load(open(pkg + "/manifest.json"))["image"]["slots"]
side = 1
while side * side < n:
    side += 1
rows = [[s for s in range(n) if s % side == r] for r in range(side)]
cols = [[s for s in range(n) if s // side == c] for c in range(side)]
# 去掉可能出现的空组（n 不是完全平方数时）
rows = [g for g in rows if g]
cols = [g for g in cols if g]
json.dump({"rows": rows, "columns": cols}, open(out, "w"))
print("  镜像槽数=%d，行组 %d 个、列组 %d 个" % (n, len(rows), len(cols)))
PY

echo "== 3) 制作修复资料（健康包自洽核验通过后才交付） =="
python3 -I "$HERE/offline_repair.py" make-kit \
    --package "$PKG" --groups "$GROUPS" --out "$KIT" \
    --digest-file "$DIGEST_OFFMEDIA"
echo "  修复资料目录："; ls -1 "$KIT"
echo "  介质之外留存的描述文件摘要：$(cat "$DIGEST_OFFMEDIA")"
echo

echo "== 4) 模拟坏盘：翻转两个块、镜像截断为前缀、删除清单 =="
python3 -I - "$PKG" <<'PY'
import os, sys
p = sys.argv[1] + "/image.bin"
data = bytearray(open(p, "rb").read())
n = len(data) // 16
assert n > 4, "演示数据应至少有 5 个块"
data[0] ^= 0xFF          # 槽 0 内容翻转
data[3 * 16] ^= 0xFF     # 槽 3 内容翻转
# 只保留前 4 个块的前缀：槽 4、5 成为缺槽
open(p, "wb").write(bytes(data[: 4 * 16]))
os.unlink(sys.argv[1] + "/manifest.json")    # 清单整个丢失
print("  损坏后镜像大小 %d 字节（原 %d），清单已删除"
      % (os.path.getsize(p), n * 16))
PY

echo "== 5) 离线重建（不信任坏盘清单，只信描述文件 + 留存摘要 + 校验载荷） =="
OUT="$WORK/repaired.pkg"
python3 -I "$HERE/offline_repair.py" repair \
    --descriptor "$KIT/repair_desc.json" \
    --digest "$(cat "$DIGEST_OFFMEDIA")" \
    --old-image "$PKG/image.bin" \
    --rows-parity "$KIT/repair_rows.bin" \
    --cols-parity "$KIT/repair_cols.bin" \
    --output "$OUT"

echo "== 6) 与迁移前逐字节比对，并交原读取系统核验 =="
cmp "$OUT/image.bin"    "$WORK/reference.pkg/image.bin"    && echo "  image.bin 逐字节相同"
cmp "$OUT/manifest.json" "$WORK/reference.pkg/manifest.json" && echo "  manifest.json 逐字节相同"
python3 -I "$HERE/archive_merge.py" verify --request "$REQ" --package "$OUT"
echo "  原包与修复资料均未被改写：$([ ! -e "$PKG/manifest.json" ] && echo 坏盘清单仍缺失)"
echo

echo "== 7) 关系不足场景：删掉全部镜像块，方程数 < 未知槽数 -> 明确失败 =="
rm -f "$WORK/empty-image.bin"
set +e
python3 -I "$HERE/offline_repair.py" repair \
    --descriptor "$KIT/repair_desc.json" \
    --digest "$(cat "$DIGEST_OFFMEDIA")" \
    --old-image "$WORK/empty-image.bin" \
    --rows-parity "$KIT/repair_rows.bin" \
    --cols-parity "$KIT/repair_cols.bin" \
    --output "$WORK/must-not-exist.pkg"
rc=$?
set -e
if [ "$rc" -eq 8 ] && [ ! -e "$WORK/must-not-exist.pkg" ]; then
    echo "  已按退出码 8 拒绝，且最终路径没有半成品（符合预期）"
else
    echo "意外结果 rc=$rc"; exit 1
fi
echo
echo "演示完成。"

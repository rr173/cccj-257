#!/usr/bin/env bash
# 小文件复现“块内短写中断 -> 重开同一作业续接到正确新布局”。
# 不依赖任何第三方库，仅用系统 python3（以 -I 隔离模式运行）。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
DATA="$WORK/data.bin"
JOB="$WORK/job.json"
SIDE="$WORK/side"

echo "== 1) 造 8 个 16 字节的块，布局含一个环 (0->1->2->0) 和树节点 =="
python3 -I "$HERE/make_job.py" --sample "$DATA" --blocks 8 --block-size 16 \
    --layout random --seed 3 --job "$JOB"
python3 -I - "$DATA" <<'PY'
import sys
raw = open(sys.argv[1], "rb").read()
for i in range(0, len(raw), 16):
    print("  旧槽 %d: %r" % (i // 16, raw[i:i + 16]))
PY

echo
echo "== 2) 首次运行，在第 2 次数据块 pwrite 上强制短写后杀掉进程 =="
# 规则 data:pwrite:2:short:3：第 2 次载荷写只落 3 字节（块内短写），随后循环补齐；
# 再叠加 data:fsync:2:exit：同一步 fsync 前直接退出，制造持久化的撕裂状态。
python3 -I "$HERE/rearrange.py" run --data "$DATA" --job "$JOB" --sidecar "$SIDE" \
    --fault data:pwrite:2:short:3 --fault data:fsync:2:exit || true
echo "   进程已退出，数据盘上保留断点（journal + 可能撕裂的一个槽）。"
ls -1 "$SIDE"

echo
echo "== 3) 不带故障重开同一作业，自动续接 =="
python3 -I "$HERE/rearrange.py" run --data "$DATA" --job "$JOB" --sidecar "$SIDE"

echo
echo "== 4) 按完成凭据全量核验 =="
python3 -I "$HERE/rearrange.py" verify --data "$DATA" --job "$JOB" --sidecar "$SIDE"

echo
echo "== 5) 再开同一作业：不会再执行一遍布局 =="
python3 -I "$HERE/rearrange.py" run --data "$DATA" --job "$JOB" --sidecar "$SIDE"

echo
echo "== 6) 最终各槽内容（权威核验以上一步 verify 为准） =="
python3 -I - "$DATA" <<'PY'
import sys
raw = open(sys.argv[1], "rb").read()
for i in range(0, len(raw), 16):
    print("  新槽 %d: %r" % (i // 16, raw[i:i + 16]))
PY
echo
echo "演示完成。"

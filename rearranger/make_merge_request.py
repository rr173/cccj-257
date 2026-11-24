#!/usr/bin/env python3
"""归档合并请求生成助手：按块缓冲计算摘要，不整读文件。

用法：
  # 用若干已有数据文件生成一份合并请求
  python3 -I make_merge_request.py --block-size 4096 \
      --archive id=alpha:path=a.bin --archive id=beta:path=b.bin \
      --request request.json

  # 造示例数据并生成请求（含跨归档重复块，便于观察去重）
  python3 -I make_merge_request.py --sample --block-size 16 --blocks 6 \
      --archives 3 --request request.json --sample-dir sample/

数据文件路径写入请求时保持调用方给出的原样（相对路径以请求文件所在目录
为准解析），摘要在生成时按当前文件内容计算。
"""

import argparse
import hashlib
import json
import os
import random
import sys


def digest_file_blocks(path, block_size):
    digests = []
    total = 0
    fd = os.open(path, os.O_RDONLY)
    try:
        while True:
            buf = b""
            while len(buf) < block_size:
                chunk = os.pread(fd, block_size - len(buf), total + len(buf))
                if not chunk:
                    break
                buf += chunk
            if not buf:
                break
            if len(buf) != block_size:
                raise SystemExit("文件尾部不足一个整块，剩余 %d 字节" % len(buf))
            digests.append(hashlib.sha256(buf).hexdigest())
            total += block_size
    finally:
        os.close(fd)
    return digests


def parse_archive_spec(spec):
    fields = {}
    for part in spec.split(":"):
        if "=" not in part:
            raise SystemExit("--archive 需要 key=value 形式: %r" % spec)
        k, v = part.split("=", 1)
        fields[k.strip()] = v
    for key in ("id", "path"):
        if key not in fields or not fields[key]:
            raise SystemExit("--archive 缺少 %s: %r" % (key, spec))
    return fields["id"], fields["path"]


def main():
    ap = argparse.ArgumentParser(description="生成归档合并请求 JSON")
    ap.add_argument("--request", required=True)
    ap.add_argument("--block-size", type=int, required=True)
    ap.add_argument("--archive", action="append", default=[],
                    help="id=<归档ID>:path=<数据文件>，按合并顺序重复给出")
    ap.add_argument("--sample", action="store_true",
                    help="先生成示例数据文件，再据其生成请求")
    ap.add_argument("--sample-dir", default="sample")
    ap.add_argument("--archives", type=int, default=3,
                    help="--sample 时生成的归档数")
    ap.add_argument("--blocks", type=int, default=6,
                    help="--sample 时每个归档的块数")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    if bool(args.archive) == bool(args.sample):
        ap.error("必须且只能指定 --archive 或 --sample")
    if args.block_size <= 0:
        ap.error("--block-size 必须是正整数")

    entries = []
    if args.sample:
        os.makedirs(args.sample_dir, exist_ok=True)
        rng = random.Random(args.seed)
        # 小池子制造跨归档与归档内重复，其余为随机唯一块
        pool = []
        for i in range(4):
            pool.append(("pool%02d-" % i).encode().ljust(
                args.block_size, b"#")[:args.block_size])
        for a in range(args.archives):
            name = "archive-%02d.bin" % a
            path = os.path.join(args.sample_dir, name)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                for b in range(args.blocks):
                    if rng.random() < 0.6:
                        blk = rng.choice(pool)
                    else:
                        blk = rng.randbytes(args.block_size)
                    blk = blk[:args.block_size].ljust(args.block_size, b"#")
                    os.write(fd, blk)
                os.fsync(fd)
            finally:
                os.close(fd)
            entries.append(("archive-%02d" % a, path))
    else:
        for spec in args.archive:
            entries.append(parse_archive_spec(spec))

    archives = []
    for aid, path in entries:
        if os.path.getsize(path) % args.block_size != 0:
            raise SystemExit("文件 %s 大小不是块长的整数倍" % path)
        archives.append({
            "id": aid,
            "path": path,
            "digests": digest_file_blocks(path, args.block_size),
        })

    request = {
        "version": 1,
        "block_size": args.block_size,
        "archives": archives,
    }
    with open(args.request, "w", encoding="utf-8") as f:
        json.dump(request, f, ensure_ascii=False, indent=2)
    total_slots = sum(len(a["digests"]) for a in archives)
    print("已生成请求 %s：%d 个归档，共 %d 个来源槽位。"
          % (args.request, len(archives), total_slots))


if __name__ == "__main__":
    main()

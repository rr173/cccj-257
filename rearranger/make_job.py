#!/usr/bin/env python3
"""作业描述生成助手（按块缓冲计算原始摘要，不整读文件）。

两种用法：
  # 生成一个 N 块的随机示例数据文件，并配套作业描述
  python3 -I make_job.py --sample data.bin --blocks 10 --block-size 16 \
      --layout reverse --job job.json

  # 对已有的、块长整数倍的数据文件，生成带原始摘要的作业骨架
  python3 -I make_job.py --from-file data.bin --block-size 4096 \
      --layout identity --job job.json

--layout:
  identity : 每个目标槽取同号旧槽（自环）
  reverse  : 目标槽 t 取旧槽 N-1-t
  random   : 随机函数布局（允许多对一、可无去处），可用 --seed 复现
生成后可手工编辑 job.json 的 layout，再用 run 子命令执行。
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


def main():
    ap = argparse.ArgumentParser(description="生成重排作业描述 JSON")
    ap.add_argument("--sample", help="生成随机示例数据文件到该路径")
    ap.add_argument("--from-file", help="对已有数据文件生成作业骨架")
    ap.add_argument("--blocks", type=int, default=10)
    ap.add_argument("--block-size", type=int, required=True)
    ap.add_argument("--layout", choices=["identity", "reverse", "random"],
                    default="identity")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--job", required=True)
    args = ap.parse_args()

    if bool(args.sample) == bool(getattr(args, "from_file")):
        ap.error("必须且只能指定 --sample 或 --from-file 之一")

    if args.sample:
        n = args.blocks
        rng = random.Random(args.seed)
        fd = os.open(args.sample, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            for i in range(n):
                os.write(fd, ("blk%04d-" % i).encode().ljust(
                    args.block_size, b"#")[:args.block_size])
            os.fsync(fd)
        finally:
            os.close(fd)
        data_path = args.sample
    else:
        data_path = getattr(args, "from_file")
        if os.path.getsize(data_path) % args.block_size != 0:
            ap.error("文件大小不是块长的整数倍")
        n = os.path.getsize(data_path) // args.block_size

    old_digests = digest_file_blocks(data_path, args.block_size)
    assert len(old_digests) == n

    if args.layout == "identity":
        layout = list(range(n))
    elif args.layout == "reverse":
        layout = list(range(n - 1, -1, -1))
    else:
        rng = random.Random(args.seed)
        layout = [rng.randrange(n) for _ in range(n)]

    job = {
        "version": 1,
        "block_size": args.block_size,
        "layout": layout,
        "old_digests": old_digests,
        "digest_alg": "sha256",
    }
    with open(args.job, "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False, indent=2)
    print("已生成作业描述 %s：%d 个槽，布局 %s"
          % (args.job, n, args.layout))


if __name__ == "__main__":
    main()

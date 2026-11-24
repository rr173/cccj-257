#!/usr/bin/env python3
"""归档包离线修复：制作修复资料与按行列异或校验重建归档包。

背景：合并后的固定块归档包迁入冷存储后，原始来源归档下线，损坏盘上的
image.bin 可能出现内容翻转、短块（只剩文件前缀）、缺槽，manifest.json
甚至可能整个丢失。本模块在不依赖任何已下线来源文件的前提下：

  make-kit（迁移时，操作员持有健康归档包与分组表）：
    * 先对归档包做脱离原请求的自洽核验（清单签名、镜像逐槽 SHA-256、
      整体摘要、去重、来源映射），不通过绝不交付修复资料；
    * 按 rows / columns 两个互不重叠的分组，各生成一份逐块异或校验载荷
      （一个组占一个块，按本侧组号顺序存放）；
    * 生成描述文件：原清单的精确字节、分组、两侧校验块与各原物理槽的
      SHA-512，并给操作员一个可带离介质的描述文件 SHA-256。

  repair（故障后，操作员持描述文件、留存摘要、旧镜像与两份校验载荷）：
    * 先校验描述文件的留存 SHA-256 与自身签名/结构，绝不信任坏盘清单；
    * 旧镜像只读一轮：缺失、短块、缺槽、SHA-512 不符一律视为擦除，
      超过封存长度或其他 I/O 错误直接拒绝；
    * 块身份始终是封存时的槽号：只按槽号写入与核验，绝不按内容摘要
      互相顶替；
    * 把“仍可核验的校验块”看成组上的异或方程，在 行组-槽-列组 构成的
      二部图上做叶剥离：某组只剩一个未知槽即可定出该槽，连锁还原整条
      坏槽链；剥离后仍有未知槽，说明关系不足、解不唯一，明确失败；
    * 重建块逐槽通过封存 SHA-512，全部好校验关系复核无矛盾后，才把
      与迁移前逐字节相同的归档包（原清单字节、镜像尺寸、槽顺序、来源
      映射不变）原子发布到一个此前不存在的路径。

载荷内存不随镜像槽数或分组数增长：任何时刻只保留若干 block_size 大小
的缓冲，不缓存整份镜像或整份校验载荷；随规模增长的只有摘要、分组成员、
未知计数等非载荷元数据。
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import shutil
import signal
import stat
import sys
import tempfile
from collections import deque

# python3 -I 不把脚本目录自动加入 sys.path；显式加入以保证独立运行时
# 能导入同目录的 archive_merge（同时不影响被 importlib 加载的场景）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import archive_merge as am  # noqa: E402

# ---------------------------------------------------------------------------
# 常量与退出码（沿用 archive_merge 的既有码位语义）
# ---------------------------------------------------------------------------
EXIT_OK = am.EXIT_OK
EXIT_USAGE = am.EXIT_USAGE
EXIT_INPUT = am.EXIT_INPUT                # 3 描述文件/分组/输入结构核验失败
EXIT_PACKAGE_INVALID = am.EXIT_PACKAGE_INVALID  # 4 健康包自洽核验失败
EXIT_IO = am.EXIT_IO                      # 5 介质 IO 错误/超长文件
EXIT_LOCK = am.EXIT_LOCK                  # 6 输出被独占
EXIT_OUTPUT_EXISTS = am.EXIT_OUTPUT_EXISTS  # 7 输出路径已存在
EXIT_REPAIR_FAILED = 8                    # 关系不足/互相矛盾/还原字节不符

DESCRIPTOR_FORMAT = "fixed-block-archive-repair-descriptor"
DESCRIPTOR_VERSION = 1
ROWS_PARITY_NAME = "repair_rows.bin"
COLS_PARITY_NAME = "repair_cols.bin"
DESCRIPTOR_NAME = "repair_desc.json"

_HEX = am._HEX


class KitError(ValueError):
    """制作修复资料的输入不合法（分组非法等）。"""


class DescriptorError(ValueError):
    """描述文件与留存摘要不符，或内容结构无效。"""


class RepairInputError(ValueError):
    """修复输入不被接受（超长文件等）。"""


class RepairFailed(Exception):
    """校验关系不足、互相矛盾或重建字节无法通过封存校验。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha512_hex(data: bytes) -> str:
    return hashlib.sha512(data).hexdigest()


def _norm_hex(value, length, where):
    if not isinstance(value, str):
        raise DescriptorError("%s 必须是十六进制字符串" % where)
    s = value.lower()
    if len(s) != length or any(c not in _HEX for c in s):
        raise DescriptorError("%s 不是合法的 %d 位十六进制摘要" % (where, length))
    return s


def _norm_int(value, where, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise DescriptorError("%s 必须是 >= %d 的整数" % (where, minimum))
    return value


def _xor_into(acc: bytearray, buf: bytes) -> None:
    """把 buf 逐字节异或进 acc（两者等长）；只复用一块缓冲。"""
    for i, b in enumerate(buf):
        acc[i] ^= b


def _read_regular(path, what):
    """读取小元数据文件，拒绝符号链接/FIFO/设备。"""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
                     | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise DescriptorError("无法读取%s %s: %s" % (what, path, exc))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise DescriptorError("%s不是普通文件: %s" % (what, path))
        return am._pread_all(fd, 0, st.st_size)
    finally:
        os.close(fd)


def _output_paths(output_path):
    """离线修复自己的暂存前缀与锁文件名（与合并工具区分，互不影响）。"""
    name = os.path.basename(os.path.normpath(output_path))
    parent = os.path.dirname(os.path.normpath(output_path)) or "."
    return (
        os.path.join(parent, ".offline_repair.%s.tmp." % name),
        os.path.join(parent, ".offline_repair.%s.lock" % name),
    )


def _acquire_output_lock(output_path):
    _prefix, lock_path = _output_paths(output_path)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
            raise BlockingIOError(exc.errno, str(exc))
        raise
    return fd


# ---------------------------------------------------------------------------
# 健康归档包的脱离请求自洽核验
#
# 制作修复资料时原请求不一定在场，归档包必须靠清单自身就能证明
# “清单、镜像、映射一致”。检查项与 archive_merge._verify_norm 对齐，
# 只是不再绑定合并请求。
# ---------------------------------------------------------------------------


def verify_package_standalone(package_path):
    """返回 (manifest_dict, 清单原始字节)；任何不一致抛 am.PackageError。"""
    if not os.path.isdir(package_path):
        raise am.PackageError("归档包不存在或不是目录: %s" % package_path)
    mpath = os.path.join(package_path, am.MANIFEST_NAME)
    raw = _read_regular(mpath, "清单")
    try:
        d = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise am.PackageError("清单不是合法 UTF-8 JSON: %s" % exc)
    if not isinstance(d, dict):
        raise am.PackageError("清单必须是 JSON 对象")
    sig = d.pop("sig", None)
    if not isinstance(sig, str) or sig != sha256_hex(am._canonical_bytes(d)):
        raise am.PackageError("清单签名不符：清单被改动或已损坏")

    if d.get("format") != am.PACKAGE_FORMAT:
        raise am.PackageError("清单格式标识不符")
    if d.get("version") != am.PACKAGE_VERSION:
        raise am.PackageError("不支持的清单版本: %r" % d.get("version"))
    bs = _norm_int(d.get("block_size"), "block_size", minimum=1)

    image = d.get("image")
    if not isinstance(image, dict):
        raise am.PackageError("清单缺少 image 信息")
    if image.get("file") != am.IMAGE_NAME:
        raise am.PackageError("清单 image.file 必须为 %r" % am.IMAGE_NAME)
    slots = _norm_int(image.get("slots"), "image.slots")
    if image.get("size") != slots * bs:
        raise am.PackageError("清单 image.size 与槽位数/块长不一致")
    digests = am._manifest_sha_list(image.get("digests"), "image.digests")
    if len(digests) != slots:
        raise am.PackageError("清单 image.digests 数量与槽位数不一致")
    whole_want = am._norm_sha256(image.get("sha256"), "image.sha256") \
        if isinstance(image.get("sha256"), str) else None
    if whole_want is None:
        raise am.PackageError("清单 image.sha256 非法")

    img_path = am._manifest_path_for(package_path, image)
    try:
        fd = os.open(img_path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
                     | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise am.PackageError("无法读取镜像 %s: %s" % (img_path, exc))
    secondary = []
    whole = hashlib.sha256()
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size != slots * bs:
            raise am.PackageError("镜像文件大小不是 image.slots * block_size")
        for i in range(slots):
            buf = am._pread_all(fd, i * bs, bs)
            whole.update(buf)
            if sha256_hex(buf) != digests[i]:
                raise am.PackageError("镜像槽 %d 实测摘要与清单不符" % i)
            secondary.append(hashlib.blake2b(buf, digest_size=32).hexdigest())
        if whole.hexdigest() != whole_want:
            raise am.PackageError("镜像整体摘要与清单不符")
        # 去重保证：内容相同的块不得在镜像里出现两份
        groups = {}
        for i, key in enumerate(zip(digests, secondary)):
            groups.setdefault(key, []).append(i)
        for idxs in groups.values():
            if len(idxs) < 2:
                continue
            first = am._pread_all(fd, idxs[0] * bs, bs)
            for other in idxs[1:]:
                if am._pread_all(fd, other * bs, bs) == first:
                    raise am.PackageError(
                        "镜像槽 %d 与 %d 内容相同却未合并" % (idxs[0], other))
    finally:
        os.close(fd)

    archives = d.get("archives")
    if not isinstance(archives, list):
        raise am.PackageError("清单 archives 必须是列表")
    seen_ids = set()
    seen_slots = set()
    next_new = 0
    for i, meta in enumerate(archives):
        where = "archives[%d]" % i
        if not isinstance(meta, dict):
            raise am.PackageError("%s 必须是对象" % where)
        aid = meta.get("id")
        if not isinstance(aid, str) or not aid:
            raise am.PackageError("%s.id 必须是非空字符串" % where)
        if aid in seen_ids:
            raise am.PackageError("归档 ID 重复: %r" % aid)
        seen_ids.add(aid)
        want_slots = _norm_int(meta.get("slots"), "%s.slots" % where)
        if meta.get("size") != want_slots * bs:
            raise am.PackageError("%s.size 与槽位数不一致" % where)
        declared = am._manifest_sha_list(meta.get("digests"),
                                         "%s.digests" % where)
        if len(declared) != want_slots:
            raise am.PackageError("%s.digests 数量与槽位数不一致" % where)
        slot_map = meta.get("slot_map")
        if not isinstance(slot_map, list) or len(slot_map) != want_slots:
            raise am.PackageError("%s.slot_map 长度不符" % where)
        for s, m in enumerate(slot_map):
            if isinstance(m, bool) or not isinstance(m, int):
                raise am.PackageError("%s.slot_map[%d] 不是整数" % (where, s))
            if m < 0 or m >= slots:
                raise am.PackageError("%s.slot_map[%d] 越界: %d"
                                      % (where, s, m))
            if digests[m] != declared[s]:
                raise am.PackageError(
                    "%s 槽 %d 映射到的镜像槽 %d 内容摘要不符" % (aid, s, m))
            if m not in seen_slots:
                if m != next_new:
                    raise am.PackageError(
                        "镜像槽位顺序不是按首次出现编号（期望 %d 实际 %d）"
                        % (next_new, m))
                seen_slots.add(m)
                next_new += 1
    if next_new != slots:
        raise am.PackageError("镜像中存在未被任何来源槽位引用的槽")
    d["sig"] = sig
    return d, raw


# ---------------------------------------------------------------------------
# 分组表校验：两个有序列表都把 0..n-1 划分成互不重叠的非空组
# ---------------------------------------------------------------------------


def validate_grouping(obj, n):
    if not isinstance(obj, dict):
        raise KitError("分组表必须是 JSON 对象，含 rows / columns 两个列表")
    rows = obj.get("rows")
    cols = obj.get("columns")
    if not isinstance(rows, list) or not isinstance(cols, list):
        raise KitError("分组表的 rows / columns 必须是列表")

    def check(side, groups):
        if n == 0:
            if groups:
                raise KitError("空镜像的 %s 分组必须为空列表" % side)
            return []
        if not groups:
            raise KitError("%s 分组不能为空（镜像有 %d 个槽）" % (side, n))
        seen = set()
        norm = []
        for gi, group in enumerate(groups):
            where = "%s[%d]" % (side, gi)
            if not isinstance(group, list) or not group:
                raise KitError("%s 必须是非空列表" % where)
            cur = []
            for s in group:
                if isinstance(s, bool) or not isinstance(s, int):
                    raise KitError("%s 存在非整数槽号 %r" % (where, s))
                if s < 0 or s >= n:
                    raise KitError("%s 槽号 %d 越界（镜像共 %d 槽）"
                                   % (where, s, n))
                if s in seen:
                    raise KitError("%s 中槽号 %d 重复出现" % (side, s))
                seen.add(s)
                cur.append(s)
            norm.append(cur)
        if len(seen) != n or any(s not in seen for s in range(n)):
            missing = sorted(set(range(n)) - seen)
            raise KitError("%s 分组未覆盖全部镜像槽，缺失 %r" % (side, missing))
        return norm

    return check("rows", rows), check("columns", cols)


# ---------------------------------------------------------------------------
# 校验载荷生成：按组逐块异或，一个组一个块，流式单块缓冲
# ---------------------------------------------------------------------------


def _write_parity(image_fd, groups, bs, out_path, fault):
    """每组读其成员块（每次一块）异或成一个校验块并写盘；回读核验后留痕。"""
    fd = os.open(out_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o644)
    digests = []
    try:
        for gi, group in enumerate(groups):
            acc = bytearray(bs)
            for s in group:
                buf = am._pread_all(image_fd, s * bs, bs)
                _xor_into(acc, buf)
            payload = bytes(acc)
            am._pwrite_all(fd, gi * bs, payload, "image", fault)
            # 校验介质允许回读：落盘后立即回读比对，再封存 SHA-512
            rb = am._pread_all(fd, gi * bs, bs)
            if rb != payload:
                raise OSError("校验块 %d 回读与写入不一致" % gi)
            digests.append(sha512_hex(rb))
        os.fsync(fd)
    finally:
        os.close(fd)
    return digests


# ---------------------------------------------------------------------------
# make-kit
# ---------------------------------------------------------------------------


def make_repair_kit(package_path, grouping, out_dir, *, fault=am.NO_FAULT,
                    digest_file=None):
    """从健康归档包与分组表生成修复资料目录，返回描述信息字典。"""
    manifest, raw_manifest = verify_package_standalone(package_path)
    bs = int(manifest["block_size"])
    n = int(manifest["image"]["slots"])
    rows, cols = validate_grouping(grouping, n)

    out_dir = os.path.normpath(os.path.abspath(out_dir))
    parent = os.path.dirname(out_dir)
    name = os.path.basename(out_dir)
    if not name or name in (".", ".."):
        raise KitError("输出路径不能是根目录或空名称")
    if am._output_exists(out_dir):
        raise am.OutputExistsError("修复资料输出路径已存在，拒绝覆盖: %s"
                                   % out_dir)
    os.makedirs(parent, exist_ok=True)
    prefix, _lock = _output_paths(out_dir)
    lock_fd = _acquire_output_lock(out_dir)
    try:
        if am._output_exists(out_dir):
            raise am.OutputExistsError("修复资料输出路径已存在，拒绝覆盖: %s"
                                       % out_dir)
        am._clean_stale_staging(parent, prefix)
        staging = tempfile.mkdtemp(prefix=prefix, dir=parent)
        published = False
        try:
            img_path = os.path.join(package_path, am.IMAGE_NAME)
            image_fd = os.open(img_path, os.O_RDONLY
                               | getattr(os, "O_NONBLOCK", 0)
                               | getattr(os, "O_NOFOLLOW", 0))
            try:
                # 第一轮：逐槽 SHA-512 与整体 SHA-256（单块缓冲元数据化）
                slot_sha512 = []
                whole = hashlib.sha256()
                for s in range(n):
                    buf = am._pread_all(image_fd, s * bs, bs)
                    whole.update(buf)
                    slot_sha512.append(sha512_hex(buf))
                if whole.hexdigest() != manifest["image"]["sha256"]:
                    raise am.PackageError("镜像整体摘要与清单不符")

                # 第二、三轮：两份校验载荷分开生成（健康介质可读多轮）
                rows_digests = _write_parity(
                    image_fd, rows, bs,
                    os.path.join(staging, ROWS_PARITY_NAME), fault)
                cols_digests = _write_parity(
                    image_fd, cols, bs,
                    os.path.join(staging, COLS_PARITY_NAME), fault)
            finally:
                os.close(image_fd)

            body = {
                "format": DESCRIPTOR_FORMAT,
                "version": DESCRIPTOR_VERSION,
                "block_size": bs,
                "image": {
                    "slots": n,
                    "size": n * bs,
                    "sha256": manifest["image"]["sha256"],
                    "slot_sha512": slot_sha512,
                },
                "manifest": {
                    "sha256": sha256_hex(raw_manifest),
                    "bytes_hex": raw_manifest.hex(),
                },
                "groups": {
                    "rows": rows,
                    "columns": cols,
                },
                "parity": {
                    "rows": {
                        "file": ROWS_PARITY_NAME,
                        "blocks": len(rows),
                        "block_size": bs,
                        "sha512": rows_digests,
                    },
                    "columns": {
                        "file": COLS_PARITY_NAME,
                        "blocks": len(cols),
                        "block_size": bs,
                        "sha512": cols_digests,
                    },
                },
            }
            body["sig"] = sha256_hex(am._canonical_bytes(body))
            desc_bytes = am._canonical_bytes(body)
            am._atomic_write(os.path.join(staging, DESCRIPTOR_NAME),
                             desc_bytes, fault)
            descriptor_digest = sha256_hex(desc_bytes)

            os.chmod(staging, 0o755)
            am._fsync_dir(staging)
            _, exit_code = fault.check("publish", "rename")
            if exit_code is not None:
                os._exit(exit_code)
            os.rename(staging, out_dir)
            published = True
            _, exit_code = fault.check("publish", "dirfsync")
            if exit_code is not None:
                os._exit(exit_code)
            am._fsync_dir(parent)
        finally:
            if not published:
                shutil.rmtree(staging, ignore_errors=True)

        if digest_file:
            digest_file = os.path.abspath(digest_file)
            fd = os.open(digest_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                         0o644)
            try:
                os.write(fd, (descriptor_digest + "\n").encode("ascii"))
                os.fsync(fd)
            finally:
                os.close(fd)
        return {
            "descriptor": os.path.join(out_dir, DESCRIPTOR_NAME),
            "descriptor_sha256": descriptor_digest,
            "slots": n,
            "row_groups": len(rows),
            "column_groups": len(cols),
        }
    finally:
        os.close(lock_fd)


def load_grouping_file(path):
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as exc:
        raise KitError("无法读取分组表 %s: %s" % (path, exc))
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KitError("分组表不是合法 UTF-8 JSON: %s" % exc)


# ---------------------------------------------------------------------------
# 描述文件加载与全量结构校验
# ---------------------------------------------------------------------------


def load_descriptor(path, expected_digest):
    raw = _read_regular(path, "描述文件")
    actual = sha256_hex(raw)
    if expected_digest is not None:
        expect = expected_digest.lower().strip()
        if len(expect) != 64 or any(c not in _HEX for c in expect):
            raise DescriptorError("留存的描述文件摘要不是合法 SHA-256")
        if actual != expect:
            raise DescriptorError(
                "描述文件与留存摘要不符：留存 %s 实测 %s" % (expect, actual))
    try:
        d = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DescriptorError("描述文件不是合法 UTF-8 JSON: %s" % exc)
    if not isinstance(d, dict):
        raise DescriptorError("描述文件必须是 JSON 对象")
    sig = d.pop("sig", None)
    if not isinstance(sig, str) or sig != sha256_hex(am._canonical_bytes(d)):
        raise DescriptorError("描述文件自身签名不符：内容被改动或已损坏")
    if d.get("format") != DESCRIPTOR_FORMAT:
        raise DescriptorError("描述文件格式标识不符")
    if d.get("version") != DESCRIPTOR_VERSION:
        raise DescriptorError("不支持的描述文件版本: %r" % d.get("version"))

    bs = _norm_int(d.get("block_size"), "block_size", minimum=1)
    image = d.get("image")
    if not isinstance(image, dict):
        raise DescriptorError("描述文件缺少 image 信息")
    n = _norm_int(image.get("slots"), "image.slots")
    if image.get("size") != n * bs:
        raise DescriptorError("image.size 与 slots/block_size 不一致")
    image_sha256 = _norm_hex(image.get("sha256"), 64, "image.sha256")
    slot_digests = image.get("slot_sha512")
    if not isinstance(slot_digests, list) or len(slot_digests) != n:
        raise DescriptorError("image.slot_sha512 数量必须等于槽位数")
    slot_digests = [_norm_hex(x, 128, "image.slot_sha512[%d]" % i)
                    for i, x in enumerate(slot_digests)]

    mf = d.get("manifest")
    if not isinstance(mf, dict):
        raise DescriptorError("描述文件缺少 manifest 信息")
    manifest_sha256 = _norm_hex(mf.get("sha256"), 64, "manifest.sha256")
    hex_bytes = mf.get("bytes_hex")
    if not isinstance(hex_bytes, str) or not hex_bytes:
        raise DescriptorError("manifest.bytes_hex 必须是非空十六进制字符串")
    try:
        manifest_bytes = bytes.fromhex(hex_bytes)
    except ValueError:
        raise DescriptorError("manifest.bytes_hex 不是合法十六进制")
    if sha256_hex(manifest_bytes) != manifest_sha256:
        raise DescriptorError("封存清单字节与其 SHA-256 不一致")
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DescriptorError("封存清单不是合法 JSON: %s" % exc)
    if not isinstance(manifest, dict):
        raise DescriptorError("封存清单必须是 JSON 对象")
    msig = manifest.pop("sig", None)
    if not isinstance(msig, str) \
            or msig != sha256_hex(am._canonical_bytes(manifest)):
        raise DescriptorError("封存清单签名无效")
    if manifest.get("format") != am.PACKAGE_FORMAT \
            or manifest.get("version") != am.PACKAGE_VERSION:
        raise DescriptorError("封存清单格式/版本不符")
    if manifest.get("block_size") != bs:
        raise DescriptorError("封存清单块长与描述文件不一致")
    mi = manifest.get("image")
    if not isinstance(mi, dict) or mi.get("slots") != n \
            or mi.get("size") != n * bs or mi.get("sha256") != image_sha256:
        raise DescriptorError("封存清单镜像信息与描述文件不一致")
    mdigests = mi.get("digests")
    if not isinstance(mdigests, list) or len(mdigests) != n:
        raise DescriptorError("封存清单 image.digests 数量不符")
    for i, x in enumerate(mdigests):
        _norm_hex(x, 64, "manifest.image.digests[%d]" % i)
    archives = manifest.get("archives")
    if not isinstance(archives, list):
        raise DescriptorError("封存清单 archives 必须是列表")
    for i, meta in enumerate(archives):
        where = "manifest.archives[%d]" % i
        if not isinstance(meta, dict) or not isinstance(meta.get("id"), str):
            raise DescriptorError("%s 结构非法" % where)
        sm = meta.get("slot_map")
        if not isinstance(sm, list):
            raise DescriptorError("%s.slot_map 必须是列表" % where)
        if any(isinstance(m, bool) or not isinstance(m, int) or m < 0
               or m >= n for m in sm):
            raise DescriptorError("%s.slot_map 存在越界或非整数" % where)

    groups = d.get("groups")
    if not isinstance(groups, dict):
        raise DescriptorError("描述文件缺少 groups")
    try:
        rows, cols = validate_grouping(
            {"rows": groups.get("rows"),
             "columns": groups.get("columns")}, n)
    except KitError as exc:
        raise DescriptorError(str(exc))

    parity = d.get("parity")
    if not isinstance(parity, dict):
        raise DescriptorError("描述文件缺少 parity 信息")

    def check_parity(side, groups_side, count):
        node = parity.get(side)
        if not isinstance(node, dict):
            raise DescriptorError("parity.%s 缺失或结构非法" % side)
        if node.get("blocks") != count or node.get("block_size") != bs:
            raise DescriptorError("parity.%s 块数/块长与分组不一致" % side)
        dig = node.get("sha512")
        if not isinstance(dig, list) or len(dig) != count:
            raise DescriptorError("parity.%s.sha512 数量必须等于组数" % side)
        fname = node.get("file")
        if not isinstance(fname, str) or not fname \
                or os.path.basename(fname) != fname:
            raise DescriptorError("parity.%s.file 必须是包内文件名" % side)
        return fname, [_norm_hex(x, 128, "parity.%s.sha512[%d]" % (side, i))
                       for i, x in enumerate(dig)]

    rows_file, rows_pdig = check_parity("rows", rows, len(rows))
    cols_file, cols_pdig = check_parity("columns", cols, len(cols))

    return {
        "block_size": bs,
        "slots": n,
        "image_sha256": image_sha256,
        "slot_sha512": slot_digests,
        "manifest_bytes": manifest_bytes,
        "manifest_sha256": manifest_sha256,
        "rows": rows,
        "columns": cols,
        "rows_parity_file": rows_file,
        "cols_parity_file": cols_file,
        "rows_parity_sha512": rows_pdig,
        "cols_parity_sha512": cols_pdig,
        "descriptor_sha256": actual,
    }


# ---------------------------------------------------------------------------
# 旧镜像抢救：故障盘只读一轮，好块直接落位到未发布的目标镜像
# ---------------------------------------------------------------------------


def _read_old_block(fd, offset, bs):
    """读一个整块；允许块内短读继续，遇 EOF 返回不足 bs 的前缀（一次扫荡）。"""
    buf = b""
    while len(buf) < bs:
        chunk = os.pread(fd, bs - len(buf), offset + len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def salvage_old_image(old_path, n, bs, slot_sha512, target_fd,
                      fault=am.NO_FAULT):
    """返回 known 布尔列表。缺失/短块/缺槽/摘要不符一律记为擦除。

    超长文件或其他 I/O 错误抛 RepairInputError / OSError。
    目标镜像（另一块盘、未发布）允许回读，因此好块在这里直接按槽号落位，
    故障盘 fd 全程只顺序扫荡一轮。
    """
    known = [False] * n
    if not os.path.exists(old_path):
        return known
    fd = os.open(old_path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
                 | getattr(os, "O_NOFOLLOW", 0))
    sealed_size = n * bs
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise RepairInputError("旧镜像不是普通文件: %s" % old_path)
        if st.st_size > sealed_size:
            raise RepairInputError(
                "旧镜像大小 %d 超过封存长度 %d，拒绝修复"
                % (st.st_size, sealed_size))
        for s in range(n):
            buf = _read_old_block(fd, s * bs, bs)
            if len(buf) < bs:
                # EOF/短块：本块与后续全部缺槽都是擦除，结束这一轮读取
                break
            if sha512_hex(buf) == slot_sha512[s]:
                # 身份是槽号：只写入 s 自己的偏移，绝不按内容互相顶替
                am._pwrite_all(target_fd, s * bs, buf, "image", fault)
                known[s] = True
    finally:
        os.close(fd)
    return known


# ---------------------------------------------------------------------------
# 校验载荷按需读取：只缓存“好/坏”元数据，不缓存任何载荷块
# ---------------------------------------------------------------------------


class ParitySource:
    """每份校验载荷一个实例；块缺失/短块/摘要不符 -> 该块视为擦除。"""

    def __init__(self, path, count, bs, sealed_digests, side):
        self.path = path
        self.count = count
        self.bs = bs
        self.sealed = sealed_digests
        self.side = side
        self.fd = None
        self.present = 0
        self.good = [False] * count       # 随规模增长的布尔元数据
        self._checked = [False] * count
        if not os.path.exists(path):
            return
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
                     | getattr(os, "O_NOFOLLOW", 0))
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            os.close(fd)
            raise RepairInputError("校验载荷 %s 不是普通文件" % path)
        limit = count * bs
        if st.st_size > limit:
            os.close(fd)
            raise RepairInputError(
                "%s 校验载荷大小 %d 超过封存长度 %d，拒绝修复"
                % (side, st.st_size, limit))
        self.fd = fd
        self.present = min(count, st.st_size // bs)

    def read(self, g):
        """返回第 g 个校验块；不可核验时返回 None。可读多轮。"""
        if g < 0 or g >= self.count or g >= self.present:
            return None
        if self._checked[g]:
            if not self.good[g]:
                return None
        else:
            self._checked[g] = True
        try:
            buf = b""
            while len(buf) < self.bs:
                chunk = os.pread(self.fd, self.bs - len(buf),
                                 g * self.bs + len(buf))
                if not chunk:
                    break
                buf += chunk
        except OSError:
            self.good[g] = False
            raise
        if len(buf) < self.bs \
                or sha512_hex(buf) != self.sealed[g]:
            self.good[g] = False
            return None
        self.good[g] = True
        return buf

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


# ---------------------------------------------------------------------------
# 叶剥离求解：行/列组异或方程唯一确定全部缺块
#
# 把每个槽看成连接“所在行组”和“所在列组”的一条边，好校验块是顶点上的
# 异或方程。某顶点仅剩一条未知边即可定出该边（槽），随后连锁剥离。
# 连通分量能被剥光当且仅当它是树（方程数=未知边数，解唯一）；留下环核
# 意味着秩不足、解不唯一，直接判失败，绝不用摘要猜内容。
# ---------------------------------------------------------------------------


def reconstruct(desc, known, image_fd, rows_src, cols_src,
                fault=am.NO_FAULT):
    n = desc["slots"]
    bs = desc["block_size"]
    rows = desc["rows"]
    cols = desc["columns"]
    row_of = [0] * n
    col_of = [0] * n
    for g, group in enumerate(rows):
        for s in group:
            row_of[s] = g
    for g, group in enumerate(cols):
        for s in group:
            col_of[s] = g

    sources = {"rows": rows_src, "columns": cols_src}
    groups_of = {"rows": rows, "columns": cols}
    unknown_count = {
        "rows": [len(g) for g in rows],
        "columns": [len(g) for g in cols],
    }
    for s, ok in enumerate(known):
        if ok:
            unknown_count["rows"][row_of[s]] -= 1
            unknown_count["columns"][col_of[s]] -= 1

    queued = {"rows": [False] * len(rows),
              "columns": [False] * len(cols)}
    queue = deque()
    for side in ("rows", "columns"):
        for g in range(len(groups_of[side])):
            if unknown_count[side][g] == 1 and sources[side].read(g) is not None:
                queued[side][g] = True
                queue.append((side, g))

    solved = 0
    while queue:
        side, g = queue.popleft()
        queued[side][g] = False
        if unknown_count[side][g] != 1:
            continue
        parity = sources[side].read(g)
        if parity is None:
            # 校验块在使用时才发现不可核验：方程失效，跳过即可
            continue
        group = groups_of[side][g]
        target = next(s for s in group if not known[s])
        acc = bytearray(parity)
        for t in group:
            if t == target:
                continue
            # 计数为 1 保证其余成员全部已知；目标盘允许回读
            buf = am._pread_all(image_fd, t * bs, bs)
            _xor_into(acc, buf)
        rebuilt = bytes(acc)
        if sha512_hex(rebuilt) != desc["slot_sha512"][target]:
            raise RepairFailed(
                "槽 %d 的重建字节与封存 SHA-512 不符，关系可能互相矛盾"
                % target)
        am._pwrite_all(image_fd, target * bs, rebuilt, "image", fault)
        known[target] = True
        solved += 1

        for other_side in ("rows", "columns"):
            og = row_of[target] if other_side == "rows" else col_of[target]
            unknown_count[other_side][og] -= 1
            if unknown_count[other_side][og] == 1 \
                    and not queued[other_side][og]:
                if sources[other_side].read(og) is not None:
                    queued[other_side][og] = True
                    queue.append((other_side, og))

    remaining = [s for s in range(n) if not known[s]]
    if remaining:
        raise RepairFailed(
            "校验关系不足以唯一确定 %d 个槽（示例槽号 %r）；不交付部分结果"
            % (len(remaining), remaining[:10]))
    return solved


# ---------------------------------------------------------------------------
# 修复主流程
# ---------------------------------------------------------------------------


def repair_package(descriptor_path, expected_digest, old_image_path,
                   rows_parity_path, cols_parity_path, output_path, *,
                   fault=am.NO_FAULT):
    desc = load_descriptor(descriptor_path, expected_digest)
    bs = desc["block_size"]
    n = desc["slots"]

    output_path = os.path.normpath(os.path.abspath(output_path))
    parent = os.path.dirname(output_path)
    name = os.path.basename(output_path)
    if not name or name in (".", ".."):
        raise DescriptorError("输出路径不能是根目录或空名称")
    if am._output_exists(output_path):
        raise am.OutputExistsError("输出路径已存在，拒绝覆盖: %s" % output_path)
    os.makedirs(parent, exist_ok=True)
    prefix, _lock = _output_paths(output_path)
    lock_fd = _acquire_output_lock(output_path)
    try:
        if am._output_exists(output_path):
            raise am.OutputExistsError("输出路径已存在，拒绝覆盖: %s"
                                       % output_path)

        rows_src = ParitySource(rows_parity_path, len(desc["rows"]), bs,
                                desc["rows_parity_sha512"], "rows")
        cols_src = ParitySource(cols_parity_path, len(desc["columns"]), bs,
                                desc["cols_parity_sha512"], "columns")
        try:
            am._clean_stale_staging(parent, prefix)
            staging = tempfile.mkdtemp(prefix=prefix, dir=parent)
            published = False
            try:
                image_path = os.path.join(staging, am.IMAGE_NAME)
                image_fd = os.open(
                    image_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o644)
                try:
                    os.ftruncate(image_fd, n * bs)
                    # 故障盘只读一轮；好块按封存槽号直接落位目标镜像
                    known = salvage_old_image(
                        old_image_path, n, bs, desc["slot_sha512"],
                        image_fd, fault)

                    reconstruct(desc, known, image_fd, rows_src, cols_src,
                                fault)

                    # 完工核验 1：逐槽 SHA-512 + 镜像整体 SHA-256
                    whole = hashlib.sha256()
                    for s in range(n):
                        buf = am._pread_all(image_fd, s * bs, bs)
                        whole.update(buf)
                        if sha512_hex(buf) != desc["slot_sha512"][s]:
                            raise RepairFailed(
                                "完工核验失败：槽 %d 与封存 SHA-512 不符" % s)
                    if whole.hexdigest() != desc["image_sha256"]:
                        raise RepairFailed(
                            "完工核验失败：镜像整体 SHA-256 与封存不符")

                    # 完工核验 2：所有好校验关系必须仍然成立（查矛盾）
                    _verify_all_parity(desc, image_fd, rows_src, cols_src)

                    _, exit_code = fault.check("image", "fsync")
                    if exit_code is not None:
                        os._exit(exit_code)
                    os.fsync(image_fd)
                finally:
                    os.close(image_fd)

                # 原清单逐字节落盘，回读比对后才允许发布
                am._atomic_write(
                    os.path.join(staging, am.MANIFEST_NAME),
                    desc["manifest_bytes"], fault)
                mraw = _read_regular(
                    os.path.join(staging, am.MANIFEST_NAME), "清单")
                if mraw != desc["manifest_bytes"] \
                        or sha256_hex(mraw) != desc["manifest_sha256"]:
                    raise RepairFailed("清单回读与封存字节不一致")

                os.chmod(staging, 0o755)
                am._fsync_dir(staging)
                _, exit_code = fault.check("publish", "rename")
                if exit_code is not None:
                    os._exit(exit_code)
                os.rename(staging, output_path)
                published = True
                _, exit_code = fault.check("publish", "dirfsync")
                if exit_code is not None:
                    os._exit(exit_code)
                am._fsync_dir(parent)
            finally:
                if not published:
                    shutil.rmtree(staging, ignore_errors=True)
        finally:
            rows_src.close()
            cols_src.close()
        return {"output": output_path, "slots": n,
                "descriptor_sha256": desc["descriptor_sha256"]}
    finally:
        os.close(lock_fd)


def _verify_all_parity(desc, image_fd, rows_src, cols_src):
    bs = desc["block_size"]
    for side, src, groups in (
            ("rows", rows_src, desc["rows"]),
            ("columns", cols_src, desc["columns"])):
        for g, group in enumerate(groups):
            parity = src.read(g)
            if parity is None:
                continue
            acc = bytearray(bs)
            for s in group:
                _xor_into(acc, am._pread_all(image_fd, s * bs, bs))
            if bytes(acc) != parity:
                raise RepairFailed(
                    "%s 组 %d 的异或关系与校验块矛盾，拒绝发布" % (side, g))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser():
    parser = argparse.ArgumentParser(
        description="归档包离线修复：修复资料制作与行列异或重建")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_kit = sub.add_parser("make-kit",
                           help="从健康归档包与分组表生成修复资料")
    p_kit.add_argument("--package", required=True, help="健康归档包目录")
    p_kit.add_argument("--groups", required=True, help="分组表 JSON 路径")
    p_kit.add_argument("--out", required=True, help="修复资料输出目录（不得存在）")
    p_kit.add_argument("--digest-file",
                       help="可选：把描述文件 SHA-256 另写到该路径留存")
    p_kit.add_argument("--fault", action="append", default=[],
                       help="故障注入规则（测试用），语义同 archive_merge")

    p_rep = sub.add_parser("repair", help="按修复资料离线重建归档包")
    p_rep.add_argument("--descriptor", required=True, help="描述文件路径")
    p_rep.add_argument("--digest", required=True,
                       help="介质之外留存的描述文件 SHA-256")
    p_rep.add_argument("--old-image", required=True,
                       help="待救旧镜像 image.bin（可缺失/截断/损坏）")
    p_rep.add_argument("--rows-parity", required=True,
                       help="行侧校验载荷路径（可缺失）")
    p_rep.add_argument("--cols-parity", required=True,
                       help="列侧校验载荷路径（可缺失）")
    p_rep.add_argument("--output", required=True,
                       help="重建归档包输出目录（必须尚不存在）")
    p_rep.add_argument("--fault", action="append", default=[],
                       help="故障注入规则（测试用），语义同 archive_merge")
    return parser


def main(argv=None):
    args = _build_parser().parse_args(argv)
    fault = am.FaultInjection(args.fault)
    try:
        if args.cmd == "make-kit":
            grouping = load_grouping_file(args.groups)
            result = make_repair_kit(
                args.package, grouping, args.out,
                fault=fault, digest_file=args.digest_file)
            print(
                "修复资料已生成：%s（%d 槽，行组 %d 个、列组 %d 个）。"
                % (args.out, result["slots"],
                   result["row_groups"], result["column_groups"]))
            print("请把下面的描述文件 SHA-256 留存到修复介质之外：")
            print("  %s" % result["descriptor_sha256"])
            if args.digest_file:
                print("摘要另存于：%s" % os.path.abspath(args.digest_file))
        else:
            result = repair_package(
                args.descriptor, args.digest, args.old_image,
                args.rows_parity, args.cols_parity, args.output, fault=fault)
            print("修复成功，归档包已原子发布：%s（%d 槽，原清单字节不变）。"
                  % (result["output"], result["slots"]))
    except (KitError, DescriptorError, RepairInputError) as exc:
        print("错误: %s" % exc, file=sys.stderr)
        return EXIT_INPUT
    except am.PackageError as exc:
        print("归档包核验失败: %s" % exc, file=sys.stderr)
        return EXIT_PACKAGE_INVALID
    except RepairFailed as exc:
        print("修复失败: %s" % exc, file=sys.stderr)
        return EXIT_REPAIR_FAILED
    except am.OutputExistsError as exc:
        print("错误: %s" % exc, file=sys.stderr)
        return EXIT_OUTPUT_EXISTS
    except BlockingIOError:
        print("错误: 该输出已被另一个进程独占", file=sys.stderr)
        return EXIT_LOCK
    except OSError as exc:
        print("IO 错误: %s" % exc, file=sys.stderr)
        return EXIT_IO
    return EXIT_OK


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    sys.exit(main())

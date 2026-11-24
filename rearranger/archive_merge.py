#!/usr/bin/env python3
"""多归档固定块去重合并：生成“镜像 + 清单”归档包。

一次请求给出块长与按固定顺序排列的归档列表；每个归档有唯一标识、数据
文件路径和按槽位顺序列出的 SHA-256 摘要。程序把全部归档的块按
“归档在请求中的顺序 -> 归档内槽位顺序”做首次出现去重，写入一个合并
镜像，并在清单里给出每个来源归档每个原槽位到镜像槽位的映射。

设计要点（详见 README.md）：

  * 载荷只按单块缓冲处理：归档读、镜像写/回读都以 block_size 为长度，
    不随总数据量增长；映射、摘要等非载荷清单随来源槽位数增长。
  * 去重按块的实际字节判断：摘要命中后还要回读镜像中的候选块逐字节比较，
    因此“声明摘要相同但字节不同”的块绝不会被误合并。
  * 镜像先写在与输出同目录的隐藏临时目录里，清单也在其中原子落盘；
    两者都 fsync 后，才用一次目录 rename 让整个归档包原子可见。
    失败或中断时最终路径上不会出现半成品，也不会出现镜像/清单不匹配。
  * 成功结果可幂等重开：输出已存在时先按请求全量核验，通过则不再改动；
    不通过则拒绝覆盖。
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

# ---------------------------------------------------------------------------
# 退出码（与 rearrange.py 的既有语义保持一致，新增“输出已存在”一档）
# ---------------------------------------------------------------------------
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_INPUT = 3              # 请求/输入文件核验失败，未发布任何归档包
EXIT_PACKAGE_INVALID = 4    # 归档包核验失败（清单/镜像/映射不符）
EXIT_IO = 5                 # 介质 IO 错误
EXIT_LOCK = 6               # 同一输出已被另一进程占用
EXIT_OUTPUT_EXISTS = 7      # 输出已存在且不是本请求的有效完成结果

REQUEST_VERSION = 1
PACKAGE_FORMAT = "fixed-block-archive-package"
PACKAGE_VERSION = 1
IMAGE_NAME = "image.bin"
MANIFEST_NAME = "manifest.json"

_HEX = set("0123456789abcdef")


class RequestError(ValueError):
    """请求结构不合法。"""


class InputError(ValueError):
    """归档文件不可读、长度不符或摘要不符。"""


class PackageError(ValueError):
    """归档包内容与清单/请求不匹配。"""


class OutputExistsError(Exception):
    """输出路径已有内容，且不是本请求的有效完成结果。"""


# ---------------------------------------------------------------------------
# 故障注入（仅测试用；正常运行为零开销）
#
# 规则格式: <目标>:<点位>:<第几次调用>:<动作>[:参数]
#   目标: image | manifest | publish
#   点位:
#     image:    pwrite | fsync
#     manifest: tmpwrite | tmpfsync | rename | dirfsync
#     publish:  rename | dirfsync
#   动作: short[:N] | exit[:N] | hang
# ---------------------------------------------------------------------------


class FaultInjection:
    def __init__(self, rules=None):
        self._rules = {}
        self._counts = {}
        for rule in rules or ():
            target, point, idx, action, *rest = rule.split(":")
            arg = int(rest[0]) if rest else None
            self._rules.setdefault((target, point), []).append(
                (int(idx), action, arg)
            )

    def check(self, target, point):
        """返回 (short_len 或 None, exit_code 或 None)。"""
        key = (target, point)
        n = self._counts.get(key, 0) + 1
        self._counts[key] = n
        for idx, action, arg in self._rules.get(key, ()):
            if idx != n:
                continue
            if action == "short":
                return (arg if arg is not None else 1), None
            if action == "exit":
                os._exit(arg if arg is not None else 137)
            if action == "hang":
                import time
                while True:
                    time.sleep(3600)
            raise ValueError("未知故障动作: %r" % action)
        return None, None


NO_FAULT = FaultInjection()


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_bytes(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _norm_sha256(value, where):
    if not isinstance(value, str):
        raise RequestError("%s 的摘要必须是十六进制字符串" % where)
    s = value.lower()
    if len(s) != 64 or any(c not in _HEX for c in s):
        raise RequestError("%s 的摘要不是合法的 SHA-256 十六进制值" % where)
    return s


def _fsync_dir(directory):
    dfd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _pread_all(fd, offset, size):
    chunks = []
    remaining = size
    while remaining:
        buf = os.pread(fd, remaining, offset)
        if not buf:
            raise OSError("pread 意外到达文件尾")
        chunks.append(buf)
        offset += len(buf)
        remaining -= len(buf)
    return b"".join(chunks)


def _pwrite_all(fd, offset, data, target, fault):
    """短写循环补齐；不做 fsync（调用方在阶段末尾统一 fsync）。"""
    off = 0
    while off < len(data):
        short_len, exit_code = fault.check(target, "pwrite")
        if exit_code is not None:
            os._exit(exit_code)
        chunk = data[off:]
        if short_len is not None:
            chunk = chunk[: max(0, min(short_len, len(chunk)))]
        if not chunk:
            chunk = data[off: off + 1]
        n = os.pwrite(fd, chunk, offset + off)
        if n <= 0:
            raise OSError("pwrite 返回 %d，介质写入失败" % n)
        off += n


def _atomic_write(path, payload, fault):
    """tmp -> fsync -> rename -> 目录 fsync 的原子落盘（目录内清单用）。"""
    directory = os.path.dirname(path) or "."
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        off = 0
        while off < len(payload):
            short_len, exit_code = fault.check("manifest", "tmpwrite")
            if exit_code is not None:
                os._exit(exit_code)
            chunk = payload[off:]
            if short_len is not None:
                chunk = chunk[: max(0, min(short_len, len(chunk)))]
            if not chunk:
                chunk = payload[off: off + 1]
            off += os.write(fd, chunk)
        _, exit_code = fault.check("manifest", "tmpfsync")
        if exit_code is not None:
            os._exit(exit_code)
        os.fsync(fd)
    finally:
        os.close(fd)
    _, exit_code = fault.check("manifest", "rename")
    if exit_code is not None:
        os._exit(exit_code)
    os.rename(tmp, path)
    dfd = os.open(directory, os.O_RDONLY)
    try:
        _, exit_code = fault.check("manifest", "dirfsync")
        if exit_code is not None:
            os._exit(exit_code)
        os.fsync(dfd)
    finally:
        os.close(dfd)


# ---------------------------------------------------------------------------
# 请求规范化
# ---------------------------------------------------------------------------


def load_request_file(path):
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as exc:
        raise RequestError("无法读取请求文件 %s: %s" % (path, exc))
    try:
        req = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RequestError("请求文件不是合法 UTF-8 JSON: %s" % exc)
    return req


def normalize_request(req, base_dir=None):
    """校验并规范化请求；相对路径先按 base_dir，再回退到当前目录解析。"""
    if not isinstance(req, dict):
        raise RequestError("请求必须是 JSON 对象")
    if "version" in req and (isinstance(req["version"], bool)
                             or req["version"] != REQUEST_VERSION):
        raise RequestError("仅支持请求 version=%d" % REQUEST_VERSION)
    if "block_size" not in req:
        raise RequestError("请求缺少 block_size")
    bs = req["block_size"]
    if isinstance(bs, bool) or not isinstance(bs, int) or bs <= 0:
        raise RequestError("block_size 必须是正整数")
    if "archives" not in req or not isinstance(req["archives"], list):
        raise RequestError("请求缺少 archives 列表")

    base = os.path.abspath(base_dir or os.getcwd())
    cwd = os.path.abspath(os.getcwd())
    archives = []
    seen_ids = set()
    for i, a in enumerate(req["archives"]):
        where = "archives[%d]" % i
        if not isinstance(a, dict):
            raise RequestError("%s 必须是对象" % where)
        for key in ("id", "path", "digests"):
            if key not in a:
                raise RequestError("%s 缺少字段 %s" % (where, key))
        aid = a["id"]
        if not isinstance(aid, str):
            raise RequestError("%s.id 必须是字符串" % where)
        if aid in seen_ids:
            raise RequestError("归档 ID 重复: %r" % aid)
        seen_ids.add(aid)
        src = a["path"]
        if not isinstance(src, str) or not src:
            raise RequestError("%s.path 必须是非空字符串" % where)
        if not isinstance(a["digests"], list):
            raise RequestError("%s.digests 必须是列表" % where)
        digests = [
            _norm_sha256(d, "%s.digests[%d]" % (where, s))
            for s, d in enumerate(a["digests"])
        ]
        if os.path.isabs(src):
            resolved = os.path.normpath(src)
        else:
            candidate = os.path.normpath(os.path.join(base, src))
            fallback = os.path.normpath(os.path.join(cwd, src))
            # 相对路径以请求文件所在目录为准；仅当该路径不存在而当前目录
            # 下确有同名文件时回退，兼容从工作目录直接调用的用法。
            resolved = candidate
            if (not os.path.exists(candidate)
                    and os.path.exists(fallback)):
                resolved = fallback
        archives.append({
            "id": aid,
            "path": src,
            "resolved_path": resolved,
            "digests": digests,
        })
    return {
        "block_size": bs,
        "archives": archives,
        "base_dir": base,
    }


def canonical_request_bytes(norm) -> bytes:
    """规范化请求的绑定哈希输入：只含请求本身，不含解析后的绝对路径。"""
    return _canonical_bytes({
        "version": REQUEST_VERSION,
        "block_size": int(norm["block_size"]),
        "archives": [
            {"id": a["id"], "path": a["path"], "digests": list(a["digests"])}
            for a in norm["archives"]
        ],
    })


def request_hash(norm) -> str:
    return sha256_hex(canonical_request_bytes(norm))


# ---------------------------------------------------------------------------
# 输入文件元数据预检（不读载荷）：不可读、非普通文件、长度不符尽早拒绝
# ---------------------------------------------------------------------------


def _check_input_fd(fd, archive, block_size):
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise InputError("归档 %r 不是普通文件: %s"
                         % (archive["id"], archive["resolved_path"]))
    if st.st_size % block_size != 0:
        raise InputError(
            "归档 %r 大小 %d 不是块长 %d 的整数倍"
            % (archive["id"], st.st_size, block_size)
        )
    slots = st.st_size // block_size
    if len(archive["digests"]) != slots:
        raise InputError(
            "归档 %r 的摘要数量 %d 与文件槽位数 %d 不一致"
            % (archive["id"], len(archive["digests"]), slots)
        )
    return slots


def _open_input(path):
    # O_NONBLOCK 防止路径被换成 FIFO 时在 open 阶段永久阻塞；
    # 随后 fstat 会拒绝一切非普通文件。
    return os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))


def preflight_inputs(norm):
    bs = norm["block_size"]
    for archive in norm["archives"]:
        try:
            fd = _open_input(archive["resolved_path"])
        except OSError as exc:
            raise InputError("无法读取归档 %r (%s): %s"
                             % (archive["id"], archive["resolved_path"], exc))
        try:
            _check_input_fd(fd, archive, bs)
        finally:
            os.close(fd)


# ---------------------------------------------------------------------------
# 去重器：哈希索引只是候选，真正合并前回读镜像逐字节确认
#
# 索引的增长只与“不同块数”有关（非载荷元数据）；载荷缓冲始终只有
# 当前块与候选块这两个 block_size 大小的缓冲。
# ---------------------------------------------------------------------------


class _Deduper:
    def __init__(self, image_fd, block_size, fault):
        self.image_fd = image_fd
        self.block_size = block_size
        self.fault = fault
        self._candidates = {}        # 实际摘要 -> [镜像槽位...]
        self._image_digests = []
        self._whole = hashlib.sha256()

    def intern(self, buf: bytes, digest: str) -> int:
        for idx in self._candidates.get(digest, ()):
            existing = _pread_all(
                self.image_fd, idx * self.block_size, self.block_size
            )
            if existing == buf:
                return idx
        idx = len(self._image_digests)
        _pwrite_all(
            self.image_fd, idx * self.block_size, buf, "image", self.fault
        )
        self._candidates.setdefault(digest, []).append(idx)
        self._image_digests.append(digest)
        self._whole.update(buf)
        return idx

    @property
    def image_digests(self):
        return list(self._image_digests)

    @property
    def image_slots(self):
        return len(self._image_digests)

    def image_sha256(self):
        return self._whole.hexdigest()


# ---------------------------------------------------------------------------
# 合并
# ---------------------------------------------------------------------------


def _identity(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _merge_archive(archive, block_size, deduper):
    """逐块核验摘要并去重，返回该归档每个原槽位对应的镜像槽位。"""
    path = archive["resolved_path"]
    try:
        fd = _open_input(path)
    except OSError as exc:
        raise InputError("无法读取归档 %r (%s): %s"
                         % (archive["id"], path, exc))
    try:
        slots = _check_input_fd(fd, archive, block_size)
        before = _identity(os.fstat(fd))
        slot_map = []
        for s in range(slots):
            try:
                buf = _pread_all(fd, s * block_size, block_size)
            except OSError as exc:
                raise InputError("读取归档 %r 失败: %s"
                                 % (archive["id"], exc))
            got = sha256_hex(buf)
            want = archive["digests"][s]
            if got != want:
                raise InputError(
                    "归档 %r 槽 %d 摘要不符：声明 %s 实测 %s"
                    % (archive["id"], s, want, got)
                )
            # 镜像写入失败属于介质 IO 错误，不包装成输入错误
            slot_map.append(deduper.intern(buf, got))
        after = _identity(os.fstat(fd))
        if before != after:
            raise InputError(
                "归档 %r 在处理期间被改动（调用方应保持输入不变）"
                % archive["id"]
            )
        return slot_map
    finally:
        os.close(fd)


def _build_manifest(norm, req_sha, archive_meta, deduper):
    bs = norm["block_size"]
    image = {
        "file": IMAGE_NAME,
        "slots": deduper.image_slots,
        "size": deduper.image_slots * bs,
        "sha256": deduper.image_sha256(),
        "digests": deduper.image_digests,
    }
    body = {
        "format": PACKAGE_FORMAT,
        "version": PACKAGE_VERSION,
        "block_size": bs,
        "request_sha256": req_sha,
        "image": image,
        "archives": archive_meta,
    }
    body["sig"] = sha256_hex(_canonical_bytes(body))
    return body


def _staging_prefix(output_path):
    name = os.path.basename(os.path.normpath(output_path))
    return ".archive_merge.%s.tmp." % name


def _lock_path(output_path):
    name = os.path.basename(os.path.normpath(output_path))
    return os.path.join(
        os.path.dirname(os.path.normpath(output_path)) or ".",
        ".archive_merge.%s.lock" % name,
    )


def _clean_stale_staging(parent, prefix):
    """持锁后清理同一输出名下的历史半成品（隐藏目录，不会作为结果可见）。"""
    try:
        names = os.listdir(parent)
    except OSError:
        return
    for name in names:
        if not name.startswith(prefix):
            continue
        path = os.path.join(parent, name)
        try:
            st = os.lstat(path)
            if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def _acquire_lock(output_path):
    lp = _lock_path(output_path)
    fd = os.open(lp, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        # flock 冲突在不同平台上可能是 EAGAIN/EWOULDBLOCK 或 EACCES，
        # 统一映射成 BlockingIOError，便于 CLI 报“已被独占”。
        if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
            raise BlockingIOError(exc.errno, str(exc))
        raise
    return fd


def _output_exists(path):
    return os.path.lexists(path)


def merge_package(request, output_path, *, base_dir=None, fault=NO_FAULT):
    """合并请求并发布归档包。返回 {'status': ..., 'manifest': ...}。

    status 为 "published"（本次生成）或 "already-complete"（既有输出已按
    同一请求核验通过，未做任何改动）。
    """
    norm = normalize_request(request, base_dir)
    req_sha = request_hash(norm)
    output_path = os.path.normpath(os.path.abspath(output_path))
    parent = os.path.dirname(output_path)
    name = os.path.basename(output_path)
    if not name or name in (".", ".."):
        raise RequestError("输出路径不能是根目录或空名称")

    if _output_exists(output_path):
        return _accept_existing(norm, req_sha, output_path)

    # 请求结构、ID 唯一性、全部归档的可读性与长度先做无载荷预检
    preflight_inputs(norm)

    os.makedirs(parent, exist_ok=True)
    lock_fd = _acquire_lock(output_path)
    try:
        if _output_exists(output_path):
            return _accept_existing(norm, req_sha, output_path)

        prefix = _staging_prefix(output_path)
        _clean_stale_staging(parent, prefix)
        staging = tempfile.mkdtemp(prefix=prefix, dir=parent)

        published = False
        try:
            image_path = os.path.join(staging, IMAGE_NAME)
            image_fd = os.open(
                image_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o644
            )
            try:
                deduper = _Deduper(image_fd, norm["block_size"], fault)
                archive_meta = []
                for archive in norm["archives"]:
                    slot_map = _merge_archive(
                        archive, norm["block_size"], deduper
                    )
                    archive_meta.append({
                        "id": archive["id"],
                        "slots": len(archive["digests"]),
                        "size": len(archive["digests"]) * norm["block_size"],
                        "slot_map": slot_map,
                        "digests": list(archive["digests"]),
                    })
                # 镜像载荷全部落盘后，才允许清单引用它们
                _, exit_code = fault.check("image", "fsync")
                if exit_code is not None:
                    os._exit(exit_code)
                os.fsync(image_fd)
            finally:
                os.close(image_fd)

            manifest = _build_manifest(norm, req_sha, archive_meta, deduper)
            _atomic_write(
                os.path.join(staging, MANIFEST_NAME),
                _canonical_bytes(manifest),
                fault,
            )
            # mkdtemp 默认 0700；归档包是给读取系统用的，放宽到常规目录权限
            os.chmod(staging, 0o755)
            _fsync_dir(staging)

            # 唯一一次发布动作：整个目录原子出现，镜像与清单不可分离
            _, exit_code = fault.check("publish", "rename")
            if exit_code is not None:
                os._exit(exit_code)
            os.rename(staging, output_path)
            published = True
            _, exit_code = fault.check("publish", "dirfsync")
            if exit_code is not None:
                os._exit(exit_code)
            _fsync_dir(parent)
        finally:
            if not published:
                shutil.rmtree(staging, ignore_errors=True)
        return {"status": "published", "manifest": manifest}
    finally:
        os.close(lock_fd)


def merge_request_file(request_path, output_path, *, fault=NO_FAULT):
    req = load_request_file(request_path)
    base = os.path.dirname(os.path.abspath(request_path))
    return merge_package(req, output_path, base_dir=base, fault=fault)


# ---------------------------------------------------------------------------
# 核验
# ---------------------------------------------------------------------------


def _read_regular_file(path, what):
    """读取包内的小元数据文件；拒绝 FIFO/设备/符号链接等异常路径。"""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
                     | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise PackageError("无法读取%s %s: %s" % (what, path, exc))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise PackageError("%s不是普通文件: %s" % (what, path))
        try:
            return _pread_all(fd, 0, st.st_size)
        except OSError as exc:
            raise PackageError("读取%s %s 失败: %s" % (what, path, exc))
    finally:
        os.close(fd)


def _load_manifest(package_path):
    if not os.path.isdir(package_path):
        raise PackageError("归档包不存在或不是目录: %s" % package_path)
    mpath = os.path.join(package_path, MANIFEST_NAME)
    raw = _read_regular_file(mpath, "清单")
    try:
        d = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackageError("清单不是合法 UTF-8 JSON: %s" % exc)
    if not isinstance(d, dict):
        raise PackageError("清单必须是 JSON 对象")
    sig = d.pop("sig", None)
    if not isinstance(sig, str) or sig != sha256_hex(_canonical_bytes(d)):
        raise PackageError("清单签名不符：清单被改动或已损坏")
    return d


def _manifest_path_for(package_path, image):
    """清单里的文件名只允许是包内单个文件名，拒绝路径穿越。"""
    name = image.get("file")
    if not isinstance(name, str) or not name or os.path.basename(name) != name:
        raise PackageError("清单 image.file 必须是包内文件名")
    path = os.path.join(package_path, name)
    if os.path.islink(path):
        raise PackageError("镜像不能是符号链接")
    return path


def _manifest_sha_list(values, where):
    """清单内的摘要列表：格式问题一律按“归档包不匹配”报告。"""
    if not isinstance(values, list):
        raise PackageError("%s 必须是列表" % where)
    try:
        return [_norm_sha256(x, where) for x in values]
    except RequestError as exc:
        raise PackageError(str(exc))


def _verify_norm(norm, req_sha, package_path):
    d = _load_manifest(package_path)
    if d.get("format") != PACKAGE_FORMAT:
        raise PackageError("清单格式标识不符")
    if d.get("version") != PACKAGE_VERSION:
        raise PackageError("不支持的清单版本: %r" % d.get("version"))
    bs = norm["block_size"]
    if d.get("block_size") != bs:
        raise PackageError("清单块长与请求不一致")
    if d.get("request_sha256") != req_sha:
        raise PackageError("清单绑定的请求哈希与当前请求不一致")

    image = d.get("image")
    if not isinstance(image, dict):
        raise PackageError("清单缺少 image 信息")
    if image.get("file") != IMAGE_NAME:
        raise PackageError("清单 image.file 必须为 %r" % IMAGE_NAME)
    slots = image.get("slots")
    if isinstance(slots, bool) or not isinstance(slots, int) or slots < 0:
        raise PackageError("清单 image.slots 非法")
    if image.get("size") != slots * bs:
        raise PackageError("清单 image.size 与槽位数/块长不一致")
    image_digests = _manifest_sha_list(image.get("digests"), "image.digests")
    if len(image_digests) != slots:
        raise PackageError("清单 image.digests 数量与槽位数不一致")

    img_path = _manifest_path_for(package_path, image)
    try:
        fd = os.open(img_path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
                     | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise PackageError("无法读取镜像 %s: %s" % (img_path, exc))
    secondary = []
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size != slots * bs:
            raise PackageError("镜像文件大小不是 image.slots * block_size")
        whole = hashlib.sha256()
        for i in range(slots):
            try:
                buf = _pread_all(fd, i * bs, bs)
            except OSError as exc:
                raise PackageError("读取镜像槽 %d 失败: %s" % (i, exc))
            whole.update(buf)
            if sha256_hex(buf) != image_digests[i]:
                raise PackageError("镜像槽 %d 实测摘要与清单不符" % i)
            secondary.append(
                hashlib.blake2b(buf, digest_size=32).hexdigest()
            )
        if whole.hexdigest() != image.get("sha256"):
            raise PackageError("镜像整体摘要与清单不符")

        # 去重保证：内容相同的块不得在镜像里出现两份。
        # 用 (sha256, blake2b) 分组，只有真正撞哈希的组才需要逐字节复核。
        groups = {}
        for i, key in enumerate(zip(image_digests, secondary)):
            groups.setdefault(key, []).append(i)
        for idxs in groups.values():
            if len(idxs) < 2:
                continue
            first = _pread_all(fd, idxs[0] * bs, bs)
            for other in idxs[1:]:
                if _pread_all(fd, other * bs, bs) == first:
                    raise PackageError(
                        "镜像槽 %d 与 %d 内容相同却未合并"
                        % (idxs[0], other)
                    )
    finally:
        os.close(fd)

    archives = d.get("archives")
    if not isinstance(archives, list):
        raise PackageError("清单 archives 必须是列表")
    if len(archives) != len(norm["archives"]):
        raise PackageError("清单归档数量与请求不一致")

    seen = set()
    next_new = 0
    for i, (meta, req_a) in enumerate(zip(archives, norm["archives"])):
        if not isinstance(meta, dict):
            raise PackageError("清单 archives[%d] 必须是对象" % i)
        if meta.get("id") != req_a["id"]:
            raise PackageError("清单 archives[%d].id 与请求不一致" % i)
        want_slots = len(req_a["digests"])
        if meta.get("slots") != want_slots:
            raise PackageError("归档 %r 的槽位数与请求不一致" % req_a["id"])
        if meta.get("size") != want_slots * bs:
            raise PackageError("归档 %r 的 size 与槽位数不一致" % req_a["id"])
        declared = _manifest_sha_list(
            meta.get("digests"), "archives[%d].digests" % i)
        if declared != req_a["digests"]:
            raise PackageError("归档 %r 的摘要与请求不一致" % req_a["id"])
        slot_map = meta.get("slot_map")
        if not isinstance(slot_map, list) or len(slot_map) != want_slots:
            raise PackageError("归档 %r 的 slot_map 长度不符" % req_a["id"])
        for s, m in enumerate(slot_map):
            if isinstance(m, bool) or not isinstance(m, int):
                raise PackageError(
                    "归档 %r 槽 %d 的镜像槽位不是整数" % (req_a["id"], s)
                )
            if m < 0 or m >= slots:
                raise PackageError(
                    "归档 %r 槽 %d 的镜像槽位 %d 越界" % (req_a["id"], s, m)
                )
            if image_digests[m] != req_a["digests"][s]:
                raise PackageError(
                    "归档 %r 槽 %d 映射到的镜像槽 %d 内容摘要不符"
                    % (req_a["id"], s, m)
                )
            # 镜像按“归档顺序 -> 槽位顺序”的首次出现编号；
            # 新出现的镜像槽必须依次为 0,1,2,...
            if m not in seen:
                if m != next_new:
                    raise PackageError(
                        "镜像槽位顺序不是按首次出现编号（期望 %d 实际 %d）"
                        % (next_new, m)
                    )
                seen.add(m)
                next_new += 1
    if next_new != slots:
        raise PackageError("镜像中存在未被任何来源槽位引用的槽")
    return d


def verify_package(request, package_path, *, base_dir=None):
    norm = normalize_request(request, base_dir)
    return _verify_norm(norm, request_hash(norm), package_path)


def verify_request_file(request_path, package_path):
    req = load_request_file(request_path)
    base = os.path.dirname(os.path.abspath(request_path))
    return verify_package(req, package_path, base_dir=base)


def _accept_existing(norm, req_sha, output_path):
    try:
        manifest = _verify_norm(norm, req_sha, output_path)
    except PackageError as exc:
        raise OutputExistsError(
            "输出已存在且不是本请求的有效完成结果，拒绝覆盖: %s" % exc
        )
    return {"status": "already-complete", "manifest": manifest}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _run_offline_repair_cli(argv):
    """把离线修复子命令转交给同目录的 offline_repair.py。

    合并/核验的既有命令与语义保持不变；离线修复是独立能力，只是复用
    同一个入口以便发现。--fault 等参数语义由 offline_repair.py 定义。
    """
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "offline_repair.py")
    spec = importlib.util.spec_from_file_location("_offline_repair", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main(argv)


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    parser = argparse.ArgumentParser(
        description="多归档固定块去重合并（镜像 + 清单的原子归档包）；"
                    "make-repair-kit/repair 为离线修复，见 offline_repair.py"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_merge = sub.add_parser("merge", help="合并请求中的归档并发布归档包")
    p_merge.add_argument("--request", required=True, help="请求 JSON 路径")
    p_merge.add_argument("--output", required=True, help="归档包输出目录")
    p_merge.add_argument(
        "--fault", action="append", default=[],
        help="故障注入规则（测试用），可重复，格式见模块头注释",
    )

    p_ver = sub.add_parser("verify", help="按请求全量核验归档包")
    p_ver.add_argument("--request", required=True)
    p_ver.add_argument("--package", required=True)

    p_kit = sub.add_parser("make-repair-kit",
                           help="从健康归档包与分组表生成离线修复资料")
    p_kit.add_argument("--package", required=True)
    p_kit.add_argument("--groups", required=True)
    p_kit.add_argument("--out", required=True)
    p_kit.add_argument("--digest-file")
    p_kit.add_argument("--fault", action="append", default=[])

    p_rep = sub.add_parser("repair", help="按修复资料离线重建归档包")
    p_rep.add_argument("--descriptor", required=True)
    p_rep.add_argument("--digest", required=True)
    p_rep.add_argument("--old-image", required=True)
    p_rep.add_argument("--rows-parity", required=True)
    p_rep.add_argument("--cols-parity", required=True)
    p_rep.add_argument("--output", required=True)
    p_rep.add_argument("--fault", action="append", default=[])

    args = parser.parse_args(argv)
    if args.cmd in ("make-repair-kit", "repair"):
        kit_cmd = "make-kit" if args.cmd == "make-repair-kit" else "repair"
        return _run_offline_repair_cli([kit_cmd] + list(argv[1:]))
    try:
        if args.cmd == "merge":
            fault = FaultInjection(args.fault)
            result = merge_request_file(
                args.request, args.output, fault=fault
            )
            m = result["manifest"]
            if result["status"] == "already-complete":
                print("该归档包此前已按同一请求完成并核验通过，未做改动。")
            else:
                print(
                    "合并完成：%d 个来源归档，%d 个来源槽位，镜像 %d 个不同块"
                    "（%d 字节），清单与镜像已原子发布。"
                    % (len(m["archives"]),
                       sum(a["slots"] for a in m["archives"]),
                       m["image"]["slots"], m["image"]["size"])
                )
        else:
            m = verify_request_file(args.request, args.package)
            print(
                "verify 通过：%d 个来源归档的全部 %d 个槽位映射到 %d 个镜像块。"
                % (len(m["archives"]),
                   sum(a["slots"] for a in m["archives"]),
                   m["image"]["slots"])
            )
    except (RequestError, InputError) as exc:
        print("错误: %s" % exc, file=sys.stderr)
        return EXIT_INPUT
    except PackageError as exc:
        print("归档包核验失败: %s" % exc, file=sys.stderr)
        return EXIT_PACKAGE_INVALID
    except OutputExistsError as exc:
        print("错误: %s" % exc, file=sys.stderr)
        return EXIT_OUTPUT_EXISTS
    except BlockingIOError:
        print("错误: 该输出已被另一个合并进程独占", file=sys.stderr)
        return EXIT_LOCK
    except OSError as exc:
        print("IO 错误: %s" % exc, file=sys.stderr)
        return EXIT_IO
    return EXIT_OK


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    sys.exit(main())

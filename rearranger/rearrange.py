#!/usr/bin/env python3
"""离线固定块文件原地重排工具。

把由等长块组成的数据文件，按作业描述里的 layout（目标槽 -> 旧槽）
原地重排。介质上只额外持久化：
  * 一个作业状态日志（纯元数据 JSON，原子替换）
  * 至多一个块长的备用载荷文件（仅在处理环时存在）
  * 完成后的凭据清单（纯元数据）

设计要点见同目录 README.md。
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import signal
import sys
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# 退出码
# ---------------------------------------------------------------------------
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_PREFLIGHT = 3          # 首次写盘前的核验失败（未动数据盘）
EXIT_RESUME_REJECTED = 4    # 续接身份/摘要核验失败（疑似外部写入）
EXIT_IO = 5                 # 介质 IO 错误
EXIT_LOCK = 6               # 文件已被其他作业占用

JOURNAL_NAME = "journal.json"
SPARE_NAME = "spare.block"
CRED_NAME = "credential.json"
LOCK_NAME = "job.lock"

JOB_VERSION = 1

# ---------------------------------------------------------------------------
# 故障注入（仅用于测试夹具；正常运行为零开销）
#
# 规则格式: <目标>:<点位>:<第几次调用>:<动作>[:参数]
#   目标: data | spare | journal
#   点位:
#     data/spare: pwrite | fsync
#     journal:    tmpwrite | tmpfsync | rename | dirfsync
#   动作: short[:N]  -> 强制只写入前 N 字节（N 省略则写 1 字节），之后正常补齐
#         exit[:N]   -> os._exit(N)，默认 137（模拟杀进程/断电）
#         hang       -> 永久挂起（仅测试独占锁用）
# ---------------------------------------------------------------------------


class FaultInjection:
    def __init__(self, rules=None):
        # (target, point) -> list[(call_index, action, arg)]
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
# 工具函数
# ---------------------------------------------------------------------------


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_job_bytes(job: dict) -> bytes:
    """作业描述的规范序列化：布局/摘要/块长等全部输入，键排序、无空白。"""
    canon = {
        "version": JOB_VERSION,
        "block_size": int(job["block_size"]),
        "layout": [int(x) for x in job["layout"]],
        "old_digests": [str(x) for x in job["old_digests"]],
        "digest_alg": str(job.get("digest_alg", "sha256")),
    }
    if canon["digest_alg"] != "sha256":
        raise ValueError("仅支持 sha256 摘要")
    return json.dumps(canon, sort_keys=True, separators=(",", ":")).encode("utf-8")


def atomic_write_json(path, payload: bytes, fault: FaultInjection):
    """在 path 所在目录上做 tmp + fsync + rename + 目录 fsync 的原子提交。"""
    directory = os.path.dirname(path) or "."
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        off = 0
        while off < len(payload):
            short_len, exit_code = fault.check("journal", "tmpwrite")
            if exit_code is not None:
                os._exit(exit_code)
            chunk = payload[off:]
            if short_len is not None:
                chunk = chunk[: max(0, min(short_len, len(chunk)))]
            if not chunk:
                chunk = payload[off: off + 1]
            n = os.write(fd, chunk)
            off += n
        _, exit_code = fault.check("journal", "tmpfsync")
        if exit_code is not None:
            os._exit(exit_code)
        os.fsync(fd)
    finally:
        os.close(fd)
    _, exit_code = fault.check("journal", "rename")
    if exit_code is not None:
        os._exit(exit_code)
    os.rename(tmp, path)
    dfd = os.open(directory, os.O_RDONLY)
    try:
        _, exit_code = fault.check("journal", "dirfsync")
        if exit_code is not None:
            os._exit(exit_code)
        os.fsync(dfd)
    finally:
        os.close(dfd)


def fsync_dir(directory):
    dfd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


# ---------------------------------------------------------------------------
# 块 IO 包装：所有载荷写入都经此处理，短写循环补齐并支持故障注入
# ---------------------------------------------------------------------------


def block_pwrite(fd, offset, data: bytes, target: str, fault: FaultInjection):
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
    _, exit_code = fault.check(target, "fsync")
    if exit_code is not None:
        os._exit(exit_code)
    os.fsync(fd)


def block_pread(fd, offset, size):
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


# ---------------------------------------------------------------------------
# 函数图分解
#
# f(t) = layout[t] 表示目标槽 t 取自旧槽 f(t)。
# 功能图由“环 + 喂入环的树”构成。剥叶（Kahn）顺序即自底向上的
# 树处理顺序：先处理所有把数据送往 t 的子节点，再处理 t 本身。
# ---------------------------------------------------------------------------


def decompose(n, layout):
    indeg = [0] * n
    for src in layout:
        indeg[src] += 1
    # 就绪队列与剥叶输出严格分开：一个节点只能入队一次、输出一次。
    # 新就绪节点一律队尾追加，绝不插到 head 之前；初始有序保证确定性。
    ready = sorted(i for i in range(n) if indeg[i] == 0)
    tree_order = []
    head = 0
    while head < len(ready):
        node = ready[head]
        head += 1
        tree_order.append(node)
        parent = layout[node]
        indeg[parent] -= 1
        if indeg[parent] == 0:
            ready.append(parent)
    on_cycle = [indeg[i] > 0 for i in range(n)]

    cycles = []
    seen = [False] * n
    for start in range(n):
        if not on_cycle[start] or seen[start]:
            continue
        cyc = []
        node = start
        while not seen[node]:
            seen[node] = True
            cyc.append(node)
            node = layout[node]
        cycles.append(cyc)
    return tree_order, on_cycle, cycles


# ---------------------------------------------------------------------------
# 重排器
# ---------------------------------------------------------------------------


@dataclass
class Journal:
    job_hash: str
    file_id: dict
    block_size: int
    nblocks: int
    tree_cursor: int
    tree_order: list
    cycles: list          # 每个环的槽位顺序
    cycle_index: int
    stage: str            # "spare"（树处理/环备用块阶段）| "rotate"（环轮转）
    rotate_cursor: int
    sig: str = ""

    def to_json(self) -> bytes:
        d = {k: v for k, v in self.__dict__.items() if k != "sig"}
        body = json.dumps(d, sort_keys=True, separators=(",", ":")).encode()
        d["sig"] = sha256_hex(body)
        return json.dumps(d, sort_keys=True, separators=(",", ":")).encode()

    @staticmethod
    def from_json(raw: bytes) -> "Journal":
        d = json.loads(raw.decode("utf-8"))
        sig = d.pop("sig", None)
        body = json.dumps(d, sort_keys=True, separators=(",", ":")).encode()
        if not sig or sig != sha256_hex(body):
            raise ValueError("作业日志签名不符：日志被外部改动过")
        return Journal(**d)


class Rearranger:
    def __init__(self, data_path, job, sidecar_dir, fault=NO_FAULT):
        self.data_path = data_path
        self.job = job
        self.sidecar = sidecar_dir
        self.fault = fault
        self.block_size = int(job["block_size"])
        self.layout = [int(x) for x in job["layout"]]
        self.old_digests = [str(x) for x in job["old_digests"]]
        self.n = len(self.layout)
        self.job_hash = sha256_hex(canonical_job_bytes(job))
        self.journal_path = os.path.join(sidecar_dir, JOURNAL_NAME)
        self.spare_path = os.path.join(sidecar_dir, SPARE_NAME)
        self.cred_path = os.path.join(sidecar_dir, CRED_NAME)
        self.lock_path = os.path.join(sidecar_dir, LOCK_NAME)
        self.fd = None
        self.spare_fd = None
        self.lock_fd = None

    # -- 路径与锁 ---------------------------------------------------------

    def _open_lock(self):
        self.lock_fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.lock_fd)
            self.lock_fd = None
            raise

    def _close_lock(self):
        if self.lock_fd is not None:
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(self.lock_fd)
                self.lock_fd = None

    # -- 首次写盘前的全部核验 ---------------------------------------------

    def preflight(self):
        if self.block_size <= 0:
            raise ValueError("block_size 必须为正数")
        if len(self.old_digests) != self.n:
            raise ValueError("old_digests 数量(%d) 与槽位数(%d) 不一致"
                             % (len(self.old_digests), self.n))
        if any(x < 0 or x >= self.n for x in self.layout):
            raise ValueError("layout 中存在越界旧槽编号")
        st = os.stat(self.data_path)
        if st.st_size != self.n * self.block_size:
            raise ValueError(
                "文件大小 %d 不是 %d 个块(每块 %d 字节)的整数倍"
                % (st.st_size, self.n, self.block_size)
            )
        fd = os.open(self.data_path, os.O_RDONLY)
        identity = hashlib.sha256()
        try:
            for slot in range(self.n):
                buf = block_pread(fd, slot * self.block_size, self.block_size)
                identity.update(buf)
                got = sha256_hex(buf)
                if got != self.old_digests[slot]:
                    raise ValueError(
                        "原始块摘要核验失败：槽 %d 声明 %s 实测 %s"
                        % (slot, self.old_digests[slot], got)
                    )
        finally:
            os.close(fd)
        file_id = {
            "size": st.st_size,
            "dev": st.st_dev,
            "ino": st.st_ino,
            "content_sha256": identity.hexdigest(),
        }
        return file_id

    # -- 日志 --------------------------------------------------------------

    def _commit_journal(self, j: Journal):
        atomic_write_json(self.journal_path, j.to_json(), self.fault)

    def _load_journal(self):
        with open(self.journal_path, "rb") as f:
            raw = f.read()
        j = Journal.from_json(raw)
        if j.job_hash != self.job_hash:
            raise ValueError("续接被拒绝：作业描述与日志不属同一作业")
        st = os.stat(self.data_path)
        if (st.st_size != j.file_id["size"]
                or st.st_dev != j.file_id["dev"]
                or st.st_ino != j.file_id["ino"]):
            raise ValueError("续接被拒绝：文件身份(大小/设备/inode)与日志不符")
        return j

    # -- 续接前逐槽复核：外部写入的损坏不能冒充正常续接 --------------------

    def _verify_resume_state(self, j: Journal):
        expected_new = [self.old_digests[self.layout[t]] for t in range(self.n)]

        # 已提交（日志已推进）的槽：内容必须等于目标摘要
        done = set(j.tree_order[: j.tree_cursor])
        for ci in range(j.cycle_index):
            done.update(j.cycles[ci])

        # 在飞槽：日志尚未推进、写盘可能正撕裂的唯一槽位。
        # 允许其内容为“原始”或“目标”两种一致状态之一。
        in_flight = -1
        active_cycle = None
        if j.stage == "rotate" and j.cycle_index < len(j.cycles):
            active_cycle = j.cycles[j.cycle_index]
            # rotate_cursor = 已提交步数；下一步（在飞）写 cyc[cursor]。
            # 活动环的已提交前缀也是 done。
            done.update(active_cycle[: j.rotate_cursor])
            if j.rotate_cursor < len(active_cycle):
                in_flight = active_cycle[j.rotate_cursor]
        elif j.stage == "spare" and j.tree_cursor < len(j.tree_order):
            # tree_cursor = 已提交树槽数；在飞的是下一树槽
            in_flight = j.tree_order[j.tree_cursor]

        problems = []
        for slot in range(self.n):
            buf = block_pread(self.fd, slot * self.block_size, self.block_size)
            got = sha256_hex(buf)
            if slot in done:
                if got != expected_new[slot]:
                    problems.append(
                        "槽 %d 已完成但内容不符（期望新摘要 %s 实测 %s）"
                        % (slot, expected_new[slot], got)
                    )
                continue
            if slot == in_flight:
                # 唯一允许处于撕裂状态的槽：必须是旧内容或目标内容之一
                if got not in (self.old_digests[slot], expected_new[slot]):
                    problems.append(
                        "在飞槽 %d 既非原始内容也非目标内容，存在外部损坏"
                        % slot
                    )
                continue
            # 其余未触槽必须仍是原始内容（撕裂只可能发生在在飞槽）
            if got != self.old_digests[slot]:
                problems.append(
                    "未触槽 %d 内容偏离原始摘要，存在外部损坏" % slot
                )

        # 备用块若存在，只可能属于 rotate 阶段的当前环；
        # spare 阶段崩溃时备用块可能是撕裂的，将被删除后重做。
        if os.path.exists(self.spare_path):
            if j.stage == "rotate" and active_cycle is not None:
                sst = os.stat(self.spare_path)
                if sst.st_size != self.block_size:
                    problems.append("备用块大小不是一个块长")
                else:
                    sfd = os.open(self.spare_path, os.O_RDONLY)
                    try:
                        sbuf = block_pread(sfd, 0, self.block_size)
                    finally:
                        os.close(sfd)
                    if sha256_hex(sbuf) != self.old_digests[active_cycle[0]]:
                        problems.append(
                            "备用块内容与当前环起点的原始摘要不符"
                        )
            elif j.stage == "spare":
                # 无法信任的半成品，直接删除并在后续重做
                os.unlink(self.spare_path)
                fsync_dir(self.sidecar)
            else:
                problems.append("存在不属于任何活动环的备用块文件")

        if problems:
            raise ValueError(
                "续接被拒绝，文件状态无法与日志对应：\n  " + "\n  ".join(problems)
            )

    # -- 主流程 ------------------------------------------------------------

    def run(self):
        # 已完成的同一作业：只核凭据，绝不再执行一遍布局
        if os.path.exists(self.cred_path):
            self._check_credential_only()
            print("该作业此前已宣布完成，未再改动数据。可用 verify 子命令全量核验。")
            return

        if os.path.exists(self.journal_path):
            os.makedirs(self.sidecar, exist_ok=True)
            self._open_lock()
            self.fd = os.open(self.data_path, os.O_RDWR)
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(self.fd)
                self.fd = None
                raise
            j = self._load_journal()
            self._verify_resume_state(j)
            print("续接身份与逐槽摘要核验通过，从断点继续。")
        else:
            # 首次写盘前的全部核验：任何异常都在本调用抛出，数据盘未被打开写
            file_id = self.preflight()
            os.makedirs(self.sidecar, exist_ok=True)
            fsync_dir(os.path.dirname(os.path.abspath(self.sidecar)) or ".")
            self._open_lock()
            tree_order, _on_cycle, cycles = decompose(self.n, self.layout)
            j = Journal(
                job_hash=self.job_hash,
                file_id=file_id,
                block_size=self.block_size,
                nblocks=self.n,
                tree_cursor=0,
                tree_order=tree_order,
                cycles=cycles,
                cycle_index=0,
                stage="spare",
                rotate_cursor=0,
            )
            # 首条日志落盘并 fsync 后，才允许第一次数据写
            self._commit_journal(j)
            self.fd = os.open(self.data_path, os.O_RDWR)
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(self.fd)
                self.fd = None
                raise
            print("预检通过：布局与全部 %d 个原始块摘要一致，作业开始。" % self.n)

        try:
            self._process_trees(j)
            self._process_cycles(j)
            self._finalize(j)
        finally:
            if self.spare_fd is not None:
                os.close(self.spare_fd)
                self.spare_fd = None
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            self._close_lock()

    # -- 树节点：子先于父，单块直写，天然幂等可重做 ------------------------

    def _process_trees(self, j: Journal):
        while j.tree_cursor < len(j.tree_order):
            t = j.tree_order[j.tree_cursor]
            src = self.layout[t]
            buf = block_pread(self.fd, src * self.block_size, self.block_size)
            if sha256_hex(buf) != self.old_digests[src]:
                raise ValueError(
                    "处理树槽 %d 时发现源旧槽 %d 内容已偏离原始摘要" % (t, src)
                )
            block_pwrite(self.fd, t * self.block_size, buf, "data", self.fault)
            j.tree_cursor += 1
            self._commit_journal(j)

    # -- 环：起点原始内容进唯一备用块，随后逐槽轮转，最后回收备用块 --------

    def _open_spare(self):
        if self.spare_fd is None:
            self.spare_fd = os.open(
                self.spare_path, os.O_RDWR | os.O_CREAT, 0o600
            )
            if os.fstat(self.spare_fd).st_size < self.block_size:
                os.ftruncate(self.spare_fd, self.block_size)

    def _process_cycles(self, j: Journal):
        while j.cycle_index < len(j.cycles):
            cyc = j.cycles[j.cycle_index]
            length = len(cyc)
            start = cyc[0]

            # 自环：layout[c0]=c0，目标内容就是自身旧内容，无需备用块
            if length == 1:
                buf = block_pread(self.fd, start * self.block_size,
                                  self.block_size)
                if sha256_hex(buf) != self.old_digests[start]:
                    raise ValueError("自环槽 %d 内容已偏离原始摘要" % start)
                j.cycle_index += 1
                j.stage = "spare"
                j.rotate_cursor = 0
                self._commit_journal(j)
                continue

            # 阶段 spare：spare <- 原始(c0)。先落盘并 fsync，再动任何环槽。
            if j.stage == "spare":
                self._open_spare()
                buf = block_pread(self.fd, start * self.block_size,
                                  self.block_size)
                if sha256_hex(buf) != self.old_digests[start]:
                    raise ValueError("环起点槽 %d 内容已偏离原始摘要" % start)
                block_pwrite(self.spare_fd, 0, buf, "spare", self.fault)
                j.stage = "rotate"
                j.rotate_cursor = 0
                self._commit_journal(j)

            # 阶段 rotate，cursor k 表示要完成第 k 个槽 cyc[k]：
            #   k < L-1 : cyc[k] <- 原始(cyc[k+1])，该源槽此刻尚未被写过；
            #   k = L-1 : cyc[L-1] <- spare（即原始(c0)）。
            # 每步先写后提交；崩溃后重做同一步，读取内容不变，天然幂等。
            if j.stage == "rotate":
                while j.rotate_cursor < length:
                    k = j.rotate_cursor
                    dst = cyc[k]
                    if k < length - 1:
                        src_slot = cyc[k + 1]
                        buf = block_pread(
                            self.fd, src_slot * self.block_size, self.block_size
                        )
                        if sha256_hex(buf) != self.old_digests[src_slot]:
                            raise ValueError(
                                "环内源槽 %d 的原始内容已缺失，无法轮转"
                                % src_slot
                            )
                    else:
                        self._open_spare()
                        buf = block_pread(self.spare_fd, 0, self.block_size)
                        if sha256_hex(buf) != self.old_digests[start]:
                            raise ValueError(
                                "备用块内容与环起点 %d 的原始摘要不符" % start
                            )
                    block_pwrite(
                        self.fd, dst * self.block_size, buf, "data", self.fault
                    )
                    j.rotate_cursor += 1
                    self._commit_journal(j)

                # 本环全部落盘并 fsync 后，先提交完成日志，再删备用块：
                #   提交前崩溃 -> rotate 阶段，cursor==L，仅重做提交；
                #   提交后、删除前崩溃 -> 续接核验清理这份无用备用块。
                if self.spare_fd is not None:
                    os.close(self.spare_fd)
                    self.spare_fd = None
                j.cycle_index += 1
                j.stage = "spare"
                j.rotate_cursor = 0
                self._commit_journal(j)
                if os.path.exists(self.spare_path):
                    os.unlink(self.spare_path)
                    fsync_dir(self.sidecar)

    # -- 完成：按原声明核对每个新槽，再写完成凭据 --------------------------

    def _finalize(self, j: Journal):
        expected_new = [self.old_digests[self.layout[t]] for t in range(self.n)]
        manifest_digests = []
        for t in range(self.n):
            buf = block_pread(self.fd, t * self.block_size, self.block_size)
            got = sha256_hex(buf)
            if got != expected_new[t]:
                raise ValueError(
                    "完工核验失败：新槽 %d 摘要 %s 与声明 %s 不符"
                    % (t, got, expected_new[t])
                )
            manifest_digests.append(got)
        os.fsync(self.fd)
        cred = {
            "job_hash": self.job_hash,
            "file_id": j.file_id,
            "block_size": self.block_size,
            "nblocks": self.n,
            "new_digests": manifest_digests,
        }
        body = json.dumps(cred, sort_keys=True,
                          separators=(",", ":")).encode()
        cred["sig"] = sha256_hex(body)
        atomic_write_json(
            self.cred_path,
            json.dumps(cred, sort_keys=True, separators=(",", ":")).encode(),
            self.fault,
        )
        print("全部 %d 个新槽按原声明核验通过，完成凭据已写入。" % self.n)

    def _check_credential_only(self):
        with open(self.cred_path, "rb") as f:
            d = json.loads(f.read().decode("utf-8"))
        sig = d.pop("sig", None)
        body = json.dumps(d, sort_keys=True,
                          separators=(",", ":")).encode()
        if not sig or sig != sha256_hex(body):
            raise ValueError("完成凭据签名不符：凭据被外部改动过")
        if d.get("job_hash") != self.job_hash:
            raise ValueError("完成凭据属于另一个作业描述")
        st = os.stat(self.data_path)
        fid = d["file_id"]
        if (st.st_size != fid["size"] or st.st_dev != fid["dev"]
                or st.st_ino != fid["ino"]):
            raise ValueError("完成凭据中的文件身份与当前文件不符")

    # -- 全量核验子命令 ----------------------------------------------------

    def verify(self):
        with open(self.cred_path, "rb") as f:
            d = json.loads(f.read().decode("utf-8"))
        sig = d.pop("sig", None)
        body = json.dumps(d, sort_keys=True,
                          separators=(",", ":")).encode()
        if not sig or sig != sha256_hex(body):
            raise ValueError("完成凭据签名不符")
        if d.get("job_hash") != self.job_hash:
            raise ValueError("凭据属于另一个作业描述")
        st = os.stat(self.data_path)
        fid = d["file_id"]
        if (st.st_size != fid["size"] or st.st_dev != fid["dev"]
                or st.st_ino != fid["ino"]):
            raise ValueError("文件身份与凭据不符")
        fd = os.open(self.data_path, os.O_RDONLY)
        try:
            for t in range(self.n):
                buf = block_pread(fd, t * self.block_size, self.block_size)
                if sha256_hex(buf) != d["new_digests"][t]:
                    raise ValueError("新槽 %d 实测摘要与凭据不符" % t)
        finally:
            os.close(fd)
        print("verify 通过：%d 个新槽全部与完成凭据一致。" % self.n)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def load_job(path):
    with open(path, "rb") as f:
        job = json.loads(f.read().decode("utf-8"))
    # 规范字段校验
    for key in ("block_size", "layout", "old_digests"):
        if key not in job:
            raise ValueError("作业描述缺少字段: %s" % key)
    return job


def default_sidecar(data_path):
    return os.path.abspath(data_path) + ".rearrange.d"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="固定块数据文件离线原地重排（崩溃可续、单块备用）"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="执行或续接一个重排作业")
    p_run.add_argument("--data", required=True, help="数据文件路径")
    p_run.add_argument("--job", required=True, help="作业描述 JSON")
    p_run.add_argument("--sidecar", help="元数据目录（默认 <data>.rearrange.d）")
    p_run.add_argument(
        "--fault", action="append", default=[],
        help="故障注入规则（测试用），可重复，格式见模块头注释",
    )

    p_ver = sub.add_parser("verify", help="按完成凭据全量核验数据文件")
    p_ver.add_argument("--data", required=True)
    p_ver.add_argument("--job", required=True)
    p_ver.add_argument("--sidecar")
    p_ver.add_argument(
        "--fault", action="append", default=[],
        help="故障注入规则（测试用），可重复，格式见模块头注释",
    )

    args = parser.parse_args(argv)
    job = load_job(args.job)
    sidecar = args.sidecar or default_sidecar(args.data)
    fault = FaultInjection(args.fault)
    r = Rearranger(args.data, job, sidecar, fault)

    try:
        if args.cmd == "run":
            r.run()
        else:
            r.verify()
    except ValueError as exc:
        # 区分预检失败与续接拒绝，便于调用方判断“数据盘是否被动过”
        code = EXIT_PREFLIGHT
        if os.path.exists(os.path.join(sidecar, JOURNAL_NAME)):
            code = EXIT_RESUME_REJECTED
        print("错误: %s" % exc, file=sys.stderr)
        return code
    except BlockingIOError:
        print("错误: 文件已被同一作业的另一个进程独占", file=sys.stderr)
        return EXIT_LOCK
    except OSError as exc:
        print("IO 错误: %s" % exc, file=sys.stderr)
        return EXIT_IO
    return EXIT_OK


if __name__ == "__main__":
    # 让 SIGTERM 走默认处置（等价于被杀），不做缓冲之外的特殊清理
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    sys.exit(main())

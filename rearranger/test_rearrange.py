#!/usr/bin/env python3
"""rearrange.py 的离线故障注入测试夹具（不依赖第三方库）。

覆盖：
  1. 小文件上的正常端到端重排 + verify 凭据核验；
  2. 固定矩阵：在数据/备用块的 pwrite、fsync 与日志提交的四个边界上
     分别注入短写与杀进程（os._exit），随后重开同一作业续接到正确布局；
  3. 随机故障风暴：随机布局 + 随机多次杀/短写 + 多次续接；
  4. 首次写盘前的布局/摘要错误必须被拒绝且数据盘零改动；
  5. 续接绑定：改作业描述、改数据文件、改日志均被拒绝；
  6. 已完成作业重开不会再执行布局；
  7. 资源约束：载荷缓冲随块长而非文件大小；备用载荷文件至多一个块长。

用法：
  python3 -I test_rearrange.py
"""

import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "rearrange.py")

# 与主程序保持一致的退出码
EXIT_OK = 0
EXIT_PREFLIGHT = 3
EXIT_RESUME_REJECTED = 4
EXIT_LOCK = 6

BLOCK_SIZE = 16  # 小文件复现“短写”：块只有 16 字节


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def make_job(workdir, nblocks, layout=None, seed=0):
    """造一个数据文件和对应的作业描述，返回 (data_path, job_path, job)。"""
    rng = random.Random(seed)
    blocks = []
    for i in range(nblocks):
        # 每块带唯一标记，保证内容两两不同、摘要有判别力
        blocks.append(("blk%04d-" % i).encode() + os.urandom(BLOCK_SIZE - 8))
    data_path = os.path.join(workdir, "data.bin")
    with open(data_path, "wb") as f:
        for b in blocks:
            f.write(b)
        f.flush()
        os.fsync(f.fileno())
    if layout is None:
        layout = [rng.randrange(nblocks) for _ in range(nblocks)]
    job = {
        "version": 1,
        "block_size": BLOCK_SIZE,
        "layout": layout,
        "old_digests": [sha256_hex(b) for b in blocks],
        "digest_alg": "sha256",
    }
    job_path = os.path.join(workdir, "job.json")
    with open(job_path, "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False)
    return data_path, job_path, job, blocks


def expected_blocks(blocks, layout):
    return [blocks[layout[t]] for t in range(len(layout))]


def run_tool(data_path, job_path, sidecar, faults=None, expect_rc=0):
    cmd = [sys.executable, "-I", TOOL, "run",
           "--data", data_path, "--job", job_path, "--sidecar", sidecar]
    for rule in faults or ():
        cmd += ["--fault", rule]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != expect_rc:
        raise AssertionError(
            "命令退出码 %d != 期望 %d\nCMD: %s\nSTDOUT:\n%s\nSTDERR:\n%s"
            % (proc.returncode, expect_rc, " ".join(cmd),
               proc.stdout, proc.stderr)
        )
    return proc


def verify_tool(data_path, job_path, sidecar):
    cmd = [sys.executable, "-I", TOOL, "verify",
           "--data", data_path, "--job", job_path, "--sidecar", sidecar]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != EXIT_OK:
        raise AssertionError("verify 失败:\n%s\n%s" % (proc.stdout, proc.stderr))


def assert_final_layout(data_path, want_blocks):
    with open(data_path, "rb") as f:
        raw = f.read()
    got = [raw[i * BLOCK_SIZE:(i + 1) * BLOCK_SIZE]
           for i in range(len(want_blocks))]
    if got != want_blocks:
        for i, (g, w) in enumerate(zip(got, want_blocks)):
            if g != w:
                raise AssertionError(
                    "新槽 %d 内容错误\n got=%r\nwant=%r" % (i, g, w)
                )
        raise AssertionError("最终布局错误（块数不一致）")


def sidecar_entries(sidecar):
    if not os.path.isdir(sidecar):
        return {}
    out = {}
    for name in os.listdir(sidecar):
        out[name] = os.path.getsize(os.path.join(sidecar, name))
    return out


# ---------------------------------------------------------------------------
# 1. 端到端正常路径
# ---------------------------------------------------------------------------

def test_happy_path():
    with tempfile.TemporaryDirectory() as d:
        data, job_path, job, blocks = make_job(d, 12, seed=42)
        side = os.path.join(d, "sc")
        run_tool(data, job_path, side)
        assert_final_layout(data, expected_blocks(blocks, job["layout"]))
        verify_tool(data, job_path, side)
        entries = sidecar_entries(side)
        assert CRED_NAME in entries
        # 正常完工后备用块不得残留
        assert SPARE_NAME not in entries, entries
        print("  ok 端到端 + verify")


# ---------------------------------------------------------------------------
# 2. 固定故障矩阵：每个注入点先打断一次，再续接完成
# ---------------------------------------------------------------------------

JOURNAL_POINTS = ["tmpwrite", "tmpfsync", "rename", "dirfsync"]
DATA_POINTS = ["pwrite", "fsync"]


def test_fault_matrix():
    # 覆盖纯树、纯环、树+环混合几种函数图
    layouts = {
        "tree_feeding_cycle": [1, 2, 1, 2, 0],      # 环(1,2)，树节点喂入
        "multi_cycle": [1, 0, 4, 3, 3],             # 环(0,1) 与自环(3)，4->3
        "fan_in": [2, 2, 2, 2, 2],                  # 多目标供数（自环 2）
        "identity": [0, 1, 2, 3, 4],                # 全部自环
        "big_cycle": [1, 2, 3, 4, 0],               # 五环
    }

    def has_real_cycle(lay):
        # 是否存在长度 >=2 的环（只有这种环才会写备用块）
        n = len(lay)
        seen = [False] * n
        for start in range(n):
            if seen[start]:
                continue
            path = []
            node = start
            while not seen[node]:
                seen[node] = True
                path.append(node)
                node = lay[node]
            if node in path and len(path) - path.index(node) >= 2:
                return True
        return False

    case_no = 0
    for name, layout in layouts.items():
        n = len(layout)
        spare_applies = has_real_cycle(layout)
        for target, points in (("data", DATA_POINTS),
                               ("spare", DATA_POINTS),
                               ("journal", JOURNAL_POINTS)):
            # 备用块故障只在真正会生成备用块的布局上注入
            if target == "spare" and not spare_applies:
                continue
            for point in points:
                for action in ("short", "exit"):
                    for call in (1, 2, 3):
                        case_no += 1
                        with tempfile.TemporaryDirectory() as d:
                            data, job_path, job, blocks = make_job(
                                d, n, layout=layout, seed=case_no
                            )
                            side = os.path.join(d, "sc")
                            rule = "%s:%s:%d:%s" % (target, point, call, action)
                            # exit 规则若始终不触发，进程会正常完成（rc=0）；
                            # 因此按实际退出码分支，而不是假定一定被杀。
                            proc = subprocess.run(
                                [sys.executable, "-I", TOOL, "run",
                                 "--data", data, "--job", job_path,
                                 "--sidecar", side, "--fault", rule],
                                capture_output=True, text=True)
                            if action == "exit" and proc.returncode == 137:
                                # 确实被打断：续接到完成
                                run_tool(data, job_path, side)
                            elif proc.returncode != 0:
                                raise AssertionError(
                                    "用例 %s 意外退出码 %d\n%s\n%s"
                                    % (rule, proc.returncode,
                                       proc.stdout, proc.stderr))
                            elif "完成凭据已写入" not in proc.stdout:
                                # short 把某步打裂但进程仍退出 0 的兜底续接
                                run_tool(data, job_path, side)
                            assert_final_layout(
                                data, expected_blocks(blocks, layout)
                            )
                            verify_tool(data, job_path, side)
    print("  ok 固定故障矩阵（%d 个用例，含短写与杀进程续接）" % case_no)


# ---------------------------------------------------------------------------
# 3. 随机故障风暴：同一作业反复被杀/短写，多次续接
# ---------------------------------------------------------------------------

def test_random_storm(trials=40):
    rng = random.Random(20261124)
    all_rules = []
    for target, points in (("data", DATA_POINTS),
                           ("spare", DATA_POINTS),
                           ("journal", JOURNAL_POINTS)):
        for point in points:
            all_rules.append((target, point))
    for trial in range(trials):
        with tempfile.TemporaryDirectory() as d:
            n = rng.randint(2, 14)
            data, job_path, job, blocks = make_job(d, n, seed=1000 + trial)
            side = os.path.join(d, "sc")
            want = expected_blocks(blocks, job["layout"])
            finished = False
            rounds = 0
            while not finished:
                rounds += 1
                cmd = [sys.executable, "-I", TOOL, "run",
                       "--data", data, "--job", job_path, "--sidecar", side]
                # 约 40% 的轮次做无故障重开（模拟运维“再开一次作业”），
                # 否则极小作业会在每轮早期反复被杀，永远到不了完工。
                if rng.random() < 0.6:
                    for _ in range(rng.randint(1, 3)):
                        target, point = rng.choice(all_rules)
                        call = rng.randint(1, 6)
                        action = rng.choice(
                            ["short", "short:2", "exit", "exit:9"])
                        cmd += ["--fault",
                                "%s:%s:%d:%s" % (target, point, call, action)]
                proc = subprocess.run(cmd, capture_output=True, text=True)
                if proc.returncode not in (0, 9, 137):
                    raise AssertionError(
                        "随机试验 %d 意外退出码 %d\n%s\n%s"
                        % (trial, proc.returncode, proc.stdout, proc.stderr)
                    )
                finished = proc.returncode == 0 and \
                    "完成凭据已写入" in proc.stdout
                if rounds > 60:
                    raise AssertionError("随机试验 %d 续接轮次超限" % trial)
            assert_final_layout(data, want)
            verify_tool(data, job_path, side)
    print("  ok 随机故障风暴（%d 个随机布局，每个最多 60 次续接）" % trials)


# ---------------------------------------------------------------------------
# 4. 预检失败：首次写盘前发现错误，数据盘一字节都不能动
# ---------------------------------------------------------------------------

def test_preflight_rejects_before_touching_disk():
    with tempfile.TemporaryDirectory() as d:
        data, job_path, job, blocks = make_job(d, 6, seed=7)
        before = open(data, "rb").read()
        st_before = os.stat(data)

        # 4a. 摘要声明错误
        bad = dict(job)
        bad["old_digests"] = list(job["old_digests"])
        bad["old_digests"][2] = "00" * 32
        bad_path = os.path.join(d, "bad_digest.json")
        json.dump(bad, open(bad_path, "w"))
        run_tool(data, bad_path, os.path.join(d, "sc1"), expect_rc=EXIT_PREFLIGHT)

        # 4b. 布局越界
        bad = dict(job)
        bad["layout"] = list(job["layout"])
        bad["layout"][0] = 99
        bad_path = os.path.join(d, "bad_layout.json")
        json.dump(bad, open(bad_path, "w"))
        run_tool(data, bad_path, os.path.join(d, "sc2"), expect_rc=EXIT_PREFLIGHT)

        # 4c. 块大小与文件大小不匹配
        bad = dict(job)
        bad["block_size"] = BLOCK_SIZE + 1
        bad_path = os.path.join(d, "bad_size.json")
        json.dump(bad, open(bad_path, "w"))
        run_tool(data, bad_path, os.path.join(d, "sc3"), expect_rc=EXIT_PREFLIGHT)

        after = open(data, "rb").read()
        st_after = os.stat(data)
        assert before == after, "预检失败后数据文件内容被改动"
        assert (st_before.st_size, st_before.st_ino) == \
               (st_after.st_size, st_after.st_ino), "预检失败后文件被替换"
        for sc in ("sc1", "sc2", "sc3"):
            assert not os.path.exists(os.path.join(d, sc)), \
                "预检失败不得留下作业目录"
    print("  ok 预检失败零写盘")


# ---------------------------------------------------------------------------
# 5. 续接绑定：外部写入损坏不能冒充正常续接
# ---------------------------------------------------------------------------

def start_interrupted_job(d, fault="data:pwrite:2:exit"):
    data, job_path, job, blocks = make_job(d, 8, seed=11)
    side = os.path.join(d, "sc")
    run_tool(data, job_path, side, faults=[fault], expect_rc=137)
    assert os.path.exists(os.path.join(side, "journal.json"))
    return data, job_path, side, job, blocks


def test_resume_binding():
    # 5a. 篡改数据文件 -> 拒绝
    with tempfile.TemporaryDirectory() as d:
        data, job_path, side, job, blocks = start_interrupted_job(d)
        with open(data, "r+b") as f:
            f.seek(BLOCK_SIZE + 3)
            f.write(b"\xff\xff\xff")
        run_tool(data, job_path, side, expect_rc=EXIT_RESUME_REJECTED)

    # 5b. 篡改日志 -> 签名不符拒绝
    with tempfile.TemporaryDirectory() as d:
        data, job_path, side, *_ = start_interrupted_job(d)
        jp = os.path.join(side, "journal.json")
        j = json.load(open(jp))
        j["tree_cursor"] = j["tree_cursor"] + 1
        json.dump(j, open(jp, "w"))
        run_tool(data, job_path, side, expect_rc=EXIT_RESUME_REJECTED)

    # 5c. 换一份不同的作业描述（同一文件、同一日志目录）-> 拒绝
    with tempfile.TemporaryDirectory() as d:
        data, job_path, side, job, blocks = start_interrupted_job(d)
        other = dict(job)
        other["layout"] = list(reversed(job["layout"]))
        other_path = os.path.join(d, "other_job.json")
        json.dump(other, open(other_path, "w"))
        run_tool(data, other_path, side, expect_rc=EXIT_RESUME_REJECTED)

    # 5d. 把数据文件换成同长度的另一个文件 -> 文件身份/摘要不符拒绝
    with tempfile.TemporaryDirectory() as d:
        data, job_path, side, *_ = start_interrupted_job(d)
        with open(data, "r+b") as f:
            f.seek(0)
            f.write(os.urandom(BLOCK_SIZE))  # 改一个未完成槽的原始内容
        run_tool(data, job_path, side, expect_rc=EXIT_RESUME_REJECTED)

    # 5e. 未受破坏的断点可以正常续接（对照组）
    with tempfile.TemporaryDirectory() as d:
        data, job_path, side, job, blocks = start_interrupted_job(d)
        run_tool(data, job_path, side)
        assert_final_layout(data, expected_blocks(blocks, job["layout"]))

    # 5f. 只有空侧车目录、没有日志：按全新作业走预检，不得凭空续接
    with tempfile.TemporaryDirectory() as d:
        data, job_path, job, blocks = make_job(d, 5, seed=13)
        side = os.path.join(d, "empty_sc")
        os.makedirs(side)
        run_tool(data, job_path, side)
        assert_final_layout(data, expected_blocks(blocks, job["layout"]))
    print("  ok 续接绑定与篡改拒绝")


def test_credential_tamper_rejected():
    # 完工后篡改凭据：run 与 verify 都必须拒绝
    with tempfile.TemporaryDirectory() as d:
        data, job_path, job, blocks = make_job(d, 6, seed=17)
        side = os.path.join(d, "sc")
        run_tool(data, job_path, side)
        cp = os.path.join(side, CRED_NAME)
        cred = json.load(open(cp))
        cred["new_digests"][0] = "aa" * 32
        json.dump(cred, open(cp, "w"))
        run_tool(data, job_path, side, expect_rc=EXIT_RESUME_REJECTED)
        cmd = [sys.executable, "-I", TOOL, "verify",
               "--data", data, "--job", job_path, "--sidecar", side]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        assert proc.returncode != 0 and "签名不符" in proc.stderr
    print("  ok 完成凭据篡改拒绝")


# ---------------------------------------------------------------------------
# 6. 完成的作业重开不得再执行布局
# ---------------------------------------------------------------------------

def test_completed_job_does_not_rerun():
    with tempfile.TemporaryDirectory() as d:
        data, job_path, job, blocks = make_job(d, 7, seed=5)
        side = os.path.join(d, "sc")
        run_tool(data, job_path, side)
        snapshot = open(data, "rb").read()
        proc = run_tool(data, job_path, side)
        assert "已宣布完成" in proc.stdout
        assert open(data, "rb").read() == snapshot
        verify_tool(data, job_path, side)
    print("  ok 完成作业重开不重排")


# ---------------------------------------------------------------------------
# 7. 独占锁：两个进程不能同时作业
# ---------------------------------------------------------------------------

def test_exclusive_lock():
    with tempfile.TemporaryDirectory() as d:
        data, job_path, _job, _blocks = make_job(d, 4, seed=3)
        side = os.path.join(d, "sc")
        # 第一个数据块 fsync 处永久挂起，稳定占住独占锁
        holder = subprocess.Popen(
            [sys.executable, "-I", TOOL, "run",
             "--data", data, "--job", job_path, "--sidecar", side,
             "--fault", "data:fsync:1:hang"]
        )
        try:
            # 等日志出现，确保 holder 已拿到锁
            import time
            for _ in range(100):
                if os.path.exists(os.path.join(side, "journal.json")):
                    break
                time.sleep(0.05)
            else:
                holder.kill()
                raise AssertionError("holder 未在预期时间内开始作业")
            run_tool(data, job_path, side, expect_rc=EXIT_LOCK)
        finally:
            holder.terminate()
            holder.wait()
        # 锁随进程释放，续接可完成
        run_tool(data, job_path, side)
    print("  ok 独占锁")


# ---------------------------------------------------------------------------
# 8. 备用载荷文件任何时刻不超过一个块长，且不含第二份载荷
# ---------------------------------------------------------------------------

def test_spare_size_bound():
    with tempfile.TemporaryDirectory() as d:
        # 大环作业，故障打在备用块写之后
        layout = [1, 2, 3, 4, 5, 0]
        data, job_path, job, blocks = make_job(d, 6, layout=layout, seed=9)
        side = os.path.join(d, "sc")
        run_tool(data, job_path, side,
                 faults=["spare:fsync:1:exit"], expect_rc=137)
        entries = sidecar_entries(side)
        assert SPARE_NAME in entries, entries
        assert entries[SPARE_NAME] == BLOCK_SIZE, entries
        # 续接完成后备用块消失
        run_tool(data, job_path, side)
        assert SPARE_NAME not in sidecar_entries(side)
    print("  ok 备用块大小上限与完工清理")


# ---------------------------------------------------------------------------
# 9. 规模与资源约束：大文件只允许块级缓冲，禁止整文件映像/mmap
# ---------------------------------------------------------------------------

def test_scale_and_buffer_bound():
    nblocks = 400
    bs = 4096
    with tempfile.TemporaryDirectory() as d:
        rng = random.Random(77)
        blocks = [os.urandom(bs) for _ in range(nblocks)]
        # 构造一个含若干大环与大量树节点的布局
        layout = []
        for t in range(nblocks):
            if t < 50:
                layout.append((t + 1) % 50)          # 一个 50 节点环
            else:
                layout.append(rng.randrange(nblocks))
        data = os.path.join(d, "big.bin")
        with open(data, "wb") as f:
            for b in blocks:
                f.write(b)
            os.fsync(f.fileno())
        job = {
            "version": 1, "block_size": bs, "layout": layout,
            "old_digests": [sha256_hex(b) for b in blocks],
            "digest_alg": "sha256",
        }
        job_path = os.path.join(d, "big_job.json")
        json.dump(job, open(job_path, "w"))
        side = os.path.join(d, "sc")
        # 中途杀一次再续接
        run_tool(data, job_path, side,
                 faults=["data:pwrite:120:exit"], expect_rc=137)
        run_tool(data, job_path, side)
        with open(data, "rb") as f:
            for t in range(nblocks):
                got = f.read(bs)
                assert got == blocks[layout[t]], "新槽 %d 内容错误" % t
        verify_tool(data, job_path, side)
    print("  ok %d 块规模作业（含 50 节点环与中断续接）" % nblocks)


def test_no_whole_file_buffers_in_source():
    """结构性护栏：数据文件的载荷 IO 只能按 block_size 走，
    不允许 mmap、对数据描述符整文件 read/os.read 或随文件大小增长的缓冲。
    元数据文件（日志/作业/凭据）体量与文件大小无关，允许整读。"""
    src = open(TOOL, encoding="utf-8").read()
    forbidden = ["import mmap", "mmap(", "self.fd.read", "os.read("]
    for token in forbidden:
        assert token not in src, "源码出现被禁止的整文件缓冲方式: %r" % token
    # 数据文件的每次读长度必须是 self.block_size，写长度由块缓冲决定
    assert "block_pread(self.fd, " in src
    assert ", self.block_size)" in src
    print("  ok 源码级载荷缓冲护栏（仅块级 IO，无 mmap/整文件读）")


def main():
    tests = [
        test_happy_path,
        test_preflight_rejects_before_touching_disk,
        test_resume_binding,
        test_credential_tamper_rejected,
        test_completed_job_does_not_rerun,
        test_exclusive_lock,
        test_spare_size_bound,
        test_scale_and_buffer_bound,
        test_no_whole_file_buffers_in_source,
        test_fault_matrix,
        test_random_storm,
    ]
    for t in tests:
        print("[%s]" % t.__name__)
        t()
    print("\n全部测试通过。")


CRED_NAME = "credential.json"
SPARE_NAME = "spare.block"

if __name__ == "__main__":
    main()

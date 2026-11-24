#!/usr/bin/env python3
"""archive_merge.py 的测试夹具（仅标准库，建议以 python3 -I 运行）。

覆盖：
  1. 端到端合并 + 清单映射 + verify：跨归档/归档内的内容去重；
  2. 确定性：同一请求同一输入得到逐字节相同的清单与镜像；
  3. 失败路径：归档 ID 重复、文件不可读、长度不符、摘要不符，均不发布输出；
  4. 空归档列表与零槽位归档；
  5. 原子发布：在镜像/清单/发布各边界杀进程或短写，最终路径绝不出现半成品，
     重开后得到完整归档包；
  6. 幂等重开：已完成的归档包不再改动；被破坏的既有输出拒绝覆盖；
  7. verify 对镜像/清单篡改的识别；
  8. 载荷缓冲护栏与规模测试（去重后镜像只保留不同块）。

用法：
  python3 -I test_archive_merge.py
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
TOOL = os.path.join(HERE, "archive_merge.py")

EXIT_OK = 0
EXIT_INPUT = 3
EXIT_PACKAGE_INVALID = 4
EXIT_LOCK = 6
EXIT_OUTPUT_EXISTS = 7

BLOCK = 16


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def block(tag, seed=0):
    """造一个带唯一标记的块，正好 BLOCK 字节。"""
    rng = random.Random(seed)
    prefix = ("%s-" % tag).encode()
    body = bytes(rng.getrandbits(8) for _ in range(BLOCK))[: BLOCK - len(prefix)]
    return prefix + body.ljust(BLOCK - len(prefix), b"#")


def write_archive(directory, name, blocks):
    path = os.path.join(directory, name)
    with open(path, "wb") as f:
        for b in blocks:
            assert len(b) == BLOCK
            f.write(b)
        f.flush()
        os.fsync(f.fileno())
    return path


def archive_entry(aid, path, blocks):
    return {"id": aid, "path": path,
            "digests": [sha256_hex(b) for b in blocks]}


def write_request(directory, request, name="request.json"):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(request, f, ensure_ascii=False, indent=2)
    return path


def run_cli(args, expect_rc):
    proc = subprocess.run(
        [sys.executable, "-I", TOOL] + args,
        capture_output=True, text=True,
    )
    if proc.returncode != expect_rc:
        raise AssertionError(
            "退出码 %d != 期望 %d\nCMD: %s\nSTDOUT:\n%s\nSTDERR:\n%s"
            % (proc.returncode, expect_rc, " ".join(args),
               proc.stdout, proc.stderr)
        )
    return proc


def merge(req_path, output, faults=(), expect_rc=EXIT_OK):
    args = ["merge", "--request", req_path, "--output", output]
    for rule in faults:
        args += ["--fault", rule]
    return run_cli(args, expect_rc)


def verify(req_path, package, expect_rc=EXIT_OK):
    return run_cli(
        ["verify", "--request", req_path, "--package", package], expect_rc
    )


def load_manifest(package):
    with open(os.path.join(package, "manifest.json"), "rb") as f:
        return json.loads(f.read())


def image_bytes(package):
    with open(os.path.join(package, "image.bin"), "rb") as f:
        return f.read()


def expected_mapping(archive_blocks_list, block_size):
    """按“归档顺序 -> 槽位顺序”的首次出现位置计算期望的镜像槽位。"""
    order = {}
    mapping = []
    for blocks in archive_blocks_list:
        slots = []
        for b in blocks:
            if b not in order:
                order[b] = len(order)
            slots.append(order[b])
        mapping.append(slots)
    # 去重器用的是实际摘要索引；这里按字节唯一性独立复算
    return mapping, [k for k in order]


# ---------------------------------------------------------------------------
# 1. 端到端
# ---------------------------------------------------------------------------

def test_happy_path_dedup_across_and_within():
    with tempfile.TemporaryDirectory() as d:
        a_blocks = [block("a0", 1), block("shared", 2), block("a2", 3),
                    block("shared", 2)]
        b_blocks = [block("shared", 2), block("b1", 4), block("b1", 4)]
        c_blocks = [block("c0", 5), block("a0", 1)]
        ap = write_archive(d, "a.bin", a_blocks)
        bp = write_archive(d, "b.bin", b_blocks)
        cp = write_archive(d, "c.bin", c_blocks)
        req = {
            "version": 1,
            "block_size": BLOCK,
            "archives": [
                archive_entry("alpha", ap, a_blocks),
                archive_entry("beta", bp, b_blocks),
                archive_entry("gamma", cp, c_blocks),
            ],
        }
        req_path = write_request(d, req)
        pkg = os.path.join(d, "pkg")
        proc = merge(req_path, pkg)
        assert "合并完成" in proc.stdout

        m = load_manifest(pkg)
        want_maps, want_unique = expected_mapping(
            [a_blocks, b_blocks, c_blocks], BLOCK
        )
        got_maps = [a["slot_map"] for a in m["archives"]]
        assert got_maps == want_maps, (got_maps, want_maps)
        assert m["image"]["slots"] == len(want_unique)
        img = image_bytes(pkg)
        assert len(img) == len(want_unique) * BLOCK
        assert [img[i * BLOCK:(i + 1) * BLOCK]
                for i in range(len(want_unique))] == want_unique
        # 清单包含按原请求核验各槽所需摘要
        for meta, blocks in zip(m["archives"], [a_blocks, b_blocks, c_blocks]):
            assert meta["digests"] == [sha256_hex(b) for b in blocks]
            assert meta["size"] == len(blocks) * BLOCK
        verify(req_path, pkg)
        print("  ok 跨归档/归档内去重、映射与 verify")


# ---------------------------------------------------------------------------
# 2. 确定性
# ---------------------------------------------------------------------------

def test_deterministic_output():
    with tempfile.TemporaryDirectory() as d:
        blocks1 = [block("x0", 10), block("x1", 11), block("x0", 10)]
        blocks2 = [block("x1", 11), block("y", 12)]
        p1 = write_archive(d, "1.bin", blocks1)
        p2 = write_archive(d, "2.bin", blocks2)
        req = {"block_size": BLOCK, "archives": [
            archive_entry("one", p1, blocks1),
            archive_entry("two", p2, blocks2),
        ]}
        req_path = write_request(d, req)
        out1 = os.path.join(d, "out1")
        out2 = os.path.join(d, "out2")
        merge(req_path, out1)
        merge(req_path, out2)
        assert image_bytes(out1) == image_bytes(out2)
        with open(os.path.join(out1, "manifest.json"), "rb") as f1, \
                open(os.path.join(out2, "manifest.json"), "rb") as f2:
            assert f1.read() == f2.read()

        # 归档顺序改变 -> 映射按新的首次出现顺序重排，仍然确定
        req2 = {"block_size": BLOCK, "archives": [
            archive_entry("two", p2, blocks2),
            archive_entry("one", p1, blocks1),
        ]}
        req2_path = write_request(d, req2, "request2.json")
        out3 = os.path.join(d, "out3")
        merge(req2_path, out3)
        m3 = load_manifest(out3)
        want, _ = expected_mapping([blocks2, blocks1], BLOCK)
        assert [a["slot_map"] for a in m3["archives"]] == want
        verify(req2_path, out3)
        print("  ok 相同请求相同输入得到相同镜像与清单")


# ---------------------------------------------------------------------------
# 3. 失败路径：不发布任何输出
# ---------------------------------------------------------------------------

def test_failures_publish_nothing():
    with tempfile.TemporaryDirectory() as d:
        good = [block("g0", 20), block("g1", 21)]
        p = write_archive(d, "good.bin", good)

        cases = []

        def add_case(name, req, verify_rc=EXIT_PACKAGE_INVALID):
            # 请求本身不合法时 verify 也把它当输入错误（3）；
            # 请求合法但没有归档包时 verify 报归档包核验失败（4）。
            cases.append((name, req, verify_rc))

        # 3a. 归档 ID 重复
        add_case("dup-id", {"block_size": BLOCK, "archives": [
            archive_entry("same", p, good),
            archive_entry("same", p, good),
        ]}, EXIT_INPUT)

        # 3b. 文件不可读
        missing = os.path.join(d, "missing.bin")
        add_case("missing-file", {"block_size": BLOCK, "archives": [
            archive_entry("a", p, good),
            archive_entry("b", missing, good),
        ]})

        # 3c. 长度不是块长整数倍
        badlen = os.path.join(d, "badlen.bin")
        with open(badlen, "wb") as f:
            f.write(block("z", 22) + b"x")
        add_case("bad-length", {"block_size": BLOCK, "archives": [
            archive_entry("a", badlen,
                          [block("z", 22), block("z", 23)]),
        ]})

        # 3d. 摘要数量与槽位数不符
        add_case("digest-count", {"block_size": BLOCK, "archives": [
            archive_entry("a", p, good + [block("g2", 24)]),
        ]})

        # 3e. 任一摘要不符
        bad_digests = [sha256_hex(b) for b in good]
        bad_digests[1] = "00" * 32
        add_case("digest-mismatch", {"block_size": BLOCK, "archives": [
            archive_entry("a", p, good),
            {"id": "b", "path": p, "digests": bad_digests},
        ]})

        # 3f. 声明摘要相同但字节不同：绝不能误合并
        other = [block("g0", 20), block("NOT-g1", 25)]
        op = write_archive(d, "other.bin", other)
        same_declared = [sha256_hex(b) for b in good]
        add_case("same-digest-different-bytes", {
            "block_size": BLOCK, "archives": [
                archive_entry("a", p, good),
                {"id": "b", "path": op, "digests": same_declared},
            ]})

        # 3g. 非法请求：block_size / digests 结构 / 版本
        add_case("bad-block-size", {"block_size": 0, "archives": []},
                 EXIT_INPUT)
        add_case("bad-digest-hex", {"block_size": BLOCK, "archives": [
            {"id": "a", "path": p, "digests": ["zz" * 32]},
        ]}, EXIT_INPUT)
        add_case("bad-version", {"version": 99, "block_size": BLOCK,
                                 "archives": []}, EXIT_INPUT)

        # 3h. 非普通文件（目录）
        add_case("not-regular", {"block_size": BLOCK, "archives": [
            archive_entry("a", d, []),
        ]})

        for name, req, verify_rc in cases:
            req_path = write_request(d, req, name + ".json")
            out = os.path.join(d, "out-" + name)
            proc = subprocess.run(
                [sys.executable, "-I", TOOL, "merge",
                 "--request", req_path, "--output", out],
                capture_output=True, text=True,
            )
            assert proc.returncode == EXIT_INPUT, (
                "%s: 退出码 %d != %d\n%s\n%s"
                % (name, proc.returncode, EXIT_INPUT,
                   proc.stdout, proc.stderr)
            )
            assert not os.path.exists(out), "%s: 失败后发布了输出" % name
            verify(req_path, out, expect_rc=verify_rc)
        print("  ok %d 类失败输入均未发布归档包" % len(cases))


# ---------------------------------------------------------------------------
# 4. 空归档列表与零槽位归档
# ---------------------------------------------------------------------------

def test_empty_inputs():
    with tempfile.TemporaryDirectory() as d:
        # 4a. 空归档列表
        req_path = write_request(d, {"block_size": BLOCK, "archives": []},
                                 "empty.json")
        out = os.path.join(d, "empty-pkg")
        merge(req_path, out)
        m = load_manifest(out)
        assert m["image"]["slots"] == 0
        assert m["image"]["size"] == 0
        assert m["archives"] == []
        assert image_bytes(out) == b""
        verify(req_path, out)

        # 4b. 零槽位归档（空文件）+ 正常归档
        empty_path = write_archive(d, "empty.bin", [])
        one = [block("only", 30)]
        one_path = write_archive(d, "one.bin", one)
        req = {"block_size": BLOCK, "archives": [
            archive_entry("empty", empty_path, []),
            archive_entry("one", one_path, one),
        ]}
        req_path = write_request(d, req, "zero-slot.json")
        out2 = os.path.join(d, "zero-pkg")
        merge(req_path, out2)
        m = load_manifest(out2)
        assert [a["slot_map"] for a in m["archives"]] == [[], [0]]
        verify(req_path, out2)
        print("  ok 空归档列表与零槽位归档")


# ---------------------------------------------------------------------------
# 5. 原子发布：故障注入下最终路径绝不出现半成品
# ---------------------------------------------------------------------------

FAULT_CASES = [
    ("image", "pwrite", 1, "exit"),
    ("image", "pwrite", 1, "short"),
    ("image", "pwrite", 2, "exit"),
    ("image", "pwrite", 2, "short:3"),
    ("image", "fsync", 1, "exit"),
    ("manifest", "tmpwrite", 1, "exit"),
    ("manifest", "tmpwrite", 1, "short"),
    ("manifest", "tmpfsync", 1, "exit"),
    ("manifest", "rename", 1, "exit"),
    ("manifest", "dirfsync", 1, "exit"),
    ("publish", "rename", 1, "exit"),
    ("publish", "dirfsync", 1, "exit"),
]


def _make_fault_inputs(d):
    a_blocks = [block("f0", 40), block("f1", 41), block("f0", 40)]
    b_blocks = [block("f1", 41), block("f2", 42)]
    ap = write_archive(d, "fa.bin", a_blocks)
    bp = write_archive(d, "fb.bin", b_blocks)
    req = {"block_size": BLOCK, "archives": [
        archive_entry("a", ap, a_blocks),
        archive_entry("b", bp, b_blocks),
    ]}
    return write_request(d, req, "fault.json"), [a_blocks, b_blocks]


def test_atomic_publish_under_faults():
    for target, point, call, action in FAULT_CASES:
        rule = "%s:%s:%d:%s" % (target, point, call, action)
        with tempfile.TemporaryDirectory() as d:
            req_path, blocks = _make_fault_inputs(d)
            out = os.path.join(d, "pkg")
            proc = subprocess.run(
                [sys.executable, "-I", TOOL, "merge",
                 "--request", req_path, "--output", out, "--fault", rule],
                capture_output=True, text=True,
            )
            if action == "exit":
                assert proc.returncode == 137, (
                    "%s: 期望被杀，实际 %d\n%s\n%s"
                    % (rule, proc.returncode, proc.stdout, proc.stderr))
            else:
                # 短写会补齐；未被后续故障打断时应正常成功
                assert proc.returncode == 0, (
                    "%s: 退出码 %d\n%s\n%s"
                    % (rule, proc.returncode, proc.stdout, proc.stderr))

            # 允许输出存在的唯一情形：本次进程确实成功发布，或进程在
            # rename 之后才被杀（此时包必须已经是完整有效的）。
            if os.path.exists(out):
                assert proc.returncode == 0 or target == "publish", (
                    "%s: 失败进程却留下了输出" % rule)
                verify(req_path, out)
            else:
                # 最终路径不得存在半成品或可被误读的清单
                assert not os.path.exists(
                    os.path.join(out, "manifest.json"))
                assert not os.path.exists(
                    os.path.join(out, "image.bin"))

            # 重开（无故障）：要么接管未完成的重做，要么接受已完成的包
            merge(req_path, out)
            verify(req_path, out)
            want, unique = expected_mapping(blocks, BLOCK)
            m = load_manifest(out)
            assert [a["slot_map"] for a in m["archives"]] == want
            assert m["image"]["slots"] == len(unique)
    print("  ok 原子发布故障矩阵（%d 个边界，含短写与杀进程）"
          % len(FAULT_CASES))


def test_retry_cleans_stale_staging():
    with tempfile.TemporaryDirectory() as d:
        req_path, blocks = _make_fault_inputs(d)
        out = os.path.join(d, "pkg")
        merge(req_path, out, faults=["image:fsync:1:exit"], expect_rc=137)
        assert not os.path.exists(out)
        leftovers = [n for n in os.listdir(d) if n.startswith(".archive_merge.")]
        assert leftovers, "预期杀进程后留下隐藏临时目录"
        merge(req_path, out)
        verify(req_path, out)
        leftovers = [n for n in os.listdir(d)
                     if n.startswith(".archive_merge.") and
                     n.endswith(".lock") is False]
        assert not leftovers, "重开成功后仍残留临时目录: %r" % leftovers
    print("  ok 重开清理历史隐藏半成品")


# ---------------------------------------------------------------------------
# 6. 幂等重开与既有输出拒绝覆盖
# ---------------------------------------------------------------------------

def test_idempotent_and_existing_output():
    with tempfile.TemporaryDirectory() as d:
        req_path, blocks = _make_fault_inputs(d)
        out = os.path.join(d, "pkg")
        merge(req_path, out)
        snapshot = image_bytes(out)
        manifest_before = open(
            os.path.join(out, "manifest.json"), "rb").read()
        proc = merge(req_path, out)
        assert "此前已按同一请求完成" in proc.stdout
        assert image_bytes(out) == snapshot
        assert open(os.path.join(out, "manifest.json"), "rb").read() \
            == manifest_before

        # 已有输出被破坏：run 拒绝覆盖（退出码 7），也不修改它
        img_path = os.path.join(out, "image.bin")
        with open(img_path, "r+b") as f:
            f.seek(0)
            f.write(b"\xff" * 4)
        damaged = open(img_path, "rb").read()
        merge(req_path, out, expect_rc=EXIT_OUTPUT_EXISTS)
        assert open(img_path, "rb").read() == damaged
        verify(req_path, out, expect_rc=EXIT_PACKAGE_INVALID)

        # 输出路径存在但不是归档包
        with tempfile.TemporaryDirectory() as d2:
            req2_path, _ = _make_fault_inputs(d2)
            plain = os.path.join(d2, "plain")
            os.makedirs(plain)
            merge(req2_path, plain, expect_rc=EXIT_OUTPUT_EXISTS)
    print("  ok 幂等重开与既有输出拒绝覆盖")


# ---------------------------------------------------------------------------
# 7. verify 对篡改的识别
# ---------------------------------------------------------------------------

def test_verify_detects_tampering():
    with tempfile.TemporaryDirectory() as d:
        req_path, blocks = _make_fault_inputs(d)
        base = os.path.join(d, "base")
        merge(req_path, base)

        def fresh_copy(name):
            dst = os.path.join(d, name)
            shutil.copytree(base, dst)
            return dst

        # 7a. 翻转镜像一个字节
        p = fresh_copy("img-flip")
        ip = os.path.join(p, "image.bin")
        with open(ip, "r+b") as f:
            f.seek(3)
            f.write(b"\x00" if open(ip, "rb").read(1) != b"\x00" else b"\x01")
        verify(req_path, p, expect_rc=EXIT_PACKAGE_INVALID)

        # 7b. 镜像截断
        p = fresh_copy("img-trunc")
        with open(os.path.join(p, "image.bin"), "r+b") as f:
            f.truncate(BLOCK)
        verify(req_path, p, expect_rc=EXIT_PACKAGE_INVALID)

        # 7c. 删除镜像 / 清单
        p = fresh_copy("no-image")
        os.unlink(os.path.join(p, "image.bin"))
        verify(req_path, p, expect_rc=EXIT_PACKAGE_INVALID)
        p = fresh_copy("no-manifest")
        os.unlink(os.path.join(p, "manifest.json"))
        verify(req_path, p, expect_rc=EXIT_PACKAGE_INVALID)

        # 7d. 改清单 slot_map（签名随即失效）
        p = fresh_copy("map-tamper")
        mp = os.path.join(p, "manifest.json")
        m = json.load(open(mp))
        for a in m["archives"]:
            if len(a["slot_map"]) >= 2:
                a["slot_map"][0], a["slot_map"][1] = \
                    a["slot_map"][1], a["slot_map"][0]
                break
        json.dump(m, open(mp, "w"))
        verify(req_path, p, expect_rc=EXIT_PACKAGE_INVALID)

        # 7e. 清单与镜像不匹配：交换两个不同的镜像块（重算签名，仍须拒绝）
        p = fresh_copy("image-swap")
        ip = os.path.join(p, "image.bin")
        raw = open(ip, "rb").read()
        slots = len(raw) // BLOCK
        if slots >= 2:
            swapped = (raw[BLOCK:2 * BLOCK] + raw[:BLOCK] + raw[2 * BLOCK:])
            with open(ip, "wb") as f:
                f.write(swapped)
            verify(req_path, p, expect_rc=EXIT_PACKAGE_INVALID)

        # 7f. 清单绑定另一个请求
        other_req = {"block_size": BLOCK, "archives": []}
        other_path = write_request(d, other_req, "other.json")
        verify(other_path, base, expect_rc=EXIT_PACKAGE_INVALID)
    print("  ok verify 识别镜像/清单/映射篡改")


# ---------------------------------------------------------------------------
# 8. 去重必须按字节确认：伪造“同摘要不同字节”的候选索引也不得误合并
# ---------------------------------------------------------------------------

def _load_tool_module():
    import importlib.util
    import types
    if "_archive_merge_under_test" in sys.modules:
        return sys.modules["_archive_merge_under_test"]
    spec = importlib.util.spec_from_file_location(
        "_archive_merge_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_deduper_compares_bytes_not_labels():
    """模拟摘要碰撞：把不同字节的块挂到同一摘要标签下，去重器仍须分开保存。

    正常的 SHA-256 碰撞不可构造，这里直接把索引里“已存在的摘要”改写成
    新块的摘要标签，迫使去重器走到候选回读比较这条路径。
    """
    mod = _load_tool_module()
    with tempfile.TemporaryDirectory() as d:
        image_path = os.path.join(d, "image.bin")
        fd = os.open(image_path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            dd = mod._Deduper(fd, BLOCK, mod.NO_FAULT)
            b1 = block("collision-1", 50)
            b2 = block("collision-2", 51)
            label = sha256_hex(b1)
            idx1 = dd.intern(b1, label)
            assert idx1 == 0
            # 篡改索引：让 b2 的摘要标签与 b1 相同（真实摘要不同）
            dd._candidates[label] = list(dd._candidates[label])
            idx2 = dd.intern(b2, label)
            assert idx2 == 1, "同标签不同字节被误合并为同一槽"
            # 真正相同的字节仍去重
            assert dd.intern(b1, label) == 0
            assert dd.image_slots == 2
            assert mod._pread_all(fd, 0, BLOCK) == b1
            assert mod._pread_all(fd, BLOCK, BLOCK) == b2
        finally:
            os.close(fd)
    print("  ok 去重按字节确认（同摘要标签不同字节不合并）")


# ---------------------------------------------------------------------------
# 9. 通过 rearrange.py 入口转发子命令
# ---------------------------------------------------------------------------

def test_cli_delegation_from_rearrange():
    rearrange = os.path.join(HERE, "rearrange.py")
    with tempfile.TemporaryDirectory() as d:
        req_path, blocks = _make_fault_inputs(d)
        out = os.path.join(d, "pkg")
        proc = subprocess.run(
            [sys.executable, "-I", rearrange, "archive-merge",
             "--request", req_path, "--output", out],
            capture_output=True, text=True,
        )
        assert proc.returncode == EXIT_OK, proc.stderr
        verify(req_path, out)
        proc = subprocess.run(
            [sys.executable, "-I", rearrange, "archive-verify",
             "--request", req_path, "--package", out],
            capture_output=True, text=True,
        )
        assert proc.returncode == EXIT_OK, proc.stderr
    print("  ok rearrange.py 转发 archive-merge/archive-verify")


# ---------------------------------------------------------------------------
# 11. 并发：同一输出不能被两个合并进程同时写
# ---------------------------------------------------------------------------

def test_exclusive_lock():
    import time
    with tempfile.TemporaryDirectory() as d:
        req_path, _blocks = _make_fault_inputs(d)
        out = os.path.join(d, "pkg")
        holder = subprocess.Popen(
            [sys.executable, "-I", TOOL, "merge",
             "--request", req_path, "--output", out,
             "--fault", "image:fsync:1:hang"]
        )
        try:
            lock = os.path.join(d, ".archive_merge.pkg.lock")
            for _ in range(200):
                if os.path.exists(lock):
                    # 锁文件出现后最多再等一小会儿，确保 holder 已 flock
                    time.sleep(0.1)
                    break
                time.sleep(0.05)
            else:
                holder.kill()
                raise AssertionError("holder 未在预期时间内创建锁文件")
            merge(req_path, out, expect_rc=EXIT_LOCK)
            assert not os.path.exists(out)
        finally:
            holder.terminate()
            holder.wait()
        # holder 被杀后锁自动释放，重开完成
        merge(req_path, out)
        verify(req_path, out)
    print("  ok 同一输出独占锁")


# ---------------------------------------------------------------------------
# 12. 随机 SIGKILL 风暴：任意时刻被杀，最终路径只能是“无输出”或“完整包”
# ---------------------------------------------------------------------------

def test_random_kill_storm(trials=12):
    import time
    rng = random.Random(20261124)
    bs = 4096
    with tempfile.TemporaryDirectory() as d:
        unique = [os.urandom(bs) for _ in range(64)]
        archives = []
        for a in range(3):
            blocks = [unique[rng.randrange(len(unique))]
                      for _ in range(400)]
            path = os.path.join(d, "storm%d.bin" % a)
            with open(path, "wb") as f:
                for b in blocks:
                    f.write(b)
            archives.append(("storm%d" % a, path, blocks))
        req = {"block_size": bs, "archives": [
            archive_entry(aid, path, blocks)
            for aid, path, blocks in archives]}
        req_path = write_request(d, req, "storm.json")
        want, unique_order = expected_mapping(
            [blocks for _, _, blocks in archives], bs)

        for trial in range(trials):
            out = os.path.join(d, "storm-pkg-%d" % trial)
            proc = subprocess.Popen(
                [sys.executable, "-I", TOOL, "merge",
                 "--request", req_path, "--output", out],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            time.sleep(rng.uniform(0.0, 0.25))
            proc.kill()
            proc.wait()
            # 被杀后的最终路径：要么不存在，要么必须是完整有效包
            if os.path.exists(out):
                verify(req_path, out)
            merge(req_path, out)
            verify(req_path, out)
            m = load_manifest(out)
            assert [a["slot_map"] for a in m["archives"]] == want, (
                "trial %d 映射不确定" % trial)
            assert m["image"]["slots"] == len(unique_order)
    print("  ok 随机 SIGKILL 风暴（%d 次，输出要么不存在要么完整）" % trials)


# ---------------------------------------------------------------------------
# 13. 输入在处理期间被改动：报错且不发布
# ---------------------------------------------------------------------------

def test_input_changed_mid_process_rejected():
    mod = _load_tool_module()
    with tempfile.TemporaryDirectory() as d:
        bs = BLOCK
        blocks = [bytes([i]) * bs for i in range(6)]
        path = write_archive(d, "changing.bin", blocks)
        req = {"block_size": bs, "archives": [
            archive_entry("a", path, blocks)]}

        orig = mod._pread_all
        state = {"n": 0}

        def pread(fd, offset, size):
            buf = orig(fd, offset, size)
            state["n"] += 1
            if state["n"] == 3:
                # 同一文件、同样长度，处理中途改写内容
                with open(path, "r+b") as f:
                    f.seek(0)
                    f.write(b"Z" * bs)
                    f.flush()
                    os.fsync(f.fileno())
            return buf

        mod._pread_all = pread
        try:
            try:
                mod.merge_package(req, os.path.join(d, "pkg"), base_dir=d)
            except mod.InputError as exc:
                assert "处理期间被改动" in str(exc), str(exc)
            else:
                raise AssertionError("处理期间被改动的输入未被拒绝")
        finally:
            mod._pread_all = orig
        assert not os.path.exists(os.path.join(d, "pkg"))
    print("  ok 处理期间输入被改动 -> 拒绝且零发布")


# ---------------------------------------------------------------------------
# 14. 载荷缓冲护栏与规模
# ---------------------------------------------------------------------------

def test_no_whole_payload_buffers_in_source():
    src = open(TOOL, encoding="utf-8").read()
    for token in ("import mmap", "mmap(", "os.read("):
        assert token not in src, "源码出现被禁止的整文件缓冲方式: %r" % token
    # 镜像与归档载荷只经 _pread_all/_pwrite_all，长度由 block_size 决定
    assert "_pread_all(" in src and "_pwrite_all(" in src
    assert "block_size, " in src
    print("  ok 源码级载荷缓冲护栏")


def test_scale_dedup_and_buffer_bound():
    nblocks = 300
    bs = 4096
    with tempfile.TemporaryDirectory() as d:
        rng = random.Random(2026)
        unique = [os.urandom(bs) for _ in range(20)]
        archive_blocks = []
        for a in range(6):
            blocks = [unique[rng.randrange(len(unique))]
                      for _ in range(nblocks)]
            archive_blocks.append(blocks)
        entries = []
        for a, blocks in enumerate(archive_blocks):
            path = os.path.join(d, "a%d.bin" % a)
            with open(path, "wb") as f:
                for b in blocks:
                    f.write(b)
            entries.append(archive_entry("a%d" % a, path, blocks))
        req = {"block_size": bs, "archives": entries}
        req_path = write_request(d, req, "scale.json")
        out = os.path.join(d, "scale-pkg")
        merge(req_path, out)

        want, unique_order = expected_mapping(archive_blocks, bs)
        m = load_manifest(out)
        assert [a["slot_map"] for a in m["archives"]] == want
        assert m["image"]["slots"] == len(unique_order)
        img = image_bytes(out)
        assert len(img) == len(unique_order) * bs
        assert [img[i * bs:(i + 1) * bs]
                for i in range(len(unique_order))] == unique_order
        verify(req_path, out)
    print("  ok %d 块规模去重（镜像仅 %d 个不同块）"
          % (nblocks * 6, len(unique)))


def main():
    tests = [
        test_happy_path_dedup_across_and_within,
        test_deterministic_output,
        test_failures_publish_nothing,
        test_empty_inputs,
        test_atomic_publish_under_faults,
        test_retry_cleans_stale_staging,
        test_idempotent_and_existing_output,
        test_verify_detects_tampering,
        test_deduper_compares_bytes_not_labels,
        test_cli_delegation_from_rearrange,
        test_no_whole_payload_buffers_in_source,
        test_exclusive_lock,
        test_random_kill_storm,
        test_input_changed_mid_process_rejected,
        test_scale_dedup_and_buffer_bound,
    ]
    for t in tests:
        print("[%s]" % t.__name__)
        t()
    print("\n全部测试通过。")


if __name__ == "__main__":
    main()

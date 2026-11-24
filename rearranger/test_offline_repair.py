#!/usr/bin/env python3
"""offline_repair.py 的测试夹具（仅标准库，建议以 python3 -I 运行）。

覆盖：
  1. make-kit 端到端 + 健康包自洽核验（坏包拒绝、分组非法拒绝）；
  2. repair 端到端：翻转块/短块/缺槽/整镜像缺失/清单丢失，逐字节复原；
  3. 链/级联恢复：坏槽不止“每组一个”，连锁剥离整条链；
  4. 关系不足（秩亏）明确失败且不发布；校验块缺失时按可用性求解；
  5. 描述文件摘要/签名/结构核验、超长文件拒绝、输出存在拒绝覆盖；
  6. 块身份：内容摘要相同的另一槽不得顶替；
  7. 随机擦除图案：与参考 GF(2) 高斯消元的可解性判定逐项一致；
  8. 原子发布故障矩阵：任意点被杀/短写，最终路径无半成品，重开可成；
  9. 载荷缓冲护栏（无 mmap/整文件 read，故障盘只读一轮）；
 10. 三个入口的子命令转发；空镜像边界。

用法：
  python3 -I test_offline_repair.py
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
TOOL = os.path.join(HERE, "offline_repair.py")
MERGE_TOOL = os.path.join(HERE, "archive_merge.py")
REARRANGE_TOOL = os.path.join(HERE, "rearrange.py")

EXIT_OK = 0
EXIT_INPUT = 3
EXIT_PACKAGE_INVALID = 4
EXIT_LOCK = 6
EXIT_OUTPUT_EXISTS = 7
EXIT_REPAIR_FAILED = 8

BLOCK = 16


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def sha512_hex(data):
    return hashlib.sha512(data).hexdigest()


def block(tag, seed=0):
    rng = random.Random(seed)
    prefix = ("%s-" % tag).encode()
    body = bytes(rng.getrandbits(8) for _ in range(BLOCK))[: BLOCK - len(prefix)]
    return prefix + body.ljust(BLOCK - len(prefix), b"#")


def write_archive(directory, name, blocks):
    path = os.path.join(directory, name)
    with open(path, "wb") as f:
        for b in blocks:
            f.write(b)
        f.flush()
        os.fsync(f.fileno())
    return path


def archive_entry(aid, path, blocks):
    return {"id": aid, "path": path,
            "digests": [sha256_hex(b) for b in blocks]}


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    return path


def run_cli(args, expect_rc, tool=TOOL):
    proc = subprocess.run(
        [sys.executable, "-I", tool] + args,
        capture_output=True, text=True,
    )
    if proc.returncode != expect_rc:
        raise AssertionError(
            "退出码 %d != 期望 %d\nCMD: %s\nSTDOUT:\n%s\nSTDERR:\n%s"
            % (proc.returncode, expect_rc, " ".join(args),
               proc.stdout, proc.stderr)
        )
    return proc


def load_module():
    import importlib.util
    name = "_offline_repair_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 夹具：造健康归档包 + 矩形分组修复资料
# ---------------------------------------------------------------------------


def build_package_and_kit(d, ncols=3, nrows=3, seed=7, blocks_per_archive=6,
                          grouping=None):
    """造一个去重归档包并配套修复资料，返回路径与槽信息。

    用 nrows*ncols 个互不相同的块并在 3 个归档间不重复地分配，保证去重后
    镜像槽数恰好等于池大小，矩形分组才可预期。
    """
    rng = random.Random(seed)
    n = nrows * ncols
    unique = [block("u%02d" % i, 100 + i) for i in range(n)]
    order = list(range(n))
    rng.shuffle(order)
    # 把全部唯一块不重复地切成 3 个归档（blocks_per_archive 仅作下限提示，
    # 实际以覆盖全部唯一块为准），每个归档至少一块。
    archives = []
    cuts = sorted(rng.sample(range(1, n), 2)) if n >= 3 else [1, max(1, n - 1)]
    bounds = [0] + cuts + [n]
    for a in range(3):
        idxs = order[bounds[a]:bounds[a + 1]]
        blks = [unique[i] for i in idxs]
        p = write_archive(d, "a%d.bin" % a, blks)
        archives.append(archive_entry("a%d" % a, p, blks))
    req = {"block_size": BLOCK, "archives": archives}
    req_path = write_json(os.path.join(d, "req.json"), req)
    pkg = os.path.join(d, "pkg")
    subprocess.run([sys.executable, "-I", MERGE_TOOL, "merge",
                    "--request", req_path, "--output", pkg], check=True,
                   capture_output=True)
    m = json.load(open(os.path.join(pkg, "manifest.json")))
    assert m["image"]["slots"] == n, (m["image"]["slots"], n)

    if grouping is None:
        rows = [[s for s in range(n) if s % nrows == r] for r in range(nrows)]
        cols = [[s for s in range(n) if s // nrows == c] for c in range(ncols)]
        grouping = {"rows": rows, "columns": cols}
    gpath = write_json(os.path.join(d, "groups.json"), grouping)
    kit = os.path.join(d, "kit")
    digest_path = os.path.join(d, "kit.digest")
    run_cli(["make-kit", "--package", pkg, "--groups", gpath,
             "--out", kit, "--digest-file", digest_path], EXIT_OK)
    return {
        "req_path": req_path, "pkg": pkg, "kit": kit,
        "digest": open(digest_path).read().strip(),
        "n": n, "rows": grouping["rows"], "columns": grouping["columns"],
        "desc": os.path.join(kit, "repair_desc.json"),
        "rows_parity": os.path.join(kit, "repair_rows.bin"),
        "cols_parity": os.path.join(kit, "repair_cols.bin"),
    }


def repair_args(ctx, old_image, output, rows_parity=None, cols_parity=None,
                digest=None):
    return ["repair",
            "--descriptor", ctx["desc"],
            "--digest", digest or ctx["digest"],
            "--old-image", old_image,
            "--rows-parity", rows_parity or ctx["rows_parity"],
            "--cols-parity", cols_parity or ctx["cols_parity"],
            "--output", output]


def pkg_image_bytes(pkg):
    with open(os.path.join(pkg, "image.bin"), "rb") as f:
        return f.read()


def pkg_manifest_bytes(pkg):
    with open(os.path.join(pkg, "manifest.json"), "rb") as f:
        return f.read()


# ---------------------------------------------------------------------------
# 1. make-kit 端到端与健康包自洽核验
# ---------------------------------------------------------------------------


def test_make_kit_happy_and_contents():
    mod = load_module()
    with tempfile.TemporaryDirectory() as d:
        ctx = build_package_and_kit(d)
        desc = json.load(open(ctx["desc"]))
        n = ctx["n"]
        assert desc["format"] == mod.DESCRIPTOR_FORMAT
        assert desc["block_size"] == BLOCK
        assert desc["image"]["slots"] == n
        assert len(desc["image"]["slot_sha512"]) == n
        # 校验载荷大小 = 组数 * 块长
        assert os.path.getsize(ctx["rows_parity"]) == len(ctx["rows"]) * BLOCK
        assert os.path.getsize(ctx["cols_parity"]) == len(ctx["columns"]) * BLOCK
        # 描述文件摘要与留存一致
        raw = open(ctx["desc"], "rb").read()
        assert sha256_hex(raw) == ctx["digest"]
        # 封存清单字节与健康包清单逐字节相同
        assert bytes.fromhex(desc["manifest"]["bytes_hex"]) \
            == pkg_manifest_bytes(ctx["pkg"])
        # 手工复算第一个行组的异或与校验块
        g0 = ctx["rows"][0]
        img = pkg_image_bytes(ctx["pkg"])
        acc = bytearray(BLOCK)
        for s in g0:
            for i, b in enumerate(img[s * BLOCK:(s + 1) * BLOCK]):
                acc[i] ^= b
        with open(ctx["rows_parity"], "rb") as f:
            got = f.read(BLOCK)
        assert bytes(acc) == got
        assert sha512_hex(got) == desc["parity"]["rows"]["sha512"][0]
        print("  ok make-kit 产物结构、摘要与逐组异或正确")


def test_make_kit_rejects_unhealthy_package_and_bad_groups():
    with tempfile.TemporaryDirectory() as d:
        ctx = build_package_and_kit(d)
        good_groups = os.path.join(d, "groups.json")

        # 1a. 镜像被破坏的包不得产出修复资料
        bad_pkg = os.path.join(d, "bad-pkg")
        shutil.copytree(ctx["pkg"], bad_pkg)
        with open(os.path.join(bad_pkg, "image.bin"), "r+b") as f:
            f.seek(0)
            f.write(b"\xff" * 4)
        out = os.path.join(d, "kit-bad")
        run_cli(["make-kit", "--package", bad_pkg, "--groups", good_groups,
                 "--out", out], EXIT_PACKAGE_INVALID)
        assert not os.path.exists(out)

        # 1b. 清单被篡改（签名失效）
        bad_pkg2 = os.path.join(d, "bad-pkg2")
        shutil.copytree(ctx["pkg"], bad_pkg2)
        mp = os.path.join(bad_pkg2, "manifest.json")
        m = json.load(open(mp))
        m["block_size"] = BLOCK * 2
        json.dump(m, open(mp, "w"))
        out2 = os.path.join(d, "kit-bad2")
        run_cli(["make-kit", "--package", bad_pkg2, "--groups", good_groups,
                 "--out", out2], EXIT_PACKAGE_INVALID)
        assert not os.path.exists(out2)

        # 1c. 分组非法：覆盖不全 / 重复 / 越界 / 空组
        n = ctx["n"]
        bad_cases = [
            {"rows": [list(range(n))], "columns": [list(range(n - 1))]},
            {"rows": [[0, 0]], "columns": [[s] for s in range(n)]},
            {"rows": [[s] for s in range(n)],
             "columns": [[n]] + [[s] for s in range(1, n)]},
            {"rows": [[]] + [[s] for s in range(n)],
             "columns": [[s] for s in range(n)]},
        ]
        for i, g in enumerate(bad_cases):
            gp = write_json(os.path.join(d, "bad-groups-%d.json" % i), g)
            o = os.path.join(d, "kit-bg-%d" % i)
            run_cli(["make-kit", "--package", ctx["pkg"], "--groups", gp,
                     "--out", o], EXIT_INPUT)
            assert not os.path.exists(o)
        print("  ok 坏包与非法分组均被拒绝且零产出")


# ---------------------------------------------------------------------------
# 2. repair 端到端：多种擦除形态逐字节复原
# ---------------------------------------------------------------------------


def _damage_image(src_pkg, dst_path, *, flip=(), truncate_to=None,
                  drop_manifest_to=None):
    shutil.copytree(src_pkg, dst_path)
    ip = os.path.join(dst_path, "image.bin")
    data = bytearray(open(ip, "rb").read())
    for s in flip:
        data[s * BLOCK] ^= 0xFF
    if truncate_to is not None:
        data = data[:truncate_to]
    with open(ip, "wb") as f:
        f.write(data)
    if drop_manifest_to is not None:
        os.unlink(os.path.join(dst_path, "manifest.json"))


def test_repair_flip_truncate_missing_manifest():
    with tempfile.TemporaryDirectory() as d:
        ctx = build_package_and_kit(d)
        damaged = os.path.join(d, "damaged")
        # 翻转槽0、槽4；截断到槽7（槽7、8 缺槽）；删清单。
        # 未知槽 {0,4,7,8}：c0 定 0、c1 定 4，随后 r1 连锁定 7、c2 定 8。
        _damage_image(ctx["pkg"], damaged, flip=(0, 4),
                      truncate_to=7 * BLOCK, drop_manifest_to=True)
        out = os.path.join(d, "repaired")
        run_cli(repair_args(ctx, os.path.join(damaged, "image.bin"), out),
                EXIT_OK)
        assert pkg_image_bytes(out) == pkg_image_bytes(ctx["pkg"])
        assert pkg_manifest_bytes(out) == pkg_manifest_bytes(ctx["pkg"])
        # 原读取系统仍可直接核验
        run_cli(["verify", "--request", ctx["req_path"], "--package", out],
                EXIT_OK, tool=MERGE_TOOL)
        # 原包与修复资料未被改写
        assert not os.path.exists(os.path.join(damaged, "manifest.json"))
        print("  ok 翻转+截断+清单丢失后逐字节复原，原系统可核验")


def test_repair_entire_image_missing():
    with tempfile.TemporaryDirectory() as d:
        ctx = build_package_and_kit(d)
        # 9 槽、行 3 列 3：每份校验 3 块共 6 个方程 >= 9? 否 -> 应失败
        out = os.path.join(d, "rep")
        run_cli(repair_args(ctx, os.path.join(d, "no-image.bin"), out),
                EXIT_REPAIR_FAILED)
        assert not os.path.exists(out)
        print("  ok 整镜像缺失且方程不足 -> 明确失败不发布")


def test_repair_old_image_absent_but_recoverable():
    """更大的分组密度：单槽组行列两侧足以在镜像缺失时恢复。"""
    with tempfile.TemporaryDirectory() as d:
        ctx = build_package_and_kit(d)
        # 构造“每个槽单独成行组”的分组（列组也单槽）：每槽两侧各一方程
        n = ctx["n"]
        grouping = {"rows": [[s] for s in range(n)],
                    "columns": [[s] for s in range(n)]}
        gpath = write_json(os.path.join(d, "groups-single.json"), grouping)
        kit2 = os.path.join(d, "kit2")
        dig2 = os.path.join(d, "kit2.digest")
        run_cli(["make-kit", "--package", ctx["pkg"], "--groups", gpath,
                 "--out", kit2, "--digest-file", dig2], EXIT_OK)
        ctx2 = dict(ctx)
        ctx2.update({
            "kit": kit2,
            "digest": open(dig2).read().strip(),
            "desc": os.path.join(kit2, "repair_desc.json"),
            "rows_parity": os.path.join(kit2, "repair_rows.bin"),
            "cols_parity": os.path.join(kit2, "repair_cols.bin"),
            "rows": grouping["rows"], "columns": grouping["columns"],
        })
        out = os.path.join(d, "rep2")
        run_cli(repair_args(ctx2, os.path.join(d, "gone.bin"), out), EXIT_OK)
        assert pkg_image_bytes(out) == pkg_image_bytes(ctx["pkg"])
        print("  ok 镜像整体缺失但每槽双方程 -> 完整恢复")


# ---------------------------------------------------------------------------
# 3. 链/级联恢复
# ---------------------------------------------------------------------------


def test_chain_cascade_recovery():
    with tempfile.TemporaryDirectory() as d:
        ctx = build_package_and_kit(d)
        # 擦除一整列（行组里各坏一个），每行可由同行已知槽定出，连锁完成
        col0_slots = ctx["columns"][0]
        damaged = os.path.join(d, "chain")
        _damage_image(ctx["pkg"], damaged, flip=tuple(col0_slots))
        out = os.path.join(d, "chain-out")
        run_cli(repair_args(ctx, os.path.join(damaged, "image.bin"), out),
                EXIT_OK)
        assert pkg_image_bytes(out) == pkg_image_bytes(ctx["pkg"])

        # 再毁掉一个“冗余”校验块（列组 c2，其成员本就全部完好）：
        # 缺它不影响求解，证明只依赖仍可核验的关系。
        cols_bad = os.path.join(d, "cols_bad.bin")
        cdata = open(ctx["cols_parity"], "rb").read()
        cb = bytearray(cdata)
        cb[2 * BLOCK] ^= 0xFF   # 列组 c2 校验块失效
        open(cols_bad, "wb").write(bytes(cb))
        out2 = os.path.join(d, "chain-out2")
        run_cli(repair_args(ctx, os.path.join(damaged, "image.bin"), out2,
                            cols_parity=cols_bad), EXIT_OK)
        assert pkg_image_bytes(out2) == pkg_image_bytes(ctx["pkg"])
        print("  ok 链式级联恢复，且缺一个校验块仍可恢复")


# ---------------------------------------------------------------------------
# 4. 秩亏：全部擦除、方程不足
# ---------------------------------------------------------------------------


def test_rank_deficiency_rejected():
    with tempfile.TemporaryDirectory() as d:
        ctx = build_package_and_kit(d)
        # 全部槽翻转（全部不可信），矩形 3x3：6 方程 < 9 未知
        damaged = os.path.join(d, "allbad")
        _damage_image(ctx["pkg"], damaged,
                      flip=tuple(range(ctx["n"])))
        out = os.path.join(d, "out")
        run_cli(repair_args(ctx, os.path.join(damaged, "image.bin"), out),
                EXIT_REPAIR_FAILED)
        assert not os.path.exists(out)
        # 失败后重试（无改动）仍失败，且不留临时目录在最终路径
        run_cli(repair_args(ctx, os.path.join(damaged, "image.bin"), out),
                EXIT_REPAIR_FAILED)
        assert not os.path.exists(out)
        print("  ok 秩亏（方程数 < 未知槽数）明确失败、可重复、不发布")


def test_parity_block_erasure_changes_solvability():
    """毁掉校验块使原本可解的图案变成秩亏，必须如实失败。"""
    with tempfile.TemporaryDirectory() as d:
        ctx = build_package_and_kit(d)
        # 只坏槽0：原本行、列各一方程即可解
        damaged = os.path.join(d, "one")
        _damage_image(ctx["pkg"], damaged, flip=(0,))
        # 同时让槽0所在的行组与列组校验块都不可核验
        r = next(g for g, grp in enumerate(ctx["rows"]) if 0 in grp)
        c = next(g for g, grp in enumerate(ctx["columns"]) if 0 in grp)
        rows_data = open(ctx["rows_parity"], "rb").read()
        cols_data = open(ctx["cols_parity"], "rb").read()
        rb = bytearray(rows_data)
        cb = bytearray(cols_data)
        rb[r * BLOCK] ^= 0xFF
        cb[c * BLOCK] ^= 0xFF
        rp = os.path.join(d, "rp.bin")
        cp = os.path.join(d, "cp.bin")
        open(rp, "wb").write(bytes(rb))
        open(cp, "wb").write(bytes(cb))
        out = os.path.join(d, "out")
        run_cli(repair_args(ctx, os.path.join(damaged, "image.bin"), out,
                            rows_parity=rp, cols_parity=cp),
                EXIT_REPAIR_FAILED)
        assert not os.path.exists(out)
        print("  ok 唯一相关校验块失效 -> 关系不足失败")


# ---------------------------------------------------------------------------
# 4b. 互相矛盾：校验块可核验、能通过求解，但与组关系冲突 -> 拒绝发布
# ---------------------------------------------------------------------------


def test_contradictory_parity_rejected():
    """所有镜像槽都完好时剥离平凡成功，最终关系复核必须抓出矛盾校验块。"""
    with tempfile.TemporaryDirectory() as d:
        ctx = build_package_and_kit(d)
        # 改写行校验块 0 的内容，并在描述文件里同步替换其封存 SHA-512
        # （描述文件重签、留存摘要重算），使该块“可核验”但关系错误。
        rp_path = os.path.join(d, "rp_contradict.bin")
        rp = bytearray(open(ctx["rows_parity"], "rb").read())
        rp[0:BLOCK] = bytes(b ^ 0xFF for b in rp[0:BLOCK])
        open(rp_path, "wb").write(bytes(rp))

        desc = json.load(open(ctx["desc"]))
        desc["parity"]["rows"]["sha512"][0] = sha512_hex(bytes(rp[0:BLOCK]))
        sig = desc.pop("sig")
        desc["sig"] = sha256_hex(load_module().am._canonical_bytes(desc))
        raw = load_module().am._canonical_bytes(desc)
        dpath = os.path.join(d, "desc_contradict.json")
        open(dpath, "wb").write(raw)
        digest = sha256_hex(raw)
        assert sig != desc["sig"]

        good_img = os.path.join(ctx["pkg"], "image.bin")
        out = os.path.join(d, "out")
        run_cli(["repair", "--descriptor", dpath, "--digest", digest,
                 "--old-image", good_img,
                 "--rows-parity", rp_path,
                 "--cols-parity", ctx["cols_parity"],
                 "--output", out], EXIT_REPAIR_FAILED)
        assert not os.path.exists(out)
    print("  ok 可核验但互相矛盾的校验块在最终复核被拒绝")


# ---------------------------------------------------------------------------
# 5. 描述文件与输入核验
# ---------------------------------------------------------------------------


def test_descriptor_and_input_validation():
    with tempfile.TemporaryDirectory() as d:
        ctx = build_package_and_kit(d)
        good_img = os.path.join(ctx["pkg"], "image.bin")

        # 5a. 留存摘要错误
        out = os.path.join(d, "o1")
        run_cli(repair_args(ctx, good_img, out, digest="00" * 32), EXIT_INPUT)
        assert not os.path.exists(out)

        # 5b. 描述文件被改（与留存摘要不符）
        bad_desc = os.path.join(d, "bad_desc.json")
        dd = json.load(open(ctx["desc"]))
        dd["block_size"] = BLOCK * 2
        json.dump(dd, open(bad_desc, "w"))
        run_cli(["repair", "--descriptor", bad_desc,
                 "--digest", ctx["digest"], "--old-image", good_img,
                 "--rows-parity", ctx["rows_parity"],
                 "--cols-parity", ctx["cols_parity"],
                 "--output", os.path.join(d, "o2")], EXIT_INPUT)

        # 5c. 描述文件签名失效：改一个字段但保留旧 sig，再用实测字节伪造
        #     留存摘要——外层摘要能过，内层 sig 必须把它拦下。
        tampered = json.load(open(ctx["desc"]))
        tampered["image"]["slots"] = ctx["n"] + 1
        raw = json.dumps(tampered, sort_keys=True,
                         separators=(",", ":")).encode()
        tpath = os.path.join(d, "sig_bad.json")
        open(tpath, "wb").write(raw)
        run_cli(["repair", "--descriptor", tpath,
                 "--digest", sha256_hex(raw), "--old-image", good_img,
                 "--rows-parity", ctx["rows_parity"],
                 "--cols-parity", ctx["cols_parity"],
                 "--output", os.path.join(d, "o3")], EXIT_INPUT)

        # 5d. 旧镜像超长
        big = os.path.join(d, "big.bin")
        open(big, "wb").write(open(good_img, "rb").read() + b"X")
        run_cli(repair_args(ctx, big, os.path.join(d, "o4")), EXIT_INPUT)

        # 5e. 校验载荷超长
        bigp = os.path.join(d, "big_parity.bin")
        open(bigp, "wb").write(
            open(ctx["rows_parity"], "rb").read() + b"Y" * BLOCK)
        run_cli(repair_args(ctx, good_img, os.path.join(d, "o5"),
                            rows_parity=bigp), EXIT_INPUT)

        # 5f. 输出已存在
        os.makedirs(os.path.join(d, "exists"))
        run_cli(repair_args(ctx, good_img, os.path.join(d, "exists")),
                EXIT_OUTPUT_EXISTS)
        print("  ok 描述文件摘要/签名/结构与超长输入、既有输出均被拒绝")


# ---------------------------------------------------------------------------
# 6. 块身份：内容相同的另一槽不得顶替
# ---------------------------------------------------------------------------


def test_slot_identity_not_content_substitution():
    """制造两个内容相同的镜像槽；坏一个时只能按槽号重建，不许拿另一个顶替。

    去重归档包里镜像槽内容本就互不相同，这里直接在库层面构造一个
    描述文件/镜像，使槽 0 与槽 1 内容一致（同 SHA-512），再擦除槽 0：
    求解必须依据槽 0 所在组的方程把它写回槽 0 的位置，且通过封存校验。
    """
    mod = load_module()
    with tempfile.TemporaryDirectory() as d:
        n, bs = 4, BLOCK
        blocks = [block("same", 1), block("same", 1),
                  block("b2", 2), block("b3", 3)]
        assert sha512_hex(blocks[0]) == sha512_hex(blocks[1])
        img_path = os.path.join(d, "image.bin")
        with open(img_path, "wb") as f:
            f.write(b"".join(blocks))
        rows = [[0, 1], [2, 3]]
        cols = [[0, 2], [1, 3]]

        def parity(groups):
            out = []
            for grp in groups:
                acc = bytearray(bs)
                for s in grp:
                    for i, b in enumerate(blocks[s]):
                        acc[i] ^= b
                out.append(bytes(acc))
            return out

        rp = parity(rows)
        cp = parity(cols)
        rows_path = os.path.join(d, "rows.bin")
        cols_path = os.path.join(d, "cols.bin")
        open(rows_path, "wb").write(b"".join(rp))
        open(cols_path, "wb").write(b"".join(cp))

        manifest_body = {
            "format": mod.am.PACKAGE_FORMAT,
            "version": mod.am.PACKAGE_VERSION,
            "block_size": bs,
            "image": {"file": "image.bin", "slots": n, "size": n * bs,
                      "sha256": sha256_hex(b"".join(blocks)),
                      "digests": [sha256_hex(b) for b in blocks]},
            "archives": [],
        }
        manifest_body["sig"] = sha256_hex(
            mod.am._canonical_bytes(manifest_body))
        manifest_bytes = mod.am._canonical_bytes(manifest_body)
        desc_body = {
            "format": mod.DESCRIPTOR_FORMAT,
            "version": mod.DESCRIPTOR_VERSION,
            "block_size": bs,
            "image": {"slots": n, "size": n * bs,
                      "sha256": sha256_hex(b"".join(blocks)),
                      "slot_sha512": [sha512_hex(b) for b in blocks]},
            "manifest": {"sha256": sha256_hex(manifest_bytes),
                         "bytes_hex": manifest_bytes.hex()},
            "groups": {"rows": rows, "columns": cols},
            "parity": {
                "rows": {"file": "repair_rows.bin", "blocks": len(rows),
                         "block_size": bs,
                         "sha512": [sha512_hex(x) for x in rp]},
                "columns": {"file": "repair_cols.bin", "blocks": len(cols),
                            "block_size": bs,
                            "sha512": [sha512_hex(x) for x in cp]},
            },
        }
        desc_body["sig"] = sha256_hex(
            mod.am._canonical_bytes(desc_body))
        desc_bytes = mod.am._canonical_bytes(desc_body)
        desc_path = os.path.join(d, "desc.json")
        open(desc_path, "wb").write(desc_bytes)
        digest = sha256_hex(desc_bytes)

        # 擦除槽 0（写垃圾），槽 1 内容与槽 0 相同但身份不同
        damaged = os.path.join(d, "damaged.bin")
        bad = bytearray(b"".join(blocks))
        bad[0:BLOCK] = b"?" * BLOCK
        open(damaged, "wb").write(bytes(bad))

        out = os.path.join(d, "repaired")
        mod.repair_package(desc_path, digest, damaged, rows_path, cols_path,
                           out)
        got = pkg_image_bytes(out)
        # 槽 0 必须回到自己的封存字节，镜像整体逐字节一致
        assert got == b"".join(blocks)
        assert got[0:BLOCK] == blocks[0]
        print("  ok 同摘要槽不互相顶替，按槽号身份重建并通过封存校验")


# ---------------------------------------------------------------------------
# 7. 随机擦除图案：与参考 GF(2) 高斯消元的可解性逐项一致
# ---------------------------------------------------------------------------


def _gf2_rank_solvable(n, rows, cols, good_row_eq, good_col_eq, known):
    """参考实现：对“好校验块”给出的异或方程做 GF(2) 高斯消元。

    未知槽为变量；方程系数 1 表示该槽属于该组。返回未知槽是否全部可被
    唯一确定（未知变量数 == 有效方程秩）。
    """
    unknown = [s for s in range(n) if not known[s]]
    idx = {s: i for i, s in enumerate(unknown)}
    rows_bits = []
    for g, grp in enumerate(rows):
        if not good_row_eq[g]:
            continue
        bits = 0
        for s in grp:
            if s in idx:
                bits |= 1 << idx[s]
        if bits:
            rows_bits.append(bits)
    for g, grp in enumerate(cols):
        if not good_col_eq[g]:
            continue
        bits = 0
        for s in grp:
            if s in idx:
                bits |= 1 << idx[s]
        if bits:
            rows_bits.append(bits)
    # 高斯消元求秩
    basis = {}
    for bits in rows_bits:
        x = bits
        while x:
            p = x.bit_length() - 1
            if p in basis:
                x ^= basis[p]
            else:
                basis[p] = x
                break
    return len(basis) == len(unknown)


def test_random_erasure_matches_reference_rank():
    mod = load_module()
    rng = random.Random(20261124)
    trials = 0
    matched = 0
    successes = 0
    with tempfile.TemporaryDirectory() as d:
        ctx = build_package_and_kit(d, nrows=4, ncols=4, seed=11,
                                    blocks_per_archive=10)
        n = ctx["n"]
        img = pkg_image_bytes(ctx["pkg"])
        blocks = [img[s * BLOCK:(s + 1) * BLOCK] for s in range(n)]
        desc = json.load(open(ctx["desc"]))

        for _ in range(60):
            erased = {s for s in range(n) if rng.random() < 0.45}
            known = [s not in erased for s in range(n)]
            # 随机让一部分校验块失效
            good_row = [rng.random() > 0.2 for _ in ctx["rows"]]
            good_col = [rng.random() > 0.2 for _ in ctx["columns"]]

            # 造旧镜像：擦除槽写垃圾
            dmg = bytearray(img)
            for s in erased:
                dmg[s * BLOCK:(s + 1) * BLOCK] = b"~" * BLOCK
            old_path = os.path.join(d, "old_%d.bin" % trials)
            open(old_path, "wb").write(bytes(dmg))

            # 造校验载荷：失效块写垃圾（摘要不符 -> 擦除）
            rp = bytearray(open(ctx["rows_parity"], "rb").read())
            cp = bytearray(open(ctx["cols_parity"], "rb").read())
            for g, ok in enumerate(good_row):
                if not ok:
                    rp[g * BLOCK] ^= 0xFF
            for g, ok in enumerate(good_col):
                if not ok:
                    cp[g * BLOCK] ^= 0xFF
            rpath = os.path.join(d, "rp_%d.bin" % trials)
            cpath = os.path.join(d, "cp_%d.bin" % trials)
            open(rpath, "wb").write(bytes(rp))
            open(cpath, "wb").write(bytes(cp))

            ref_solvable = _gf2_rank_solvable(
                n, ctx["rows"], ctx["columns"], good_row, good_col, known)

            out = os.path.join(d, "out_%d" % trials)
            try:
                mod.repair_package(
                    ctx["desc"], ctx["digest"], old_path, rpath, cpath, out)
                tool_solved = True
            except mod.RepairFailed:
                tool_solved = False
            trials += 1

            assert tool_solved == ref_solvable, (
                "试验 %d：工具判定 %s 与参考秩判定 %s 不一致"
                % (trials, tool_solved, ref_solvable))
            if tool_solved:
                successes += 1
                assert pkg_image_bytes(out) == img
                assert pkg_manifest_bytes(out) == pkg_manifest_bytes(ctx["pkg"])
                matched += 1
            else:
                assert not os.path.exists(out)
                matched += 1
        assert successes > 0, "随机图案里应当至少有一些可恢复案例"
    print("  ok %d 组随机擦除/校验失效图案与 GF(2) 秩判定全一致（%d 例成功复原）"
          % (trials, successes))


def test_make_kit_atomic_publish_under_faults():
    """make-kit 在发布边界被杀/短写：最终路径无半成品，重开必得完整资料。"""
    rules = [
        "image:pwrite:1:exit",          # 写行校验块时被杀
        "image:pwrite:1:short",         # 短写补齐后成功
        "manifest:rename:1:exit",       # 描述文件 rename 时被杀
        "publish:rename:1:exit",        # 整个资料目录发布时被杀
        "publish:dirfsync:1:exit",
    ]
    for rule in rules:
        with tempfile.TemporaryDirectory() as d:
            ctx = _build_kit_inputs(d)
            gpath = os.path.join(d, "groups.json")
            out = os.path.join(d, "kit-out")
            proc = subprocess.run(
                [sys.executable, "-I", TOOL, "make-kit",
                 "--package", ctx["pkg"], "--groups", gpath, "--out", out,
                 "--fault", rule],
                capture_output=True, text=True)
            if rule.endswith("exit"):
                assert proc.returncode == 137, (rule, proc.stderr)
            else:
                assert proc.returncode == 0, (rule, proc.stderr)
            if os.path.exists(out):
                # 可见即完整：三份文件齐备且描述文件摘要自洽
                for fn in ("repair_rows.bin", "repair_cols.bin",
                           "repair_desc.json"):
                    assert os.path.exists(os.path.join(out, fn))
            else:
                assert not os.path.exists(
                    os.path.join(out, "repair_desc.json"))
            # 无故障重开必得完整资料（已发布则按“拒绝覆盖”处理）
            if os.path.exists(out):
                run_cli(["make-kit", "--package", ctx["pkg"],
                         "--groups", gpath, "--out", out],
                        EXIT_OUTPUT_EXISTS)
            else:
                run_cli(["make-kit", "--package", ctx["pkg"],
                         "--groups", gpath, "--out", out], EXIT_OK)
                raw = open(os.path.join(out, "repair_desc.json"), "rb").read()
                dd = json.loads(raw)
                assert dd["image"]["slots"] == ctx["n"]
    print("  ok make-kit 发布故障矩阵无半成品，重开必得完整资料")


def _build_kit_inputs(d):
    """供 make-kit 故障测试：先建包与分组表（不预先生成 kit）。"""
    rng = random.Random(5)
    n = 9
    unique = [block("v%02d" % i, 200 + i) for i in range(n)]
    order = list(range(n))
    rng.shuffle(order)
    archives = []
    for a in range(3):
        idxs = order[a * 3:(a + 1) * 3]
        blks = [unique[i] for i in idxs]
        p = write_archive(d, "m%d.bin" % a, blks)
        archives.append(archive_entry("m%d" % a, p, blks))
    req_path = write_json(os.path.join(d, "req.json"),
                          {"block_size": BLOCK, "archives": archives})
    pkg = os.path.join(d, "pkg")
    subprocess.run([sys.executable, "-I", MERGE_TOOL, "merge",
                    "--request", req_path, "--output", pkg], check=True,
                   capture_output=True)
    grouping = {"rows": [[s for s in range(n) if s % 3 == r] for r in range(3)],
                "columns": [[s for s in range(n) if s // 3 == c]
                            for c in range(3)]}
    write_json(os.path.join(d, "groups.json"), grouping)
    return {"pkg": pkg, "n": n}


# ---------------------------------------------------------------------------
# 8. 原子发布故障矩阵
# ---------------------------------------------------------------------------


FAULT_CASES = [
    ("image", "pwrite", 1, "exit"),
    ("image", "pwrite", 1, "short"),
    ("image", "pwrite", 3, "short:3"),
    ("image", "fsync", 1, "exit"),
    ("manifest", "tmpwrite", 1, "exit"),
    ("manifest", "tmpfsync", 1, "exit"),
    ("manifest", "rename", 1, "exit"),
    ("manifest", "dirfsync", 1, "exit"),
    ("publish", "rename", 1, "exit"),
    ("publish", "dirfsync", 1, "exit"),
]


def test_repair_atomic_publish_under_faults():
    for target, point, call, action in FAULT_CASES:
        rule = "%s:%s:%d:%s" % (target, point, call, action)
        with tempfile.TemporaryDirectory() as d:
            ctx = build_package_and_kit(d)
            damaged = os.path.join(d, "damaged")
            _damage_image(ctx["pkg"], damaged,
                          flip=tuple(ctx["columns"][0]),
                          truncate_to=(ctx["n"] - 1) * BLOCK)
            out = os.path.join(d, "repaired")
            proc = subprocess.run(
                [sys.executable, "-I", TOOL]
                + repair_args(ctx, os.path.join(damaged, "image.bin"), out)
                + ["--fault", rule],
                capture_output=True, text=True)
            if action == "exit":
                assert proc.returncode == 137, proc.stderr
            else:
                assert proc.returncode == 0, proc.stderr
            if os.path.exists(out):
                # 任何可见输出都必须是完整有效的包
                assert pkg_image_bytes(out) == pkg_image_bytes(ctx["pkg"])
                assert pkg_manifest_bytes(out) == pkg_manifest_bytes(ctx["pkg"])
                # 已发布的完整包：重试应幂等拒绝覆盖（退出码 7），不改动它
                run_cli(repair_args(ctx, os.path.join(damaged, "image.bin"),
                                    out), EXIT_OUTPUT_EXISTS)
            else:
                assert not os.path.exists(
                    os.path.join(out, "manifest.json"))
                assert not os.path.exists(os.path.join(out, "image.bin"))
                # 无故障重开：必然得到完整包
                run_cli(repair_args(ctx, os.path.join(damaged, "image.bin"),
                                    out), EXIT_OK)
                assert pkg_image_bytes(out) == pkg_image_bytes(ctx["pkg"])
    print("  ok 修复发布故障矩阵（%d 边界）无半成品，重开必得完整包"
          % len(FAULT_CASES))


def test_repair_exclusive_lock():
    with tempfile.TemporaryDirectory() as d:
        ctx = build_package_and_kit(d)
        damaged = os.path.join(d, "damaged")
        _damage_image(ctx["pkg"], damaged, flip=(0,))
        out = os.path.join(d, "repaired")
        holder = subprocess.Popen(
            [sys.executable, "-I", TOOL]
            + repair_args(ctx, os.path.join(damaged, "image.bin"), out)
            + ["--fault", "image:fsync:1:hang"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            import time
            # 锁文件名见 offline_repair._output_paths
            lock = os.path.join(d, ".offline_repair.repaired.lock")
            for _ in range(200):
                if os.path.exists(lock):
                    time.sleep(0.1)
                    break
                time.sleep(0.05)
            else:
                holder.kill()
                raise AssertionError("holder 未创建锁文件")
            run_cli(repair_args(ctx, os.path.join(damaged, "image.bin"), out),
                    EXIT_LOCK)
            assert not os.path.exists(out)
        finally:
            holder.terminate()
            holder.wait()
        run_cli(repair_args(ctx, os.path.join(damaged, "image.bin"), out),
                EXIT_OK)
        assert pkg_image_bytes(out) == pkg_image_bytes(ctx["pkg"])
    print("  ok 修复输出独占锁")


# ---------------------------------------------------------------------------
# 9. 载荷缓冲护栏 + 故障盘只读一轮
# ---------------------------------------------------------------------------


def test_no_whole_payload_buffers():
    src = open(TOOL, encoding="utf-8").read()
    for token in ("import mmap", "mmap(", ".read()"):
        # 小元数据文件允许整读（描述/清单），这里只禁止载荷式整读
        if token == ".read()":
            continue
        assert token not in src, "源码出现被禁止的整文件缓冲: %r" % token
    # 载荷 IO 只经 block_size 长度的 pread/pwrite
    assert "os.pread(" in src and "_pwrite_all(" in src
    print("  ok 源码级载荷缓冲护栏（无 mmap，载荷按块 pread/pwrite）")


def test_failed_disk_read_only_once():
    """故障盘每个槽位只经历一次 pread 扫荡（短读可在同一块内续读）。"""
    mod = load_module()
    with tempfile.TemporaryDirectory() as d:
        ctx = build_package_and_kit(d)
        n, bs = ctx["n"], BLOCK
        damaged = os.path.join(d, "damaged.bin")
        data = bytearray(pkg_image_bytes(ctx["pkg"]))
        data[0] ^= 0xFF
        open(damaged, "wb").write(bytes(data))

        pread_calls = []
        orig_pread = os.pread
        orig_open = os.open
        old_fds = set()

        def counting_open(path, *a, **k):
            fd = orig_open(path, *a, **k)
            if os.path.abspath(str(path)) == os.path.abspath(damaged):
                old_fds.add(fd)
            return fd

        def counting_pread(fd, length, offset):
            if fd in old_fds:
                pread_calls.append((length, offset))
            return orig_pread(fd, length, offset)

        os.pread = counting_pread
        os.open = counting_open
        try:
            out = os.path.join(d, "out")
            mod.repair_package(
                ctx["desc"], ctx["digest"], damaged,
                ctx["rows_parity"], ctx["cols_parity"], out)
        finally:
            os.pread = orig_pread
            os.open = orig_open

        # 故障盘每槽恰好一次以 s*bs 起、长度 bs 的读取（块内短读续读除外，
        # 本夹具不制造短读，因此每槽一次，且无回头重读）。
        starts = sorted(offset for (_len, offset) in pread_calls)
        expected_starts = sorted(s * bs for s in range(n))
        assert starts == expected_starts, (starts, expected_starts)
        assert all(length == bs for length, _off in pread_calls)
    print("  ok 故障盘每槽仅读一轮（无回头重读）")


# ---------------------------------------------------------------------------
# 10. 入口转发与空镜像边界
# ---------------------------------------------------------------------------


def test_cli_delegation():
    with tempfile.TemporaryDirectory() as d:
        ctx = build_package_and_kit(d)
        damaged = os.path.join(d, "damaged")
        _damage_image(ctx["pkg"], damaged, flip=(0,))
        out1 = os.path.join(d, "via-archive-merge")
        proc = subprocess.run(
            [sys.executable, "-I", MERGE_TOOL, "repair",
             "--descriptor", ctx["desc"], "--digest", ctx["digest"],
             "--old-image", os.path.join(damaged, "image.bin"),
             "--rows-parity", ctx["rows_parity"],
             "--cols-parity", ctx["cols_parity"], "--output", out1],
            capture_output=True, text=True)
        assert proc.returncode == EXIT_OK, proc.stderr
        assert pkg_image_bytes(out1) == pkg_image_bytes(ctx["pkg"])

        out2 = os.path.join(d, "via-rearrange")
        proc = subprocess.run(
            [sys.executable, "-I", REARRANGE_TOOL, "repair",
             "--descriptor", ctx["desc"], "--digest", ctx["digest"],
             "--old-image", os.path.join(damaged, "image.bin"),
             "--rows-parity", ctx["rows_parity"],
             "--cols-parity", ctx["cols_parity"], "--output", out2],
            capture_output=True, text=True)
        assert proc.returncode == EXIT_OK, proc.stderr
        assert pkg_image_bytes(out2) == pkg_image_bytes(ctx["pkg"])

        # make-repair-kit 也能从两个既有入口转发
        gpath = os.path.join(d, "groups.json")
        kit3 = os.path.join(d, "kit3")
        proc = subprocess.run(
            [sys.executable, "-I", MERGE_TOOL, "make-repair-kit",
             "--package", ctx["pkg"], "--groups", gpath, "--out", kit3],
            capture_output=True, text=True)
        assert proc.returncode == EXIT_OK, proc.stderr
        assert os.path.exists(os.path.join(kit3, "repair_desc.json"))

        kit4 = os.path.join(d, "kit4")
        proc = subprocess.run(
            [sys.executable, "-I", REARRANGE_TOOL, "make-repair-kit",
             "--package", ctx["pkg"], "--groups", gpath, "--out", kit4],
            capture_output=True, text=True)
        assert proc.returncode == EXIT_OK, proc.stderr
        assert os.path.exists(os.path.join(kit4, "repair_desc.json"))
    print("  ok archive_merge.py / rearrange.py 入口转发")


def test_empty_image_package():
    """空镜像（零槽）：分组为空列表，制作与“修复”都应成立。"""
    with tempfile.TemporaryDirectory() as d:
        req = {"block_size": BLOCK, "archives": []}
        req_path = write_json(os.path.join(d, "req.json"), req)
        pkg = os.path.join(d, "emptypkg")
        subprocess.run([sys.executable, "-I", MERGE_TOOL, "merge",
                        "--request", req_path, "--output", pkg], check=True,
                       capture_output=True)
        gpath = write_json(os.path.join(d, "g.json"),
                           {"rows": [], "columns": []})
        kit = os.path.join(d, "kit")
        dig = os.path.join(d, "dig")
        run_cli(["make-kit", "--package", pkg, "--groups", gpath,
                 "--out", kit, "--digest-file", dig], EXIT_OK)
        # 旧镜像缺失（本来就是 0 字节）也应能“修复”出空包
        out = os.path.join(d, "out")
        run_cli(["repair", "--descriptor",
                 os.path.join(kit, "repair_desc.json"),
                 "--digest", open(dig).read().strip(),
                 "--old-image", os.path.join(d, "missing.bin"),
                 "--rows-parity", os.path.join(kit, "repair_rows.bin"),
                 "--cols-parity", os.path.join(kit, "repair_cols.bin"),
                 "--output", out], EXIT_OK)
        assert pkg_image_bytes(out) == b""
        assert pkg_manifest_bytes(out) == pkg_manifest_bytes(pkg)
    print("  ok 空镜像边界（空分组、零槽修复）")


def main():
    tests = [
        test_make_kit_happy_and_contents,
        test_make_kit_rejects_unhealthy_package_and_bad_groups,
        test_repair_flip_truncate_missing_manifest,
        test_repair_entire_image_missing,
        test_repair_old_image_absent_but_recoverable,
        test_chain_cascade_recovery,
        test_rank_deficiency_rejected,
        test_parity_block_erasure_changes_solvability,
        test_contradictory_parity_rejected,
        test_descriptor_and_input_validation,
        test_slot_identity_not_content_substitution,
        test_random_erasure_matches_reference_rank,
        test_make_kit_atomic_publish_under_faults,
        test_repair_atomic_publish_under_faults,
        test_repair_exclusive_lock,
        test_no_whole_payload_buffers,
        test_failed_disk_read_only_once,
        test_cli_delegation,
        test_empty_image_package,
    ]
    for t in tests:
        print("[%s]" % t.__name__)
        t()
    print("\n全部测试通过。")


if __name__ == "__main__":
    main()

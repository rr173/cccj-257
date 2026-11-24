# 多归档固定块去重合并（镜像 + 清单归档包）

把多个固定块数据文件按一次请求合并成一个占用更少空间的镜像，同时保留
每个来源归档每个原槽位在镜像中的位置，供读取系统还原原有归档内容。

本能力与原有的**单文件原位重排**（`rearrange.py` 的 `run` / `verify`）
相互独立：合并只读输入、只写新的输出目录，不改变任何输入文件，也不改变
原位重排的命令行为。

| 文件 | 作用 |
| --- | --- |
| `archive_merge.py` | 合并与核验主程序（仅 Python 3 标准库，建议 `python3 -I`） |
| `make_merge_request.py` | 请求生成助手（按块缓冲计算摘要，可造示例数据） |
| `test_archive_merge.py` | 端到端、失败路径、原子发布故障矩阵、随机被杀、篡改识别、规模测试 |
| `demo_archive_merge.sh` | 小文件端到端演示 |

## 请求（输入 JSON）

```json
{
  "version": 1,
  "block_size": 4096,
  "archives": [
    {"id": "alpha", "path": "a.bin", "digests": ["<sha256>", "..."]},
    {"id": "beta",  "path": "b.bin", "digests": ["<sha256>", "..."]}
  ]
}
```

* `block_size` 必须是正整数；所有文件大小必须是块长的整数倍。
* `archives` 的顺序即合并顺序；`id` 必须唯一；`digests[s]` 是归档槽位 `s`
  开始处理时的完整块的 SHA-256，数量必须等于槽位数。
* `path` 可为绝对路径；相对路径以**请求文件所在目录**为基准解析（若该路径
  不存在、而当前工作目录下确有同名文件，则回退到当前工作目录，兼容直接
  从工作目录调用）。
* 空 `archives` 列表和零槽位归档（空文件，`digests` 为空数组）都是合法输入。

## 用法

```bash
# 合并并按“镜像 + 清单”原子发布归档包
python3 -I archive_merge.py merge --request request.json --output out.pkg

# 按原请求全量核验归档包（镜像内容、映射、去重、清单签名）
python3 -I archive_merge.py verify --request request.json --package out.pkg

# 也可以从原位重排工具的入口转发
python3 -I rearrange.py archive-merge  --request request.json --output out.pkg
python3 -I rearrange.py archive-verify --request request.json --package out.pkg
```

生成请求：

```bash
# 用已有文件构造请求（按给出顺序合并）
python3 -I make_merge_request.py --block-size 4096 \
    --archive id=alpha:path=a.bin --archive id=beta:path=b.bin \
    --request request.json

# 造带重复块的示例数据并生成请求
python3 -I make_merge_request.py --sample --block-size 16 --blocks 6 \
    --archives 3 --seed 1 --request request.json --sample-dir sample
```

### 作为库调用

```python
import archive_merge

request = {
    "block_size": 4096,
    "archives": [
        {"id": "alpha", "path": "a.bin", "digests": [...]},
    ],
}
result = archive_merge.merge_package(request, "out.pkg", base_dir=".")
# result["status"] 为 "published" 或 "already-complete"
manifest = archive_merge.verify_package(request, "out.pkg", base_dir=".")
```

文件形式的请求可用 `merge_request_file(request_path, output_path)` /
`verify_request_file(request_path, package_path)`，相对路径以请求文件所在
目录为基准。

## 归档包布局

输出目录只有两个文件：

```
out.pkg/
├── image.bin      合并后的固定块镜像
└── manifest.json  清单（带自身 sha256 签名）
```

`manifest.json` 结构：

```json
{
  "format": "fixed-block-archive-package",
  "version": 1,
  "block_size": 4096,
  "request_sha256": "<规范化请求的 sha256>",
  "image": {
    "file": "image.bin",
    "slots": 3,
    "size": 12288,
    "sha256": "<镜像整体 sha256>",
    "digests": ["<镜像槽 0 摘要>", "..."]
  },
  "archives": [
    {
      "id": "alpha",
      "slots": 2,
      "size": 8192,
      "slot_map": [0, 2],
      "digests": ["<该归档原槽 0 摘要>", "<原槽 1 摘要>"]
    }
  ],
  "sig": "<去掉 sig 后的规范化 JSON 的 sha256>"
}
```

读取系统按 `archives[i].slot_map[s]` 即可从 `image.bin` 取回来源归档 `i`
原槽位 `s` 的字节；`digests` 与 `request_sha256` 足以按原请求核验每个槽位。

## 去重规则与确定性

* 镜像槽位按**请求中的归档顺序、再按各归档原槽位顺序**，记录每种不同块
  **第一次出现**的位置，因此相同请求、相同输入必然得到相同的槽位映射与
  相同的镜像字节。
* 候选合并以 SHA-256 建立索引，但**真正合并前一定回读镜像中候选块逐字节
  比较**：只有内容完全相同才复用槽位。摘要相同而字节不同的块不会被误合并
  （`verify` 还会在镜像内部检查“内容相同的块不得出现两份”）。
* 不同摘要必然对应不同槽位；相同内容必然指向同一槽位。

## 原子发布与失败语义

镜像先写入输出目录同级的隐藏临时目录（`.archive_merge.<名字>.tmp.*`），
清单也在其中以 tmp → fsync → rename → 目录 fsync 原子落盘；两者都持久化
并 `fsync` 后，才用**一次目录 rename** 把整个归档包发布到最终路径。

* 进程在任何时刻被杀或失败，最终路径上都不会出现只有镜像或只有清单的
  半成品，也不会出现两者互不匹配的归档包；
* 失败时不会发布任何可被读取系统当作完成结果的输出；隐藏临时目录会在
  下一次同一输出的合并持锁后清理；
* 输入文件在本次处理期间由调用方保持不变：程序逐块核验摘要，并在开始与
  结束比较文件的设备/inode/大小/mtime/ctime，处理期间被改动会报错且不发布。

## 输入错误的拒绝（退出码 3，零发布）

* 请求结构非法（`block_size` / `archives` / `digests` 类型、版本）；
* 归档 `id` 重复；
* 文件不可读、不是普通文件、大小不是块长整数倍、摘要数量与槽位数不符；
* 任一槽位摘要与文件内容不符（含“声明摘要相同但字节不同”，此时按实际
  字节比较绝不会误合并，而摘要核验会直接失败）。

## 幂等重开与既有输出

* 输出已存在时，先按**同一请求**对归档包做全量核验：通过则报告
  “此前已完成”，不做任何改动；
* 不通过则拒绝覆盖（退出码 7），既有内容保持原样；需要重建时请换输出路径
  或由调用方自行移走旧包。

## 资源约束

* 载荷（归档读、镜像写/回读）始终只使用 `block_size` 大小的缓冲：
  没有 mmap、没有整文件 `read()`，缓冲不随总数据量增长；
* 映射、去重索引、摘要等非载荷清单可随来源槽位数/不同块数增长；
* 同一输出加 `flock` 独占锁，第二个进程以退出码 6 立即失败，锁随进程退出
  自动释放。

## 退出码

| 码 | 含义 |
| --- | --- |
| 0 | 成功（本次发布，或此前已按同一请求完成的幂等重开/verify） |
| 2 | 命令行用法错误 |
| 3 | 请求或输入文件核验失败（未发布归档包） |
| 4 | 归档包核验失败（清单签名/映射/镜像/去重不符） |
| 5 | 介质 IO 错误 |
| 6 | 同一输出已被另一进程独占 |
| 7 | 输出已存在且不是本请求的有效完成结果 |
| 9/137 | 测试故障注入主动 `os._exit`（正常运行不会出现） |

## 测试与演示

```bash
python3 -I test_archive_merge.py   # 全套（含原子发布故障矩阵与篡改识别）
./demo_archive_merge.sh            # 小文件合并 -> 核验 -> 篡改拒绝 -> 重开幂等
```

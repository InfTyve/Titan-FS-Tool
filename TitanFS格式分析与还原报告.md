# VPhoneGaga（光速 Titan）虚拟机文件系统 `fs/` 格式逆向报告

> 样本：`fs.zip`（1.11 MB）、`dex.zip`、`lib.zip`、`lib2.zip`
> 结论先说：**没有加密**。这是 VPhoneGaga 自研的 "Titan FS" 容器格式，文件内容以明文存储，只带 CRC32 校验。
> 你看到的“乱码”来自三件事：文件名是十六进制 inode 号、元数据是自描述二进制结构、大文件被塞进带对齐空洞的容器镜像。

---

## 1. 宿主侧目录长这样（虚拟里是普通目录）

```
fs/
├── androidfs.bin        64 B      ROM 描述符（魔数 SFDA）
├── config.gz            1.3 KB    guest 内核 .config（可 zcat）
├── locales.bin          491 B     语言列表，纯文本 "en_US en_AU ..."
├── fscache.bin          402 KB    只读层路径缓存（dentry 索引）
├── root/                ← 挂载点 /
│   ├── superblock.bin   16 B      魔数 BPUS，gen=11
│   └── readonly.bin     1.8 MB    只读层镜像（ATIT 索引容器）
├── data/                ← 挂载点 /data
│   ├── superblock.bin   16 B      BPUS，gen=5
│   ├── fscache.bin      64 MB     inode 记录池（稀疏，实际仅前 1 MB 有数据）
│   └── 00000000/
│       ├── 00000001     16 B      = /data/local.prop 的文件内容
│       └── 00000004     2419 B    = /data/misc/wifi/WifiConfigStore.xml
└── cache/               ← 挂载点 /cache
    ├── superblock.bin   16 B      BPUS，gen=5
    └── 00000000/
        ├── 00000000     52 B      目录内容（文本：name=inode）
        ├── 00000000.metadata 138 B  ATEM inode 元数据
        └── 00000001 … 00000004 + .metadata
```

命名规律（`libuserkernel64.so` 里的格式串 `%08x/%08lx.metadata`、`%s.metadata` 印证）：

- `<8位十六进制>/` 是 **inode 分桶目录**，推测 `桶号 = inode >> 8`（样本 inode 都 < 256，全落在 `00000000`）；
- `<8位十六进制>` 是 **该 inode 的数据文件**（目录的内容是 `名字=inode\r\n` 文本）；
- `<8位十六进制>.metadata` 是 **该 inode 的元数据**（魔数 ATEM）。

---

## 2. 四个魔数 = 四种结构

| 魔数 | 出现位置 | 含义 |
|---|---|---|
| `SFDA` | `androidfs.bin` | Android FS 描述符（`SFDA`+ver=1000+183）。dex 里有 `androidfs_10.0.0`、`androidfs_%d.%d.%d`、`ENGINE_ERROR_ANDROIDFS_CORRUPTED`，说明 ver=1000 表示 ROM 版本 **10.0.0**，183 是内部 build |
| `BPUS` | 各分区 `superblock.bin` | 16 字节超级块：`BPUS` + 头长 16 + 代次 gen + 保留。gen 是写回计数，用于缓存一致性 |
| `ATIT` | `root/readonly.bin`、`data/fscache.bin` | 两种用法：① 索引镜像（只读层）② 变长 inode 记录池 |
| `ATEM` | `*.metadata` | 单 inode 元数据（mode/uid/gid/时间/xattr） |

> 魔数字节序有点反直觉：`META` 写成 `ATEM`、`ADFS` 写成 `SFDA`，是作者按“小端读出来像词”写的，不是加密。

### 2.1 `ATIT` 索引镜像（`root/readonly.bin`）

```
+0x00  'ATIT'
+0x04  u32 头长 = 0x40
+0x08  u32 条目数 = 10
+0x0C  u32 已用末尾 = 0x1BFDB8
+0x10  u32 最后一个数据块偏移 = 0x1BFCC1
+0x14  u32 0x91922793（校验/种子，未验证）
+0x18  u32 条目大小 = 52
+0x40  N × 52 字节条目
        mode, uid, gid, nlink,
        mtime, mtime2, ctime, ctime2,
        flag(恒为 0x50000000), size, data_off, crc32, meta_len
之后是数据区：[文件数据][xattr 块]，offset 由条目给出
```

- 目录内容：`u32 名字长(含\0) + 名字 + u64 子条目索引`，紧密排列。
- 每个文件的 xattr 块紧跟在数据之后（`01 00 00 00` + `security.selinux\0` + u32 长度 + 值）。
- **CRC32 是明文数据的 CRC**：`/init` 条目声明 `0x1D641438`，对数据区实算得到 `0x1D641438`，完全一致 → 证明数据未加密。

样本解出的只读层：

```
/                     drwxr-xr-x
├── apex/  debug_ramdisk/  dev/  mnt/  proc/  share/  sys/   (空目录)
├── fstab.${ro.hardware}   241 B   -rw-r-----  uid0 gid2000
└── init                   1830032 B  -rwxr-x---  ELF64 aarch64 静态链接
```

`/init` 可以直接 `readelf`、`file` 识别，是 VPhoneGaga 的 guest init。

### 2.2 `ATIT` 记录池（`data/fscache.bin`）

文件头 64 字节：`ATIT` + 头长 0x40 + 镜像总长（0x04000000=64 MB）+ 0x40 + 首条记录偏移 0x100040 + 0x40000 + 空闲量 + 末条偏移；
第 1 条记录固定在 **1 MB 处**，之后记录首尾相接，每条用 `prev` 指向上一条（反向链，事务/回滚用）。

记录头 84 字节：

| 偏移 | 类型 | 含义 |
|---|---|---|
| +0 | char[4] | `ATIT` |
| +4 | u32 | 本记录总长 |
| +8 | u32 | 状态（1=有效） |
| +12 | u64 | 上一条记录偏移 |
| +20 | u64 | inode 号 |
| +28 | u64 | 文件大小 |
| +36 | u32 | mode |
| +40/+44/+48 | u32 | uid / gid / nlink |
| +52/+56 | u32 | mtime / mtime 副本 |
| +60 | u32 | ctime |
| +64 | u32 | xattr 区长度（含 4 字节计数） |
| +68 | u32 | 目录项区长度 |
| +72 | u32 | 保留 |
| +76 | u32 | atime |
| +80 | u32 | xattr 个数 |
| +84 | — | xattr 数组：`name\0 + u32 值长 + 值` |
| 80+len | — | 目录项数组：`u64 子inode + u32 名字长(含\0) + 名字`，紧密排列，记录尾部 0~3 字节补齐 |

> 注意 `mode==0` 的记录是“池头/池尾超级块”（`ATIT` + 总量 + 下一可用偏移），不是 inode，解析时必须跳过，否则会把 inode 0 覆盖成空节点。

样本解出的 `/data`：

```
/data
├── local.prop              16 B  -rw-r--r--  uid0 gid0
└── misc/                  drwxrwx--t   uid1000 gid9998
    └── wifi/              drwxrwx--t
        └── WifiConfigStore.xml  2419 B  -rw-r--r--
```

文件内容是明文 XML（`<?xml version='1.0' encoding='utf-8' standalone='yes'?>` + `WifiConfiguration` 节点）。

### 2.3 `ATEM` 元数据（`*.metadata`）

```
+0  'ATEM'   +4 u32 头长(0x30=48)
+8  mode     +12 uid   +16 gid   +20 nlink
+24 mtime    +28 mtime 副本(旧实现把秒写进了纳秒字段)
+32 ctime    +36 ctime 副本
+40 size     +44 xattr 区长度   +48 xattr 个数
+52 xattr 数组
```

### 2.4 `fscache.bin`（fs 根目录那个 402 KB）

只读层的“路径 → inode”加速索引：3798 个可打印串、391 条完整路径（`/init`、`/sbin/adbd`、`/system/bin/app_process32`、`/system/vendor/lib/libhoudini.so` …）+ 单调递增的 u32 键数组。
它不是数据本体，**丢了对还原文件没有任何影响**（App 会重建）。样本里它的存在说明这台 VM 的 `/system` 是另一份 `androidfs_10.0.0` ROM 包挂进来的，ROM 本体不在本次上传里。

---

## 3. “加密”到底存不存在？—— 5 条反证

| 检查 | 结果 |
|---|---|
| 数据熵 | 内容区 4.9–5.5 bit/byte（可执行文件典型值），加密/压缩应 ≈ 7.9 |
| ELF | `root/readonly.bin` 偏移 0x1000 起是完整 ELF64 aarch64，`readelf -h/-l` 正常 |
| CRC32 | 索引里声明的 CRC 与明文实算一致（`/init` = `0x1D641438`） |
| 文本 | fstab、`WifiConfigStore.xml`、`local.prop`、`security.selinux` xattr 全为明文 |
| 二进制 | `libuserkernel64.so` 中能搜到 `ATIT`/`BPUS` 串，且导出 `init_kernel_api`、`main`；其中的 OpenSSL 1.1.1d、libspake2 是给 TLS/adb 配对用的，FS 路径没有 AES/EVP 调用 |

所以：**不存在需要“解密”的密钥**。要用工具“解锁”，只需要按结构解析。

---

## 4. 附带的解析工具 `titanfs.py`

纯 Python 3、无第三方依赖，直接对 `fs/` 目录操作：

```bash
python3 titanfs.py info    fs/                 # 魔数、版本、各层 inode 统计
python3 titanfs.py tree    fs/                 # 打印所有层的目录树
python3 titanfs.py tree    fs/ --layer data    # 只看 /data
python3 titanfs.py ls      fs/ misc/wifi       # 等价 ls
python3 titanfs.py extract fs/ out/            # 按层还原成真实目录
```

本次样本的实际运行结果：

```
层 root(readonly)   inode=10  目录=8  文件=2
层 data             inode=5   目录=2  文件=3
层 cache            inode=5   目录=5  文件=0

/workspace/extracted/root_readonly/init                     1830032 B  ELF64 aarch64
/workspace/extracted/root_readonly/fstab.${ro.hardware}         241 B
/workspace/extracted/data/local.prop                             16 B
/workspace/extracted/data/misc/wifi/WifiConfigStore.xml         2419 B
```

导出的 `WifiConfigStore.xml` 与原始 `data/00000000/00000004` 逐字节一致（`cmp` 通过）。

实现要点（换到别的实例也能用）：

1. 三个分区各自解析：先读 `fscache.bin` 的 ATIT 记录（最新），再用 `<inode>.metadata` 补齐（旧式落盘），最后按 inode 号读内容文件；
2. 目录内容兼容三种表示：ATIT 记录里的目录项数组、`name=inode` 文本、`u32 长度+名字+u64` 结构；
3. 跳过 ATIT 池头/池尾超级块（`mode==0`）；
4. 目录项区从 `+80+xlen` 起算，同时用“记录尾 − 目录项长度”做候选校验，兼容尾部补齐差异。

---

## 5. 实操建议

1. **不要手工改这些 bin 再塞回 App**。`superblock.bin` 里的 gen 是代次号，`fscache.bin` 与 `.metadata` 有主从关系，改错就会触发 `ENGINE_ERROR_ANDROIDFS_CORRUPTED`。
2. 只想拿文件：把整个 `fs/` 拷到电脑，跑 `titanfs.py extract`；想反向放回：用同一路径覆盖 `fs/` 整个目录，让 App 自己重建缓存（先删该分区 `fscache.bin` 更保险）。
3. **上传建议**：`data/fscache.bin` 是 64 MB 但基本全是 0（稀疏），zip 后只有 1 MB 左右，所以“fs.zip 才 1 MB”不代表 VM 里没数据。真正占空间的是各分区的 `<inode>` 内容文件和 ROM 包。分批上传时优先传 `data/`、`cache/` 下的 `00000000/…` 内容文件；体积大的 `fscache.bin` 可以用 `tar -S`（稀疏打包）或 `dd` 跳过全零段。
4. 本次样本 `/data` 只有 4 个 inode、`/cache` 只有 4 个空目录，说明这台 VM 几乎是**全新实例**（或真正有数据的实例在别的目录）。要看具体 App 的数据，需要上传那种用过的实例的 `data/` 分区。
5. `/system`、`/system/app` 这些属于 ROM 包（`androidfs_10.0.0`），不在 `fs/` 里；要提取 ROM 内容得拿 APK/OBB 里的 `androidfs*` 资源包。

---

## 6. 附录：另外几个上传包的作用

| 包 | 内容 | 与本题的关系 |
|---|---|---|
| `dex.zip` | `classes.dex` / `classes2` / `classes3`（21.7 MB） | Java 侧只负责管理实例：`com.vphonegaga.titan.VPhoneManager`、`IVPhoneInstance`、`IVPhoneConfig`；ROM 版本键 `androidfs_10.0.0` / `androidfs_7.1.2` / `androidfs_version.xml`，错误码 `ENGINE_ERROR_ANDROIDFS_CORRUPTED / MISSING / VERSION_MISMATCH / EXTRACT_ANDROIDFS_FAIL`。没有任何 FS 加密代码 |
| `lib2.zip` | `libVPhoneGaGaLib/Lim/Lin.so` | VM 引擎主体（渲染、输入、虚拟设备） |
| `lib.zip` | 24 个 so | 关键：**只有 64 位那份 `libuserkernel64.so` 里能搜到 `ATIT`/`BPUS` 魔数**，即 Titan FS 的实现在 guest 内核里；导出符号为 `init_kernel_api` / `main` / `shell_process` / `notify_rendered`。`libuserkerneI.so`（末尾是大写 `I` 而非 `l`）是 2024-02 的 32 位新版，与 2023 版 `libuserkernel.so` 符号一致，疑似同名混淆/规避按名加载，但没有 FS 魔数 |


---

## 7. 工具二：`titanunpack.py` / `titanpack.py`（解包 + 打包闭环）

两个脚本都**不需要装任何 Python 包**：优先用 `py7zr`，没装时自动回退到系统 `7z`/`7za` 命令行（Alpine 上 `apk add p7zip` 即可，几十 KB）。解包会自动识别 7z / 7z.001 分卷 / zip / tar，并在解压结果里递归找出所有实例的 `fs/` 根。

> Alpine / 受 PEP 668 保护的 Python 环境里 `pip install py7zr` 会报 `externally-managed-environment`，此时直接 `apk add p7zip` 就行，不必动系统 Python。

### 7.1 解包

```bash
python3 titanunpack.py fs.7z -o out          # 自动解压 + 自动发现分区 + 导出
python3 titanunpack.py fs.7z -o out --list   # 只看目录树
python3 titanunpack.py fs.7z -o out --only data,vendor
```

输出结构：

| 路径 | 内容 |
|---|---|
| `out/raw/` | 原始解压结果（含 `androidfs.bin`、`superblock.bin`、`fscache.bin`） |
| `out/by-partition/<分区>/` | 按分区还原的真实文件树 |
| `out/merged/` | Android 视角的合并视图（`/system`、`/data` … 按挂载点摆好） |
| `out/manifest.tsv` | 路径 ↔ inode ↔ mode/uid/gid/size/xattr 清单（打包回灌靠它） |
| `out/links.tsv` | 所有符号链接的目标与处理方式 |

### 7.2 符号链接：默认“实体化”，不产生任何软链

虚拟机镜像里 `/system/bin/sh -> mksh`、`/system/vendor -> /vendor` 这类链接极其常见。若直接 `os.symlink()` 落地，宿主机上就多出一堆**指向自己文件系统的指针**——`/vendor` 会真的指到本机 `/vendor`，任何跟随链接的复制、打包、杀软扫描都会“穿透”读到虚拟机之外的文件。

默认策略 `--links materialize`：

| 情况 | 处理 |
|---|---|
| 链接目标解析到镜像内普通文件 | 复制内容，落成**普通文件** |
| 目标解析到镜像内目录 | 建**真实目录** |
| 目标越出镜像根（`../../etc/passwd`） | 不落盘，只记 `links.tsv` |
| 目标在镜像内不存在（`/etc/passwd` 这类宿主路径） | 不落盘，只记 `links.tsv` |
| 目标本身是链接 | 最多跟 8 跳，仍不落地 |

链接目标一律在**镜像命名空间内**换算：镜像里的 `/vendor/lib/x.so` 指本分区根下的 `/vendor/lib/x.so`，不会变成宿主机的 `/vendor`。导出结束还会自检一遍输出目录，发现残留软链直接清理并报警。

需要原样保留链接时用 `--links native`（自动把绝对目标改成相对路径）；都不想落地用 `--links skip`。

### 7.3 打包 / 回灌

```bash
# 1) 原样重打包
python3 titanpack.py out/raw -o fs.7z --prefix backup/vphone/files/vm1/fs

# 2) 改完文件回灌（覆盖层按 Android 路径组织）
python3 titanpack.py out -o fs.7z --overlay mypatch/ --prefix backup/vphone/files/vm1/fs

# 3) 只回灌，不打包（先在副本上演练）
python3 titanpack.py out --overlay mypatch/ --apply-only --dry-run

# 4) 已经改了 by-partition 导出副本？一键收养回镜像（v2026.11.02+）
python3 titanpack.py out -o fs.7z --adopt-unpacked out --strict
python3 titanpack.py out -o fs.7z --adopt-unpacked      # 省略目录=自动向上找 by-partition
```

`--overlay` 里放 `data/misc/wifi/WifiConfigStore.xml` 这种相对路径，工具查 `manifest.tsv` 找到 inode，直接覆写 `<分区>/<桶>/<inode>`，并同步更新 `.metadata` 的 size 与 ATIT 记录池里的 size 字段。改完再解包能读回新内容（已用 2419 B → 126 B 的样本验证）。

`--adopt-unpacked` 解决的是最常见的误操作：直接改了 `out/by-partition/...`。它逐文件对比导出副本与镜像内 inode 内容（大小不同直接算改过；大小相同再比 mtime 和 sha256），把有差异的写回镜像，等价于自动生成一份 overlay。配合 `--dry-run` 可先看清单，配合 `--apply-only` 只收养不打包。注意它**只处理镜像里已存在的路径**，新增文件仍需 `--overlay`（且要先在虚拟机内建同名文件）。

打包侧的安全约束：

- 符号链接**不进包**，明细写 `pack-links.tsv`（`--links text` 才写出 `.link.txt` 文本，仍不跟随）；
- 所有条目名规范化，`..`、绝对路径一律拒绝，防压缩包穿越；
- 打包后自动重开校验条目数与解压体积；
- `--volume 20m` 按字节切片成 `fs.7z.001/002…`（`cat fs.7z.0* > fs.7z` 即可合并）。

**限制**：新增文件（镜像里原本没有的路径）暂不支持回灌，因为要同时改父目录项与 inode 分配；需要新增时先在虚拟机里建同名空文件，再用覆盖层写入内容。

### 7.4 怎么保证“打包完能直接导入”

引擎挂载时会校验三样东西，任何一样不对就抛 `ENGINE_ERROR_ANDROIDFS_CORRUPTED`。`titanpack.py` 在打包前会逐项跑一遍：

| 检查项 | 判定依据 | 工具动作 |
|---|---|---|
| 包内结构 | `androidfs.bin` 必须在包根，分区目录与它同级 | 直接从 fs 根遍历，不加多余前缀 |
| 分区代次 | 每分区 `superblock.bin` 为 `BPUS`+16 字节，带 gen（样本 root=11、data/cache=5） | 打印实际 gen，不改写 |
| 记录池一致性 | `fscache.bin` 里每条 `(inode,size)` 必须等于 `<桶>/<inode>` 的实际大小 | 默认自动改写池内 size 字段；`--no-fix` 只报告 |
| 只读层完整性 | `readonly.bin` 每条 `crc32(数据区) == 表内 crc` | 逐条校验，不符直接报错（禁止带病打包） |

```bash
python3 titanpack.py out -o fs.7z --strict     # 检查不过就中止，退出码 3
python3 titanpack.py out -o fs.7z --no-fix     # 只报告，不自动修
```

同时生成 `IMPORT.txt`（不进包），写清包内结构和导入步骤。

**只读层不可回灌**：`root` 分区的内容内联在 `readonly.bin` 的数据区，改一个字节就要重排偏移、重算 CRC、还要动 64 字节头里的总长字段，风险远大于收益。覆盖层里若包含 `root/...` 的文件，工具会明确拒绝并提示原因。要改 `/system`、`/init` 这类东西，得换整个 ROM 包（`androidfs_10.0.0`），不在 `fs/` 这一层。

**为什么不能“删掉 fscache.bin 让引擎重建”**：实测把 `data/fscache.bin` 删除后，该分区的目录项、inode 号映射全部丢失（`data` 分区只剩 2 个 16 字节文件，目录树为空）——记录池是**唯一**的目录结构来源，不是可丢弃的缓存。所以回灌必须就地改池内字段，不能清缓存。

### 7.5 包内目录层：别把 `androidfs_10.0.0/` 打丢了

同一套工具会遇到两种包布局：

| 原始包结构 | 打包时应有的前缀 |
|---|---|
| `fs.zip` 根下直接是 `androidfs.bin`（用户数据目录） | 无前缀 |
| `xxx.zip` 根下是 `androidfs_10.0.0/`，里面才是 `androidfs.bin`（ROM 全量包） | `androidfs_10.0.0` |

`titanpack.py` 会从 fs 根往上找名为 `raw` 的解包目录：`out/raw/androidfs_10.0.0` → 自动取前缀 `androidfs_10.0.0`；`out/raw` → 前缀为空。日志里会打印 `包内前缀（自动）`，也可用 `--prefix` 覆盖。

先探路再打包：

```bash
python3 titanpack.py /root/workspace/out --check-only     # 只检查，不产包
```

### 7.6 排障：先确认你跑的是哪一版脚本

旧版 `titanpack.py` 只有 py7zr 后端，**装了 7z 命令行也照样报**：

```
[错误] 缺少 py7zr，请先: pip3 install py7zr
```

这句提示在新版里已经不存在了。核对办法：

```bash
python3 titanpack.py --version      # titanpack 2026.11.01
python3 titanpack.py --doctor       # 一眼看清 7z / py7zr / 磁盘
```

`--doctor` 输出示例：

```
=== 环境自检（titanpack 2026.11.01）===
Python  : 3.11.1
7z 命令 : /usr/bin/7z
           7-Zip 23.01 (x64)
py7zr   : 未安装（有 7z 命令就不需要它）
磁盘    : 当前目录剩余 252.0 GB / 共 256.0 GB
结论: 可以直接打包 —— python3 titanpack.py <镜像目录> -o fs.7z --strict
```

新版打包时日志会打印实际用到的后端和模式：

```
[3/3] 打包 -> fs.7z（7z）
  后端: /usr/bin/7z  7-Zip 23.01 (x64)
  就地打包（不复制文件）: cwd=.../out/raw  arg=androidfs_10.0.0
```

**就地打包**是大镜像的关键：cwd 设为镜像根的父目录，直接 `7z a`，不再把 1.7 GB 硬链接/复制到临时目录。只有两种情况会退回临时目录模式：包内前缀与镜像根层级对不上，或者镜像里存在符号链接（为保证不把链接打进包）。

后端优先级：**系统 7z 命令行 > py7zr**。py7zr 是纯 Python，1 GB 以上会慢到不可用，且吃内存。

### 7.7 分卷

```bash
python3 titanpack.py out -o fs.7z --volume 20m      # 切成 fs.7z.001 / .002 …（纯字节切片）
cat fs.7z.??? > fs.7z                               # 合并：三字符通配，别用 .0*
python3 titanunpack.py fs.7z.001 -o out             # 解包端会自动找齐并拼接
```

> 注意 `cat fs.7z.0*` 只能匹配到第 99 片，`.100` 往后会被漏掉，合并出来的包必然 `Bad7zFile`。必须用 `???`（或 `sort -V`）。

解包端处理分卷的方式是先按编号拼成一个临时 `.7z` 再解，7z 原生分卷和本工具切片的分卷走同一条路径。

### 7.8 大镜像的建议参数

| 内容 | 建议 |
|---|---|
| ROM 全量包（`androidfs_10.0.0/`，1 GB 以上） | `--level 3`（mx3），ROM 里多是已压缩的 apk/img，压太狠收益低、耗时成倍 |
| 只是想把文件搬回手机 | `--level 0`（仅存储），速度最快，包≈原大小 |
| 用户数据目录（`fs.zip` 那种，几十 MB） | 默认 `--level 5` |

### 7.9 换了系统 APK 却不生效，怎么定位

现象：把 `/system/priv-app/.../Settings.apk` 换成改好的版本、平台签名、导入后界面**一点没变**，功能还正常。签名可以排除，这基本只有两种可能：

| 判定 | 含义 | 处理 |
|---|---|---|
| 包内哈希 == 你本地文件 | 文件进包了，系统没换 | 查 oat/odex、多副本、实例路径 |
| 包内哈希 != 你本地文件 | 改动根本没进包 | 改法错了，用 `--overlay` 重来 |

用 `apkcheck.py` 一条命令判定：

```bash
python3 apkcheck.py fs.7z --apk Settings.apk --local 改好的.apk
```

输出会直接告诉你结论，并列出该 APK 在镜像里的真实文件（`<分区>/<桶>/<inode>`）、来源（普通桶 / 记录池 / 只读层）、以及同目录有没有 `oat/`、`*.odex`。

**最常见的错误改法**：解包后去改 `out/by-partition/system/priv-app/Settings/Settings.apk`。那个目录是**导出的可读副本**，`titanpack.py` 打的是 `out/raw/` 里的镜像数据，两者毫无关系。现在打包时会检测并警告：

```
[警告] out/by-partition 里有 1 个文件比镜像新（你改的应该是这份副本）
        /system/priv-app/Settings/Settings.apk
       那是解包导出的副本，打包读的是 raw/ 镜像，直接改它不会进包。
```

正确做法 A（已经改在 by-partition 里了，一条命令收养）：

```bash
python3 titanpack.py out -o fs.7z --adopt-unpacked out --strict
```

正确做法 B（走覆盖层）：

```bash
mkdir -p patch/system/priv-app/Settings
cp 改好的.apk patch/system/priv-app/Settings/Settings.apk
python3 titanpack.py out -o fs.7z --overlay patch --strict
# 再核对一遍
python3 apkcheck.py fs.7z --apk Settings.apk --local 改好的.apk
```

回灌时会自动同步 `.metadata` 的 size 和记录池里的 size 字段，这两处不同步，引擎会按旧长度截断文件，APK 直接损坏。

**如果 APK 在只读层**（`来源 只读层(readonly.bin)`）：内容内联在 `readonly.bin` 的数据区，改一个字节要重排偏移 + 重算 CRC，工具会拒绝回灌。这种分区里的 APK 没法用覆盖层换。

**如果哈希一致但界面没变**，按顺序查：

1. `apkcheck` 报的“伴随预编译”里有 `oat/arm64/Settings.odex` 之类的，删掉整个 `oat/` 目录；
2. 同一个 APK 在多个分区/多个路径各有一份（`apkcheck` 会列出全部命中），系统加载的可能不是改的那份；
3. 导入的实例目录不是你以为的那个 `vm<N>`。

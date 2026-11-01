# Titan FS 光速虚拟机解包工具集 · 使用手册

六个脚本 + 一份逆向报告，全部单文件、无第三方依赖（`titanpack.py` 可选 `py7zr`，但优先用系统 `7z`），Python 3.8+ 即可运行。

---

## 0. 文件清单

| 文件 | 一句话职责 | 写盘 | 典型输入 |
|---|---|---|---|
| `titanfs.py` | 只读解析单个分区：看目录树、权限、大小 | 否（`extract` 除外） | 解包后的 `fs/` 目录 |
| `titanunpack.py` | 一键解包，还原全部分区为可读文件树 | 是 → `out/` | `fs.7z` / `fs.7z.001` / `fs.zip` / 目录 |
| `titanpack.py` | 打包成可导入的 ROM 包；把改动回灌进镜像 | 是 → `fs.7z` | `out/` 或 `out/raw/` |
| `apkcheck.py` | 核对包里某个 APK 是不是你改的那一份 | 否 | `fs.7z` 或 `out/` |
| `titangrep.py` | 在包/镜像里搜字符串，自动穿透 APK | 否（临时解压） | `fs.7z` 或 `out/raw/` |
| `ropatch.py` | 直接改写 `readonly.bin`（只读层） | 是 | `fs.7z` 或 `system/readonly.bin` |
| `TitanFS格式分析与还原报告.md` | 格式逆向结论、结构表、排障记录 | — | — |

**关键区分**：`titanunpack` 导出的 `out/raw/` 是镜像真实数据（打包读它），`out/by-partition/` 是还原出来的可读副本（打包**不读**它）。改错地方是这套工具最常见的坑。

---

## 1. 我要做什么 → 用哪个

| 目的 | 命令 |
|---|---|
| 把 ROM 包解成能浏览的文件树 | `titanunpack.py` |
| 看看里面有哪些分区、多大、什么版本 | `titanunpack.py --list` 或 `titanfs.py info` |
| 改 `build.prop`、XML、配置文件后重新打包 | `titanunpack` → 改 → `titanpack --adopt-unpacked` |
| 新增/替换文件（干净的改法） | `titanunpack` → 建 `patch/` → `titanpack --overlay` |
| 替换 `/system/priv-app` 里的 APK 但界面没变 | `apkcheck.py` → `ropatch.py` |
| 确认改动到底进没进包 | `apkcheck.py` / `titangrep.py` / `ropatch.py apks` |
| 只要一个可导入的包，不做任何修改 | `titanunpack` → `titanpack` |

---

## 2. 环境准备

```bash
python3 --version          # 需要 3.8+
python3 titanpack.py --doctor
```

`--doctor` 会检查 7z 是否可用、py7zr 是否安装、磁盘空间是否够。7z 缺失时：

| 系统 | 安装命令 |
|---|---|
| Alpine（虚拟机 / Termux 常见） | `apk add p7zip` |
| Debian / Ubuntu | `apt install p7zip-full` |
| macOS | `brew install sevenzip` |
| Windows | 装 7-Zip 并确保 `7z.exe` 在 PATH |

没有 7z 也能跑，`titanpack.py` 会退回 `py7zr`（`pip install py7zr`），但速度慢一半以上。

---

## 3. titanfs.py —— 单分区只读解析

### 何时用

- 只想看看某个分区的目录树、文件权限、uid/gid、大小，不想导出几个 GB；
- 排查"这个 inode 到底是什么"，需要看 `partition/path/inode/mode/size`；
- 手上已经有一个 `fs/` 目录（App 私有目录里那种）。

### 命令

```bash
python3 titanfs.py info    <fs目录>
python3 titanfs.py tree    <fs目录> [--layer all|data|cache|root]
python3 titanfs.py ls      <fs目录> <路径>
python3 titanfs.py extract <fs目录> <输出目录> [--layer ...]
```

### 参数

| 参数 | 含义 |
|---|---|
| `fs` | 含 `androidfs.bin` 与 `root/` `data/` `cache/` 的容器目录 |
| `--layer` | 只看某一层；`all` 为默认，含 `root` 层和 `readonly.bin` 只读层 |

### 输出示例

```
$ python3 titanfs.py info fs
== 容器目录: fs
androidfs.bin : magic=SFDA ver=1000 field=183
root  superblock: magic=BPUS hdr=16 gen=11
层 root             inode=2       目录=1     文件=1

$ python3 titanfs.py tree fs

=== 层: root  (根 inode=0, mode=drwxr-xr-x, 子项 2)
default.prop                             ino=2            53  -rw-r--r--

$ python3 titanfs.py ls fs /
=== root : / (ino=0 drwxr-xr-x)
  default.prop                           ino=2      -rw-r--r--       53

$ python3 titanfs.py extract fs dump
已导出层 root             -> dump/root
```

`tree` 里出现 `[缺失 inode N]` 表示目录项指向了一个没有内容的 inode，通常是记录池里被裁剪过的陈旧项，属于正常现象。

### 限制

- 不生成 `manifest.tsv`，不能用于后续回灌；
- 不支持 7z/zip 直接输入，必须先解压；
- 不处理符号链接，遇到就按普通文件读。

---

## 4. titanunpack.py —— 一键解包

### 何时用

拿到 `fs.7z`、`fs.7z.001`（分卷）、`fs.zip`，想得到一个能直接浏览、能改、能重新打包的目录。**所有改文件的工作都从这一步开始。**

### 命令

```bash
python3 titanunpack.py fs.7z -o out
```

### 参数

| 参数 | 含义 |
|---|---|
| `src` | `7z` / `7z.001` / `zip` / `tar`，或已解压的目录 |
| `-o, --out` | 输出目录，默认 `./unpacked` |
| `--list` | 只列目录树，不导出文件内容（仍要解压，但省去几 GB 写入） |
| `--only` | 只处理指定分区，逗号分隔，如 `--only data,vendor` |
| `--no-raw` | 导出后删除 `raw/`（省空间，但**之后不能用它打包**） |
| `--no-merge` | 不生成 `merged/` 合并视图 |
| `--keep-meta` | 保留 `.metadata` 等原始文件到 `raw/` |
| `--links` | 链接策略：`materialize`（默认）/ `native` / `skip` |

### 输出结构

```
out/
├── raw/                     镜像原始数据，titanpack 打包读这里
│   └── androidfs_10.0.0/
│       ├── androidfs.bin
│       ├── system/superblock.bin  fscache.bin  <桶>/<inode>
│       └── data/  cache/  root/  vendor/
├── by-partition/            按分区还原的可读树（给人看的）
│   ├── system/priv-app/Settings/Settings.apk
│   └── data/misc/wifi/WifiConfigStore.xml
├── merged/                  Android 视角的合并树（/、/system、/vendor…）
├── manifest.tsv             每个文件的 分区/路径/inode/权限/大小
├── links.tsv                没能落盘的链接（越界、特殊文件）
└── report.txt               控制台日志存档
```

### 关于链接

`--links materialize`（默认）把符号链接**实体化**：目标在镜像内就复制成真实文件或真实目录，绝不生成指向宿主机的软链。越界目标（如 `/proc/self/exe`）不落盘，只记进 `links.tsv`。

需要原始语义时用 `--links native`，但注意：`titanpack.py` 检测到镜像含链接会强制走 staging，避免链接穿透。

### 分卷包

```bash
python3 titanunpack.py fs.7z.001 -o out     # 直接给 .001 即可，自动拼接全部卷
```

卷不完整会报缺哪一片。手工合并时必须用 `cat fs.7z.??? > fs.7z`，用 `.0*` 只能匹配到第 99 片。

---

## 5. titanpack.py —— 打包与回灌

### 何时用

- 改完文件，要生成一个能直接导入虚拟机的 ROM 包；
- 只想把某个文件塞进镜像、不重新打包（`--apply-only`）；
- 打包前体检（`--check-only`）。

### 命令

```bash
# 最常用：解包目录 → 可导入包
python3 titanpack.py out -o fs.7z --strict

# 只改了 by-partition 里的文件，自动同步回镜像再打包
python3 titanpack.py out -o fs.7z --adopt-unpacked --strict

# 用覆盖层新增/替换（推荐，干净）
python3 titanpack.py out -o fs.7z --overlay patch --strict

# 只回灌，不打包
python3 titanpack.py out --overlay patch --apply-only

# 打包前体检 / 环境自检
python3 titanpack.py out --check-only
python3 titanpack.py --doctor
```

### 参数

| 参数 | 含义 |
|---|---|
| `src` | `out/`（含 `raw/` 的目录）、`out/raw/`、或直接的 fs 根 |
| `-o, --out` | 输出包路径 |
| `-f, --format` | `auto` / `7z` / `zip` / `tar`，默认按 `-o` 后缀推断 |
| `--prefix` | 包内前缀，如 `androidfs_10.0.0`；默认自动推断 |
| `--volume` | 分卷大小，如 `20m`、`4g` |
| `--level` | `0` 仅存储（最快）/ `5` 默认压缩 |
| `--exclude` | 排除路径，逗号分隔（相对 fs 根） |
| `--links` | `skip`（默认跳过）/ `text`（写成 `.link.txt`） |
| `--overlay` | 覆盖层目录，内部按 `system/...` 组织 |
| `--adopt-unpacked [DIR]` | 把 `by-partition/` 改过的文件写回镜像；省略 DIR 自动向上查找 |
| `--manifest` | `manifest.tsv` 路径，默认自动找 |
| `--apply-only` | 只回灌/只收养，不打包 |
| `--dry-run` | 演练，不写盘 |
| `--no-check` | 跳过完整性检查 |
| `--no-fix` | 只报告不自动修记录池 size |
| `--strict` | 检查不通过则中止打包 |
| `--check-only` | 只体检，不打包 |

### 打包前会做什么检查

1. 包内结构：`androidfs.bin`、各分区 `superblock.bin` 是否齐全；
2. 分区代次：读 `BPUS` 里的 `gen`，打印出来（system 4527、vendor 507 这种大数字是正常的，改动后递增）；
3. 记录池一致性：`fscache.bin` 里每个 inode 的 size 与内容文件实际大小比对，不一致会自动修（`--no-fix` 可关闭）；
4. 只读层 CRC32：`readonly.bin` 逐条重算，有一条不符就报错。

### 输出

```
[1/3] 完整性检查
  · ROM 版本 10.0.0 / build 183
  · system: 代次 gen=4527
  ✓ 结构自洽，可直接导入
[2/3] 收集完成：目录 10，文件 65，链接跳过 0，合计 1.7 GB
[3/3] 打包 -> fs.7z（7z）
  就地打包（不复制文件）: cwd=out/raw  arg=androidfs_10.0.0
  完成：65 个文件，1.7 GB，耗时 176.3s
  校验：条目 65（期望 65）✓，解压后 1.7 GB（期望 1.7 GB）✓
  导入说明：IMPORT.txt
  自检：0 个符号链接，无穿透风险
```

包同目录会生成 `IMPORT.txt`（导入步骤说明），镜像含链接时还会生成 `pack-links.tsv`。

### 覆盖层怎么写

```
patch/
└── system/
    └── priv-app/
        └── Settings/
            └── Settings.apk
```

```bash
python3 titanpack.py out -o fs.7z --overlay patch --strict
```

覆盖层只对**普通桶文件**和**记录池里的可变 inode** 生效。目标在 `readonly.bin` 里时会明确拒绝并提示改用 `ropatch.py`。

---

## 6. apkcheck.py —— 核对 APK 是否进包

### 何时用

换了个 APK、重新打包、导入后界面没变。第一件事不是重刷，而是问包里那份到底是不是你改的。

### 命令

```bash
python3 apkcheck.py fs.7z --apk Settings.apk
python3 apkcheck.py fs.7z --apk system/priv-app/Settings/Settings.apk --local 改好的.apk
```

### 参数

| 参数 | 含义 |
|---|---|
| `src` | 打好的包（7z/7z.001/zip/tar）或已解包的目录 |
| `--apk` | 镜像内路径；只写文件名则全盘搜索所有同名 APK |
| `--local` | 你改好的 APK，用于 sha256 比对 |
| `--keep` | 保留临时解压目录 |

### 输出怎么读

工具会给出：命中路径、inode、大小、**来源**（普通桶 / 记录池 / 只读层）、sha256 比对结论、同目录有无 `oat/` 预编译文件、是否存在多份副本。

| 来源 | 含义 | 处理 |
|---|---|---|
| 普通桶文件 | 内容在 `<分区>/<桶>/<inode>` | `--overlay` 或 `--adopt-unpacked` 直接回灌 |
| 记录池 | 变长 inode 存在 `fscache.bin` | 回灌时同步池内 size，`titanpack` 已覆盖 |
| 只读层 | 内联在 `readonly.bin`，带 CRC32 | 回灌无效，必须用 `ropatch.py` |

命中多处说明存在多份副本，系统加载的可能不是你改的那一份。

---

## 7. titangrep.py —— 字符串搜索（穿透 APK）

### 何时用

想确认"我改的那串字到底在不在包里"，或者想知道它物理上在哪个文件里。

### 命令

```bash
python3 titangrep.py fs.7z "存储ZZ"
python3 titangrep.py fs.7z "存储ZZ" --apk Settings
python3 titangrep.py unpacked/raw "存储空间管理器"
python3 titangrep.py fs.7z "存储ZZ" --only '*readonly.bin'
```

### 参数

| 参数 | 含义 |
|---|---|
| `src` | `fs.7z` / 解包目录 / raw 镜像目录 |
| `words` | 要搜的文字，可多个 |
| `--apk` | 只搜路径含该关键字的文件，如 `Settings` |
| `--only` | 只搜包内匹配该通配符的文件，流式解压不落盘 |
| `--max-mb` | APK 内部单个成员读取上限，`0` 为不限 |

### 能力与边界

- 自动按 UTF-8 / UTF-16LE / UTF-16BE / GBK 四种编码匹配，中文不会漏；
- 遇到 APK 会递归进 `resources.arsc`、`res/values/strings.xml` 等成员；
- 大文件流式分块搜索（8 MB + 4 KB overlap），1.2 GB 只占几十 MB 内存，跨块的关键字也能命中；
- **搜不到不等于没有**：如果 APK 是 deflate 压缩的独立条目（只读层里的那种），明文搜索看不见。这时改用 `ropatch.py apks`。

0 命中时工具会直接提示下一步该用哪个命令。

---

## 8. ropatch.py —— 改写只读层

### 何时用

`readonly.bin` 体积接近整个分区（比如 1.2 GB），说明这个分区的文件系统整个内联在里面，没有独立的桶文件可改。此时 `titanpack --overlay` 完全无效，只能直接改 ATIT 容器。

### 命令

```bash
python3 ropatch.py fs.7z info                                   # 列出所有只读层
python3 ropatch.py fs.7z info --part system                     # 单个层的头部与统计
python3 ropatch.py fs.7z apks --part system --pkg com.android.settings   # 找 APK 条目
python3 ropatch.py fs.7z list --part system --min-size 10M      # 按体积列条目
python3 ropatch.py fs.7z find --part system "some-text"         # 明文搜索
python3 ropatch.py fs.7z extract --part system 123 -o old.apk   # 导出某条目
python3 ropatch.py fs.7z replace --part system 123 改好的.apk --yes
python3 ropatch.py fs.7z verify --part system                   # 全量 CRC 复验
```

### 子命令

| 子命令 | 作用 | 关键参数 |
|---|---|---|
| `info` | 头部、条目数、数据区、尾部空闲 | `--part` |
| `list` | 按大小列条目 | `--min-size` `--kind` |
| `find` | 明文子串搜索，定位所属条目 | `needle` |
| `apks` | 找 zip 容器并解析包名 | `--pkg` `--min-size` `--max-probe` `--limit` |
| `extract` | 导出条目（顺带校 CRC） | `-o` |
| `replace` | 替换条目内容 | `--yes` `--backup` `--align` |
| `verify` | 全量 CRC 校验 | `--limit` |

### 公共开关

| 开关 | 含义 |
|---|---|
| `--part` | 指定分区（ROM 模式必需，除非只有一个只读层） |
| `--fresh` | 丢弃解压缓存，重新从 7z 解出 |
| `--keep` | 保留临时目录便于排查 |
| `--no-writeback` | 只改临时副本，不写回 7z |

### 定位条目：优先 `apks`，不要用 `find`

APK 内部 deflate 压缩，包名和界面文案在只读层里**不以明文存在**，`find` 必然 0 命中。`apks` 走另一条路：检查条目开头是不是 `PK`，是就当 zip 打开，只读中央目录和 `AndroidManifest.xml` 的字符串池取包名。

```
$ python3 ropatch.py fs.7z apks --part system --pkg settings
扫描 61 个候选（>= 1.0 MB，单个体积上限 128.0 MB）…
[结果] 1 个匹配：

条目     大小         成员     包名
1234   38.4 MB    2113   com.android.settings
```

两者都不行时，用 `list --min-size 10M` 按体积列出来，逐个 `extract` 打开看。

### 替换的两条路径

| 新文件 vs 原文件 | 策略 | 影响 |
|---|---|---|
| **≤** 原文件 | 原位覆写 + 尾部补零 | 文件总大小不变，最安全 |
| **>** 原文件 | 追加到文件末尾，xattr 块一起搬 | 文件变大，原位置成为空洞 |

追加要同步三个字段：条目 `size`/`data_off`/`crc32`，以及头部 `0x0C`（已用末尾）、`0x10`（最后数据偏移）。漏一个，引擎会读到旧长度或 CRC 失败。

### ROM 模式的缓存

首次操作会把该分区的 `readonly.bin` 解到 `/tmp/ropatch_cache/<hash>/`（hash = 路径+大小+mtime），后续命令直接复用，改完用 `7z u` 写回。写回成功后缓存自动删除。1.2 GB 的层大概解一次几分钟，之后每条命令都是秒级。

改完 `verify` 通过即可直接导入，**不需要再跑 `titanpack`**。

---

## 9. TitanFS格式分析与还原报告.md —— 参考文档

不是工具，是逆向结论：四种魔数的字段表（`SFDA` / `BPUS` / `ATEM` / `ATIT`）、只读层与记录池布局、`7.1–7.10` 各工具的原理与排障记录。

什么时候翻它：

- 想搞清 `gen=4527` 是什么、为什么改动后会变；
- 要判断某个文件在哪个层（桶 / 记录池 / 只读层）；
- 遇到 `ANDROIDFS_CORRUPTED`、导入失败这类错误，查第 7 节的排障表；
- 需要自己写工具，想直接拿结构表。

---

## 10. 三条完整工作流

### A. 只想解包看看

```bash
python3 titanunpack.py fs.7z -o out --list      # 只看路径树，不写文件
python3 titanunpack.py fs.7z -o out             # 再真解
ls out/by-partition/                            # 按分区浏览
```

只想看包内有哪些文件、不想等解压，直接用 `7z l fs.7z` 更快。

### B. 改普通文件（配置、XML、小的二进制）

```bash
# 1. 解包
python3 titanunpack.py fs.7z -o out

# 2. 走覆盖层，别直接改 by-partition
mkdir -p patch/system/etc
cp 改好的 build.prop  patch/system/etc/build.prop

# 3. 回灌并打包
python3 titanpack.py out -o fs.7z --overlay patch --strict

# 4. 复核
python3 titangrep.py fs.7z "你改的那串字"
```

已经改了 `out/by-partition/` 里那份也不要紧：

```bash
python3 titanpack.py out -o fs.7z --adopt-unpacked --strict
```

它会逐个比对导出副本与镜像内 inode（大小 / mtime / sha256），命中的写回并同步 `.metadata` 与记录池 size。

### C. 换系统 APK（只读层）

```bash
# 1. 先确认它在不在包里、在哪个层
python3 apkcheck.py fs.7z --apk Settings.apk --local 改好的.apk

# 2. 在只读层里 → 按包名找条目号
python3 ropatch.py fs.7z apks --part system --pkg com.android.settings

# 3. 导出确认（unzip -l 能看到 AndroidManifest.xml 就对了）
python3 ropatch.py fs.7z extract --part system 1234 -o old.apk

# 4. 替换（新 apk 不大于旧 apk 时走原位覆写，最安全）
python3 ropatch.py fs.7z replace --part system 1234 改好的.apk --yes

# 5. 全量校验，通过即导入
python3 ropatch.py fs.7z verify --part system
```

---

## 11. 常见错误对照

| 现象 | 原因 | 处理 |
|---|---|---|
| `[警告] by-partition 里有比镜像新的文件` | 改的是导出副本，打包读 `raw/` | 加 `--adopt-unpacked`，或改用 `--overlay` |
| 打包成功，导入后文件还是旧的 | 目标在 `readonly.bin` 里，回灌被拒 | `ropatch.py` |
| `find` / `titangrep` 0 命中 | 目标是压缩 APK，明文搜不到 | `ropatch.py apks --pkg ...` |
| `error: externally-managed-environment` | 系统 Python 禁 pip | 装系统 7z，或用 `python3 -m venv` |
| 分卷解包报缺片 | `cat fs.7z.0*` 只到第 99 片 | 改 `cat fs.7z.??? > fs.7z` |
| `ANDROIDFS_CORRUPTED` | 只读层 CRC 不符或 size 未同步 | `ropatch.py verify` 定位，重做替换 |
| 包里多出一层 `androidfs_10.0.0/` 或少一层 | 前缀判断错 | 用 `--prefix` 显式指定 |
| 导入后 APK 打不开 | 记录池 size 还是旧值 | `titanpack` 不加 `--no-fix` 重打 |

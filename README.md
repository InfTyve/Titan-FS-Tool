# Titan FS 光速虚拟机解包工具集 · 使用手册

六个脚本 + 一份逆向报告，全部单文件、无第三方依赖（`titanpack.py` 可选 `py7zr`，但优先用系统 `7z`），Python 3.8+ 即可运行。

本手册对应版本：`ropatch.py` 2026.11.11-rev9、`titanunpack.py` 2026.11.03、`apksync.py` 2026.11.08。三者迭代最快，命令有出入时以 `--help` 为准。查版本：`python3 ropatch.py --version`。

本次修订（rev9）：写回保留镜像头部标识字段；`info` 增加条目表与数据区自检；条目数改为按数据起点反推；多分区默认选 `system`；`digest --expect` 支持直接给摘要值。变更明细见文末第 13 节。

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
| `apksync.py` | 批量按包名替换 APK（一次处理一文件夹） | 是 | `fs.7z` + APK 目录 |
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
| 给 ROM 预置新应用，放到 `/system/app`（一个或一整批） | `ropatch.py add --mkdir` / `--from <目录>`（见 8 节） |
| 新增后应用「不见了」/ 想在导入前确认写得对不对 | `ropatch.py check --path` → 必要时 `sortdir`（见 8 节） |
| 替换 `framework.jar` / `services.jar` 这类系统 jar | `ropatch.py jars` → `ropatch.py replace --path`（见 8 节） |
| 一次换十几个 APK，不想逐个查条目号 | `apksync.py`（见 9 节） |
| 只想看某个目录里有什么 | `ropatch.py tree` / `ropatch.py path /system/app` |
| 确认改动到底进没进包 | `ropatch.py digest --expect`（最直接）/ `apkcheck.py` / `titangrep.py` |
| `check` 显示的包名和你的 APK 对不上 | 升级 `ropatch.py` 后重跑 `check`（包名解析修订，见 13 节） |
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
| `--fresh` | 丢弃 `raw/`、`by-partition/`、`merged/`，强制重新解压 |

### raw/ 会被复用，注意别看成旧数据

`raw/` 里存着整个镜像（system 分区 1.2 GB）。为了省时间，默认不重复解压，但会用 `.archive.stamp` 记住归档的**大小 + 修改时间**：

- 归档没动 → `复用已有 raw/（归档未变）`
- 归档改过（`ropatch add/replace` 写回后 size 必然变）→ `归档已变化，丢弃旧 raw/ 重新解压`
- 想无条件重来 → `--fresh`

早期版本只看 `raw/` 是否非空就直接复用，于是**改完包再解包，看到的还是改动前的内容**——这是「明明加进去了，解包却没有」最常见的原因，和镜像本身无关。判断依据很简单：解包日志第 1 行的提示。看到「复用」而你又刚改过包，就加 `--fresh`。

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
python3 ropatch.py fs.7z tree --part system / --depth 2         # 看目录树
python3 ropatch.py fs.7z path --part system /system/app         # 路径 -> 条目号
python3 ropatch.py fs.7z apks --part system --pkg com.android.settings   # 找 APK 条目
python3 ropatch.py fs.7z jars --part system --name framework     # 找 jar 的完整路径
python3 ropatch.py fs.7z list --part system --min-size 10M      # 按体积列条目
python3 ropatch.py fs.7z find --part system "some-text"         # 明文搜索
python3 ropatch.py fs.7z show --part system 123                 # 看条目全貌（xattr、子项）
python3 ropatch.py fs.7z extract --part system 123 -o old.apk   # 导出某条目
python3 ropatch.py fs.7z extract --part system --path /framework/framework.jar -o old.jar
python3 ropatch.py fs.7z add --part system /system/app NewApp.apk ./NewApp.apk --yes      # 平铺放进 /system/app
python3 ropatch.py fs.7z add --part system /system/app --from ./新应用 --mkdir --yes      # 整批加
python3 ropatch.py fs.7z replace --part system 123 改好的.apk --yes
python3 ropatch.py fs.7z replace --part system --path /framework/framework.jar 新.jar --yes --drop-oat
python3 ropatch.py fs.7z verify --part system                   # 全量 CRC 复验
python3 ropatch.py fs.7z digest --part system --path /framework/framework.jar --expect 新.jar  # 改动到底进没进包
python3 ropatch.py fs.7z check --part system --path /system/app/YourApp/YourApp.apk      # 导入前体检
python3 ropatch.py fs.7z sortdir --part system --path /system/app --yes                   # 修复目录项顺序
python3 ropatch.py fs.7z ovl --part system --dir /system/priv-app                        # 覆盖层现状
python3 ropatch.py fs.7z install --part system --dir /system/priv-app SFM.apk --yes      # 装进 /system/priv-app（不动只读层）
python3 ropatch.py fs.7z update --part system --dir /system/priv-app --name SFM 新版.apk --yes   # 换内容（只动一个文件）
python3 ropatch.py fs.7z restore --part system --dir /system/priv-app --name SFM --yes            # 换错了撤回
python3 ropatch.py androidfs_10.0.0 uninstall --dir /system/priv-app SFM --yes           # 从覆盖层摘掉
```

### 子命令

| 子命令 | 作用 | 关键参数 |
|---|---|---|
| `info` | 头部、条目数、数据区、尾部空闲 | `--part` |
| `list` | 按大小列条目 | `--min-size` `--kind` |
| `tree` | 打印目录树（逐层解析目录项） | `--depth` `--limit` |
| `path` | 把 `/system/app` 解析成条目号，并列出子项 | `path` |
| `find` | 明文子串搜索，定位所属条目 | `needle` |
| `apks` | 找 zip 容器并解析包名 | `--pkg` `--min-size` `--max-probe` `--limit` |
| `jars` | 遍历目录树，列所有 `.jar` 的完整路径 + dex 个数/版本 | `--name` `--min-size` `--max-probe` `--limit` |
| `show` | 单条目全貌：权限、时间、xattr 十六进制、目录子项 | `--limit` `--xattr-bytes` |
| `extract` | 导出条目（顺带校 CRC） | `-o`，或 `--path` 代替条目号 |
| `add` | 在目录下**新增**文件条目，支持批量（重写整个容器） | `dir` 可写条目号或路径；`--from <目录>` `--mkdir` `--yes` `--backup` `--xattr-from` `--sort` `--append` |
| `replace` | 替换条目内容（`.jar` 目标额外做 dex/AOT 检查） | `--path` `--yes` `--backup` `--align` `--drop-oat` `--keep-zip` `--force` |
| `verify` | 全量 CRC 校验 | `--limit` |
| `digest` | 算镜像内某文件的内容指纹，可与本地文件或摘要值比对 | `--path`（必填）`--expect`（文件路径 / 64 位 sha256 / 8 位 crc） |
| `check` | 导入前体检：路径、目录项、排序、内容、缓存逐项核对 | `--path`（必填）`--no-cache` |
| `sortdir` | 把目录子项按名字重排（补救旧版 append 的顺序错乱） | `--path`（目录）`--yes` |
| `ovl` | 看覆盖层：哪些 inode 被接管、某目录是否被接管、差在哪 | `--dir`（可选，如 `/system/priv-app`） |
| `install` | 把 APK 装进只读分区，**只写覆盖层** | `--dir`（默认 `/system/priv-app`）`--name` `--mode` `--force` `--yes` |
| `update` | 换已有应用的 apk 内容，目录表/水位/只读层全不动 | `--dir` `--name`（必填）`--apk-name` `--mode` `--yes` |
| `restore` | 撤掉覆盖层里的 apk，恢复只读层原内容 | `--dir` `--name`（必填）`--apk-name` `--yes` |
| `uninstall` | 从覆盖层摘掉一个应用 | `--dir` `--yes`（只能对解包目录用） |

### 公共开关

| 开关 | 含义 |
|---|---|
| `--part` | 指定分区（ROM 模式必需，除非只有一个只读层） |
| `--member` | 直接指定包内成员全路径，包里有多个同名 `readonly.bin` 时用它锁定 |
| `--fresh` | 丢弃解压缓存，重新从 7z 解出 |
| `--keep` | 保留临时目录便于排查 |
| `--no-writeback` | 只改临时副本，不写回 7z |

`--member` 什么时候用：包里同时有两份 `readonly.bin`（主目录 + `backup/`、`system.bak/` 这类副本）时，工具会按「路径短、不像备份」排序，把最像主目录的排第一并打印全部候选。若排序结果不对，用 `--member` 写全路径锁定：

```bash
python3 ropatch.py fs.7z info --member androidfs_10.0.0/system/readonly.bin
```

只更新被锁定的那一个成员，其余副本保持原样——**这既是保险也是陷阱**：如果你改的是 `system.bak` 里那份，导入后当然不生效。

缓存一致性：本地缓存的 `readonly.bin` 与包内成员大小不符时（例如上次写回前留下的旧副本），工具会自动重解，不会拿着历史快照继续改。

### 定位条目：优先 `apks`，不要用 `find`

APK 内部 deflate 压缩，包名和界面文案在只读层里**不以明文存在**，`find` 必然 0 命中。`apks` 走另一条路：检查条目开头是不是 `PK`，是就当 zip 打开，只读中央目录，并解析 `AndroidManifest.xml` 根节点 `<manifest package>` 取包名（rev2 起不再从字符串池里猜）。

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

> `原数据留在 0x1b70a000（空洞，不再被引用）` 这行只说明旧数据没被回收，**不代表写入成功**。写入是否真的落到容器里，看紧接着的 `回读校验`（工具内部）和 `digest`（独立验证）。容器在 7z 里的话，还要看写回时那行 `✓ 已更新 xxx（… crc …）`——它现在会把包内成员重新读一遍再比对 CRC，对不上会直接报错而不是静默放过。

### ROM 模式的缓存

首次操作会把该分区的 `readonly.bin` 解到 `/tmp/ropatch_cache/<hash>/`（hash = 路径+大小+mtime），后续命令直接复用，改完用 `7z u` 写回。写回成功后缓存自动删除。1.2 GB 的层大概解一次几分钟，之后每条命令都是秒级。

改完 `verify` 通过即可直接导入，**不需要再跑 `titanpack`**。

### 新增文件：`add`（把应用放进 `/system/app`）

替换 = 改内容，只动一个条目的字节；**新增 = 改结构**，要同时动三处：条目表 +1、父目录的目录项数组 +1、数据区后移。这些都在一个事务里做完，**一次只重写一遍容器**。

目录参数可以**直接写路径**，不用先去 manifest 里查 inode；目录不存在时加 `--mkdir` 自动建（支持多级）。

```bash
# 1. 看有哪些目录（不确定就往这一层看一眼）
python3 ropatch.py fs.7z tree --part system / --depth 2

# 2. 加一个应用（目录不存在会自动建 /system/app/NewApp）
python3 ropatch.py fs.7z add --part system /system/app/NewApp 新应用.apk ./新应用.apk --mkdir

# 3. 执行
python3 ropatch.py fs.7z add --part system /system/app/NewApp 新应用.apk ./新应用.apk --mkdir --yes

# 4. 一次加一个文件夹（十几个应用就这一条命令）
python3 ropatch.py fs.7z add --part system /system/app --from ./新应用 --mkdir --yes

# 5. 体检 + 复验（check 会逐项核对路径、目录项、排序、内容）
python3 ropatch.py fs.7z check --part system --path /system/app/NewApp/新应用.apk
python3 ropatch.py fs.7z tree --part system /app --fresh --depth 2
python3 ropatch.py fs.7z verify --part system

# 6. 交叉验证（可选但强烈建议）：换一个独立实现再读一遍目录树
python3 titanunpack.py fs.7z -o /tmp/chk --list
```

第 6 步是关键：`titanunpack` 走的是 `titanfs.py` 的解析器，和 `ropatch` 完全独立。两边读出来的目录树一致，才说明目录项写对了——这是目前唯一能做的交叉验证，最终裁判仍是 Titan 引擎本身。

`path` / `add` 会自动处理"只读层的根就是分区根"：`/system/app` 在镜像里其实是 `/app`，两种写法都认。解析失败时会列出该层现有子项，方便对照。

演练输出（样本实测）：

```
目标目录 : 新建 /app/NewApp
  + 条目 7     NewApp.apk                                   6 B
新增     : 文件 1 个、目录 2 个
改写     : 目录项 1 个目录
条目表   : 6 -> 9 项（数据区整体后移 156 B）
容器     : 5.7 KB -> 6.0 KB（+242 B）
xattr    : 复制各自父目录（security.selinux）
```

新条目序号从原来的条目总数往上连续分配（目录先分、文件后分）。xattr 默认复制父目录的（`security.selinux=u:object_r:system_file:s0`），也可用 `--xattr-from <条目号>` 指定；文件权限 `0644`（源文件带可执行位则 `0755`），owner 沿用父目录。

#### 目录项必须按名字有序（新增应用「凭空消失」的真正原因）

只读层的目录项是一个**按名字升序排列的数组**，真实 ROM 无一例外：

```
/ ------------------------------------------------------------------
  apex  debug_ramdisk  dev  fstab.${ro.hardware}  init  mnt  proc  share  sys
```

引擎按名字查找时用的是有序遍历或二分。**新条目如果追加到末尾，位置是错的，就会被直接跳过**——写进去的字节全对、`verify` 的 CRC 全对、`tree` 也能列出来（`tree` 是顺序遍历，不挑顺序），但开机后应用列表里没有它。这就是「不是变成浏览器，而是直接没了」。

所以 `add` 现在的行为是：

| 父目录当前状态 | 行为 |
|---|---|
| 原有子项严格升序（绝大多数情况） | 按名字**插到正确位置**，原有项的字节顺序整体顺移 |
| 原有子项本就无序 | 只追加到末尾，并提示该目录无序 |
| 加 `--sort` | 即使原本无序也强制重排 |
| 加 `--append` | 强制只追加（调试用，正常别加） |

`--sort` 与 `--append` 互斥，只能选一个。默认行为已经会「有序则插入」，正常不需要加任何一个；`--append` 只用于确认某个目录的布局是否真的有问题。

写入后自检会按**原布局**重新解析，并检查「原有子项一个不少、inode 未变、写后仍严格升序」，任一项不过就丢弃临时文件。

#### 已经写坏了怎么救：`sortdir`

如果包是用旧版 `add` 写的（顺序错乱），不用重打 1.2 GB，直接把目录项就地排回去：

```bash
# 1. 先体检，确认问题就是排序
python3 ropatch.py fs.7z check --part system --path /system/app/你的应用/你的应用.apk

# 2. 演练：看哪些项要移位
python3 ropatch.py fs.7z sortdir --part system --path /system/app

# 3. 执行
python3 ropatch.py fs.7z sortdir --part system --path /system/app --yes
```

`sortdir` 只改目录数据，**不动 inode、不动文件内容**。重排后总长不变时原位覆写（尾部补零）；变长时追加到容器末尾，原数据成为空洞。写完会重新解析确认全序且子项集合未变。

> `sortdir` 的演练行会告诉你目录数据会变大还是不变（`目录数据 : 20 B -> 20 B`）。变大不必担心，只是多占几十字节、原位置留下空洞，和 `replace` 追加的机制一样。

#### 导入前体检：`check`

`check` 把这个文件「能不能被引擎找到」拆成五项逐条核对：

```
$ python3 ropatch.py fs.7z check --part system --path /system/app/YourApp/YourApp.apk
镜像     : androidfs_10.0.0/system/readonly.bin
路径     : /app/YourApp/YourApp.apk = 条目 1234
属性     : -rw-r--r--  uid=0 gid=0  size=5.1 MB
内容     : ✓ CRC 3f8a12cd 与条目表一致
父目录   : /app（条目 12） 子项 31 个，数据 1180 B/1180 B
           ✓ 目录项 'YourApp' -> 条目 1234
 排序     : ✓ 目录按名字升序，这一项在第 14 位（位置正确）
APK      : ✓ 合法 zip，manifest 3.4 KB，包名 com.example.newapp
缓存     : fscache.bin 3.1 MB，不含该路径
           路径不在缓存里：这只是按路径索引的 dentry 缓存，
           新路径缺失本身不拦目录遍历；仍报错时优先看上面的『排序』行。
           对照：同目录 8 个老条目，缓存里命中 8 个  ← 缓存较完整，但不足以判定新路径会被拦截
```

| 行 | 说明 |
|---|---|
| `内容` | 从容器读字节重算 CRC，对不上说明数据被截断 |
| `父目录 / 排序` | 名字在不在目录项里、指向哪个 inode、位置是否符合升序 |
| `APK` | 是否合法 zip、manifest 大小、包名（读不出说明不是 APK） |
| `缓存` | 该路径在不在 `fscache.bin` 里，并**用同目录老条目做对照**（仅参考，不参与判定） |

缓存那行只是**参考信息**，不参与「结构自洽」判定。`fscache.bin` 是按**路径名**索引的 dentry 缓存（里面是 `/system/bin/adbd` 这类绝对路径），不是 inode 号表，新路径不在其中不会被它「截获」。同目录老条目全部命中只说明这份缓存较完整，不能据此判定新路径会被拦截；真正会导致「应用不见」的是上面的排序行。

> 别和 `data/fscache.bin` 搞混：那个在可写层里，是 inode 记录池（ATIT 头 + 预分配），titanpack 回灌时要同步它的 size（见 5 节）。这里说的是只读分区根目录下那份、按路径索引的 dentry 缓存。

`check` 的包名取自二进制 manifest 的根节点 `<manifest package="…">`，不是从字符串池里猜的——字符串池里常有 `com.android.settings.icon` 这类更短的组件名，按「最短 `com.*`」猜会报错。若显示的不是你的包名，先确认工具版本，再用 `digest` 或「导出后独立解析」核对（第 13 节有现成命令）。

#### 放到哪个目录

| 目录 | 用途 | 额外要求 |
|---|---|---|
| `/system/app` | 普通系统应用 | 无，放进来自动可用 |
| `/system/priv-app` | 需要 `signature\|privileged` 权限的应用 | Android 9+ 还要在 `/system/etc/permissions/` 里放白名单 XML，否则开机报 `PrivappPermissions` 并**拒绝安装** |
| `/system/etc/permissions` | 权限白名单 XML | 放完无需重启编译，开机由 PMS 读取 |
| `/priv-app`（分区根下） | **不是**系统应用目录 | system-as-root 的镜像里它是个空壳，PMS 只扫 `/system/priv-app`，写进去必然「看不见」 |

#### 别把应用放进根下的 `/priv-app`

这是最难自查的一种错：`check` 五项全 ✓、`verify` 0 条不符、`tree` 也看得见，但应用就是不出现。

原因是这类镜像（system 分区根就是 `init.rc`、`default.prop` 那一套）**根下同时存在两个 `priv-app`**：

```
/priv-app              ← 空壳，往往是 0 项，PMS 不扫
/system/priv-app       ← 真正的 51 个系统应用（Settings、PermissionController…）
```

PMS 只扫 `SYSTEM_PARTITIONS` 下的 `<分区>/priv-app`，不扫根下这个空壳。写错位置的现象和「排序错」一模一样，但 `sortdir` 救不了。

自查一条命令：

```bash
python3 ropatch.py fs.7z tree --part system /system/priv-app --limit 5
python3 ropatch.py fs.7z tree --part system /priv-app --limit 5
```

后者若只有你自己加的那一项、前者有几十项，就是加错位置了。正确命令（注意目录要写到应用名那一级）：

```bash
python3 ropatch.py fs.7z add --part system /system/priv-app/SFM SFM.apk ./SFM.apk --mkdir --yes
```

`--mkdir` 会建出 `/system/priv-app/SFM`，APK 落在 `/system/priv-app/SFM/SFM.apk`。写成父目录 `/system/priv-app` 会把 APK 平铺在它下面（PMS 也能扫到，但不是标准结构，部分 ROM 的 privapp 白名单校验会漏掉）。

> 早期版本有个 bug：`--mkdir` 新建目录时无条件剥掉 `system/` 前缀，于是 `/system/priv-app/SFM` 被写成 `/priv-app/SFM`。rev5 起改为「以父目录能否解析为准」，`/system/priv-app` 存在就直接用它。旧包里的错误条目无法就地删除，只能从原始包重做。


`/system/priv-app` 的白名单条目长这样，文件名随意（惯例 `privapp-permissions-<包名>.xml`）：

```xml
<permissions>
    <privapp-permissions package="com.example.newapp">
        <permission name="android.permission.WRITE_SECURE_SETTINGS"/>
    </privapp-permissions>
</permissions>
```

这个 XML 同样用 `add` 加进去（目标是目录 `/system/etc/permissions`），权限就不用管了，工具有默认值。

#### 四条限制

1. **目录要么已存在，要么加 `--mkdir`**。加 `--mkdir` 时会从「已存在的最深一级」开始往下逐级创建（`/system/app/NewApp` 若 `/app` 已有，就只建 `NewApp`）；整条路径必须落在同一个只读层里，跨分区（如 `/system/xxx` 指向 vendor）不会被拼出来。每建一级目录多分配一个条目，`tree` 里能看到。
2. **目录项必须保持按名字升序**。这是「新应用凭空消失」的唯一原因：追加到末尾虽然写得进去、`tree` 也看得见，但引擎按名字查会跳过它。工具现在默认插到正确位置，`--append` 才退回旧行为。写坏的包用 `sortdir` 就地修，不必重打。
3. **`fscache.bin` 不拦新增**。它是按路径名索引的 dentry 缓存，不是 inode 号表；新路径查不到时引擎会回落到目录遍历，`check` 的缓存行只作参考、不参与判定。目前确认的「新应用不出现」全部是排序问题，不是缓存。真正需要绕开元数据时，才用「占位法」（见下）。
4. **写完必须验**。工具在临时文件上重开，按**原布局**逐项比对：原有条目 mode/size/crc 一个不能变、每个被动过的目录子项一一对上、写后仍严格升序、新条目 size/crc 一致。任一项不过就丢弃临时文件，原文件不动。

#### 写坏了是什么样

| 现象 | 原因 | 怎么救 |
|---|---|---|
| 新应用**不出现**，其它都正常 | 目录项顺序不对（旧版追加到末尾），引擎按名字查找跳过它 | `check` 确认 → `sortdir` 就地重排 |
| 新 APK 变成了另一个 APK（体积、内容都不对） | 父目录项被按错误布局整块重排（2026.11.08 之前） | 无局部修复，只能从原始包重做 |
| 同目录下别的应用也打不开了 | 目录项被写坏（旧版 `--sort`） | 无局部修复，从原始包重做 |
| 导入后无法开机 | `/system/app` 目录项严重损坏 | 回滚原始 `fs.7z` |

区分「顺序错」和「数据坏」：`check` 报 `排序 ✗` 而 `父目录 ✓`、`内容 ✓`，就是前者，`sortdir` 能救；如果 `check` 连路径都解析不出来，或 `verify` 报 CRC 不符，才是后者。

所以顺序永远是：`add` 演练 → 执行 → `check` → `verify` → 再导入。

#### 代价

| 项 | 说明 |
|---|---|
| 全量重写 | 条目表在数据区前面，多一条就要把 1.2 GB 数据整体后移 52 字节 |
| 磁盘 | 约 2 倍空间（先写临时文件再原子替换） |
| 耗时 | 与容器体积线性，1.2 GB 约几分钟——所以**一次加多个比加一个多次快得多** |

`add` 支持一次加一整批，只写回一次：

```bash
# 一个文件夹里十几个 apk，全部加进 /system/app
python3 ropatch.py fs.7z add --part system /system/app --from ./新应用 --mkdir --yes
```

`--from <目录>` 的取文件规则（不做递归，只取该层）：

| 文件 | 处理 |
|---|---|
| `*.apk` | 校验前 4 字节是不是 `PK`，不是就跳过（非 zip 的东西放进 `/system` 会让开机扫描卡住） |
| `*.jar` | 直接加，不做 zip 校验（jar 由 `replace` 负责，见下节） |
| 其它后缀 | 忽略，不报错 |
| 目标目录已有同名文件 | 跳过并提示「已存在，改用 replace」，不会静默覆盖 |

批量的好处不只是省时间：所有新条目共用一次条目表重排，不会出现「加到第 7 个时前面几个的偏移算错」这类问题。坏处是一次失败全批回滚——但这正是想要的行为。

加完之后想逐个核对，用 `path` 列一次目录，条目号、名字、大小都在里面：

```bash
python3 ropatch.py fs.7z path --part system /system/app
```

#### 更稳的替代路线

如果 `add` 被自检拒绝、或导入后没反应，换这两条：

- **占位法**（兜底，会牺牲一个原应用）：只在 `add` 被自检拒绝、或目标目录的布局实在推断不出时用。挑一个用不上的系统 APK，用 `replace` 把内容换成你的 APK。系统按 APK 里的包名注册应用，不看文件名，目录结构、inode、记录池零改动，是目前唯一 100% 不碰元数据的做法。代价是少一个原应用——如果那个应用也要保留，别走这条路，先查 `check` 的排序行。
- **让引擎自己建**：在虚拟机里正常安装一次，再把 `fs` 导出来打包。inode、目录项、记录池全部由引擎分配，100% 合法。

#### 判断「到底进没进包」：`digest`

`digest` 直接读容器里那段字节算指纹，**不经过任何解包、导出、合并环节**，所以它不会像「解包后看有没有变」那样被骗：

```bash
python3 ropatch.py fs.7z digest --part system \
      --path /framework/framework.jar --expect 新.jar
```

```
镜像     : androidfs_10.0.0/system/readonly.bin
路径     : /framework/framework.jar = 条目 2
内容     : 4.3 KB  条目记录 crc 3d52e446  size 4365  off 0x1bc
实测     : 4.3 KB  crc 3d52e446
sha256   : 5ca0b86a449ad06fb34aa7c72d977b47d2f70232839d9b723362d0eda50917ea

对比文件 : 新.jar（4365 字节）
           crc 3d52e446  sha256 5ca0b86a449ad06f…
✓ 完全一致：镜像里就是这个文件
```

`--expect` 也可以直接给摘要值，省掉「先存文件再比」这一步（rev9）：

```bash
python3 ropatch.py fs.7z digest --part system \
      --path /system/priv-app/SFM/SFM.apk \
      --expect c066c420d846538d2b693fc6af5cbeff96b0ea633034693145acf39303101c3a
```

```
对比摘要 : sha256 c066c420d846538d2b693fc6af5cbeff96b0ea633034693145acf39303101c3a
✓ 完全一致：镜像里就是这个文件
```

传 64 位十六进制按 sha256 比，传 8 位按 crc32 比，其余才当文件路径。

也可能是这种——字节不同但内容相同，同样算成功：

```
✗ 字节不一致（镜像 4365 字节 / 文件 4370 字节）
  但逻辑内容完全一致：成员、大小、CRC 全部相同
  → 改动确实进包了，差异只是压缩方式/时间戳（写回时按原包重打成了 STORED）
```

四种结论，对应四种处理：

| 最后一行 | 含义 | 该怎么办 |
|---|---|---|
| `✓ 完全一致` | 字节级相同 | 不用管，可以直接导入 |
| `✓ 但逻辑内容完全一致`（跟着一行「成员、大小、CRC 全部相同」） | 写回时按原包形态把 dex 重打成 STORED，zip 头/时间戳变了 | 正常，改动已经在包里，不用重做 |
| `✗ 逻辑内容也不同：… N 个成员内容不同` | 镜像里是另一个版本 | 替换没写回，或写回到了别的成员（查 `--member`） |
| `✗ 字节数相同但内容不同` | 大小碰巧一样，内容是旧的 | 典型「改了但没写回」，重跑 `replace --yes` |

**sha256 对不上不等于失败**。jar/apk 只要被重新打过包（STORED 对齐、时间戳更新），整体 sha256 必然变；只有成员级 CRC 才说明内容。所以判 jar 别用 `sha256sum` 比整包，用 `digest`。

如果 `digest` 说一致、但解包出来还是旧文件，那就是**解包看的是旧目录**（`by-partition/` 里上次导出的副本），不是包的问题。

### 覆盖层：`ovl` / `install`（往只读分区加应用，推荐）

**这一节解决的是「改完进包了、装机却看不到应用」。**

#### 为什么会看不到

引擎读文件不是直接读 `readonly.bin`，而是**先查同目录下的覆盖层**：

```
androidfs_10.0.0/
├── system/
│   ├── readonly.bin          ← 出厂只读层（1.2 GB）
│   └── 00000000/             ← 覆盖层（inode >> 12 分桶）
│       ├── 00000e59          ← /system/priv-app 的目录表（纯文本）
│       └── 00000e59.metadata ← 它的 ATEM 头（权限/时间/xattr）
```

- 覆盖层里**没有**某个目录的目录表 → 引擎读 `readonly.bin` 那份；
- 覆盖层里**有** → 引擎**只**读它，`readonly.bin` 那份**整个被忽略**。

所以 `add` 把新应用写进 `readonly.bin` 是白费的：目录表被覆盖层接管后，只读层里那 52 项根本没人看。**这也是为什么之前"进包了、但应用没出现"**——包是对的，引擎没读那一层。

对第三方改过的包实测（`小山虚拟机 SAN OS 5.5`），它的 `/system/priv-app` 覆盖层是这样：

```
AndroidMigratePrebuilt=e5a
BackupRestoreConfirmation=e5c
…
VpnDialogs=f52              ← 41 行照抄只读层
BlockedNumberProvider=1268  ← 6 行是新加的，inode 从 0x1268 起
CalendarProvider=126d
…
TelephonyProvider=128f
```

而它的 `readonly.bin` 与官方原包**逐条零差异**。也就是说：那位作者一个字节都没改只读层，全靠覆盖层。

#### 目录表的格式（实测，别自己编）

纯文本，一行一项，`名字=<十六进制 inode>`，`\r\n` 结尾：

| 元素 | 规则 |
|---|---|
| 名字 | UTF-8，不能含 `=`、`/`、换行 |
| inode | **小写十六进制、无前导零**：`e5a`、`1268` |
| 行尾 | `\r\n`（`0d 0a`），最后一行也要有 |
| 排序 | 不强制；照抄原顺序、新项追加末尾即可（实测这么干能跑） |
| 文件位置 | `<分区>/<inode>>12 的八位十六进制>/<inode 的八位十六进制>` |

`<inode>.metadata` 是 99 字节起的 ATEM 头，和只读层条目里的 xattr 是同一套内容：

```
+0  "ATEM"     +4  0x30      +8  mode     +12 uid
+16 gid        +20 nlink     +24 mtime/ctime/atime（各 4 字节）
+40 保留 0     +44 xattr 区总长（含下面的条目数字段）  +48 xattr 个数
+52 xattr：名字\0 + u32 长度 + 值（含结尾 \0）
```

`security.selinux` 必须是 `u:object_r:system_file:s0`。第三方包里有把 `priv-app` 应用写成 `u:r:untrusted_app:s0` 的（直接 `cp` 了 `/data/app` 的目录），那种在 SELinux 强制模式下会被拒；本工具按系统应用的规范写。

#### `superblock.bin` 里的 inode 水位

分区目录下还有一个 16 字节的 `superblock.bin`：

```
42 50 55 53  "BPUS"
10 00 00 00  头长 16
a6 11 00 00  ← inode 水位（下一个可分配的 inode）
00 00 00 00
```

实测对照：

| 包 | 最大已用 inode | `superblock` 值 |
|---|---|---|
| 原包（只读层 4518 条） | `0x11a5` | `0x11a6` |
| `SAN OS 5.5`（覆盖层用到 `0x12a5`） | `0x12a5` | `0x12a6` |

所以它是**下一个可分配的 inode**，不是条目数。`install` 会自动把它推到「新 inode 的最大值 + 1」，**只增不减**——摘掉应用时刻意不往回降，降水位会让引擎认为后面那些 inode 是空的。

这也是 `add` 那条路的一个隐性坑：直接改 `readonly.bin` 加条目，`superblock` 不会跟着动，引擎可能认为新 inode 不存在。`install` 不存在这个问题。


#### 命令

```bash
# 看现状：哪些 inode 被接管、priv-app 有没有被覆盖、差在哪
python3 ropatch.py fs.7z ovl --part system --dir /system/priv-app
python3 ropatch.py androidfs_10.0.0 ovl --dir /system/priv-app

# 演练（不加 --yes 只看计划）
python3 ropatch.py fs.7z install --part system --dir /system/priv-app SFM.apk

# 真的写
python3 ropatch.py fs.7z install --part system --dir /system/priv-app SFM.apk --yes

# 换个目录名、改 apk 权限
python3 ropatch.py fs.7z install --part system --dir /system/priv-app SFM.apk \
      --name SFM --mode 644 --yes

# 已经装过同名，要换掉里面的 apk
python3 ropatch.py fs.7z install --part system --dir /system/priv-app 新版.apk \
      --name SFM --force --yes

# 摘掉（目录表恢复成只读层原样）
python3 ropatch.py androidfs_10.0.0 uninstall --dir /system/priv-app SFM --yes
```

`image` 位置可以给三种东西，工具自己认：

| 给什么 | 例子 |
|---|---|
| fs 压缩包 | `fs.7z`（内部解出来改，再 `7z u` 写回） |
| 解包出来的目录 | `out/raw/androidfs_10.0.0`、或它的上级 `out/raw`、或 `out` |
| 某个分区的目录 | `out/raw/androidfs_10.0.0/system` |

#### 输出长什么样

```
目标目录 : 路径 /system/priv-app = 条目 3673 (0xe59)，现有 52 项，来自 只读层

新增条目 :
  SFM/                         inode 0x11a7  目录 0755
  SFM/SFM.apk                  inode 0x11a8  5.0 MB  com.android.ty.systemservices
  覆盖层目录表 : …/androidfs_10.0.0/system/00000000/00000e59
                …/androidfs_10.0.0/system/00000001/000011a7
  新 inode 起点 : 0x11a7（只读层 4519 条已占 0..0x11a6）

✓ 已写入覆盖层
  …/system/00000000/00000e59  1125 字节（53 行）
  …/system/00000001/000011a7  23 字节（1 行）
  …/system/00000001/000011a8  5.0 MB

readonly.bin 未改动，CRC 与 used_end 都保持原样。
```

**四个数可以拿去设备上对号**：

| 输出里的值 | 设备上怎么看 |
|---|---|
| `条目 3673 (0xe59)` | `ls -di /system/priv-app` 的 inode |
| `inode 0x11a7` | `ls -di /system/priv-app/SFM` |
| `inode 0x11a8` | `ls -di /system/priv-app/SFM/SFM.apk` |
| `53 行` | `wc -l /system/priv-app` 的父目录项数 |

新 inode 的分配规则：`max(只读层条数, 覆盖层里已有的最大 inode) + 1`。原包 `count = 4519` 就从 `0x11a7` 起，不会撞到任何原有 inode。

#### `install` 和 `add` 怎么选

| | `install`（覆盖层） | `add`（改只读层） |
|---|---|---|
| 动 `readonly.bin` | **不动**，CRC / `used_end` / 头部全不变 | 追加数据、改条目表、改 `count` |
| 写多少 | 几十 KB（几张目录表 + APK） | APK 大小 + 目录数据，1.2 GB 文件整体重写 |
| 失败风险 | 低，删掉那几个文件就回到原状 | 改坏 `count` / 排序会让整个分区读不出来 |
| 引擎认不认 | 认（这是引擎自己的机制） | 只在**没有**覆盖层时认 |
| 适合 | 往 `/system/priv-app`、`/system/app` 加新应用 | 分区本来就没有覆盖层的纯净包 |

**有覆盖层的包，一律用 `install`。** 判断方法：`ls androidfs_10.0.0/system/` 下有没有 `0000000N` 这种八位十六进制目录。

#### 边界

1. 只处理「新增一个应用目录」这一个场景。要改已有应用的文件内容，用 `replace`（那条路走的是条目数据，不受覆盖层影响）。
2. `uninstall` 只能对**解包出来的目录**用。`7z` 里删成员要重建整个包，工具不做。
3. 覆盖层里的目录表是**整份替换**，不是合并。所以 `install` 每次都把父目录的全部 52 项 + 新项一起重写——这也是它必须先把原表读全的原因。
4. 打包回 `7z` 时压缩参数会丢（`7z u` 用默认值），包体积可能变大，功能不受影响。

### 更新已装好的应用：`update` / `restore`

换一个已经在系统里的 APK（覆盖层装的、只读层自带的都行），**不用碰目录表**。

#### 为什么只换一个文件就够

还是那个第三方包的证据：

| 覆盖层文件 | 大小 | 指向的路径（只读层） |
|---|---|---|
| `system/00000000/00000f09` | 178,900,032 B | `/system/priv-app/PrebuiltGmsCore/PrebuiltGmsCore.apk` |
| `system/00000000/00000f94` | 134,450,148 B | `/system/product/app/webview/webview.apk` |

只读层里 inode `0xf09` / `0xf94` 这两条是 **`size=0` 的空占位条目**，而覆盖层里同名文件是 178 MB / 134 MB 的真货。覆盖层里**没有**对应的目录表——说明引擎解析到目录项拿到 inode 后，会先看覆盖层里有没有 `<inode>`，有就直接用它的内容。

所以更新 = 覆盖一个文件。目录表、目录项顺序、`superblock` 水位、`readonly.bin` 全部不动，**没有任何结构性风险**。

#### 命令

```bash
# 看会换成什么（不加 --yes 只演练）
python3 ropatch.py fs.7z update --dir /system/priv-app --name SFM 新版SFM.apk

# 真的换
python3 ropatch.py fs.7z update --dir /system/priv-app --name SFM 新版SFM.apk --yes

# 目录里有多个 apk 时指定换哪个
python3 ropatch.py fs.7z update --dir /system/priv-app --name SFM 新版.apk --apk-name SFM.apk --yes

# 换错了，恢复成原版（只删覆盖层这一个文件）
python3 ropatch.py fs.7z restore --dir /system/priv-app --name SFM --yes
```

`--name` 是应用**目录名**，不是包名。不确定叫什么就先跑：

```bash
python3 ropatch.py fs.7z ovl --part system --dir /system/priv-app
```

#### 输出长什么样

```
更新目标 : 路径 /system/priv-app/SFM  (目录 inode 0x11a7)
  SFM.apk                      inode 0x11a8  <- 原地换内容
  旧  : 5.0 MB    crc 6280dcf6
  新  : 6.2 MB    crc 1a2b3c4d  com.android.ty.systemservices
  目录表 / 水位 / readonly.bin 全部不动

✓ 已更新 …/system/00000001/000011a8
  6.2 MB / crc 1a2b3c4d，内容与源文件逐字节一致
  …/system/00000001/000011a8.metadata（100644 0:0）
```

换只读层自带的系统应用也一样，只是 inode 落在 `00000000` 桶：

```
更新目标 : 路径 /system/priv-app/DocumentsUI  (目录 inode 0xe86)
  DocumentsUI.apk              inode 0xe87  <- 原地换内容
  旧  : 4.8 MB    crc 66f8c471
  新  : 5.1 MB    crc 9ec743a3  com.android.documentsui
```

#### 权限、属主、时间

- `mode` 默认沿用原文件（`update` 不会把 `0644` 改成别的），要改加 `--mode 600`；
- `uid/gid` 沿用原文件（系统应用都是 `0:0`，但 `system/app` 里可能是 `1000`）；
- `mtime` 取新文件的修改时间。

#### 什么时候 `restore` 不给用

`restore` 只删覆盖层里那**一个 apk 文件**，前提是只读层里有同名条目兜底。

如果是 `install` 装进去的新应用（只读层里压根没有这个 inode，`size=0`），删文件会让目录表指向一个不存在的 inode，应用直接坏掉。这种情况工具会拦住，提示改用 `uninstall`：

```
[提示] SFM.apk 在只读层里是空的，说明它是 install 装进去的
       只删文件会让目录表指向不存在的 inode，应用会坏
       要整个摘掉：uninstall --dir /system/priv-app --name SFM --yes
```

#### 三种操作的分界

| 想做的事 | 命令 | 动哪些文件 |
|---|---|---|
| 加一个原来没有的应用 | `install` | 父目录表 + 应用目录表 + 2 个 metadata + apk + 水位 |
| 换已有应用的内容 | `update` | **只有** apk + 它的 metadata |
| 换错了要撤回 | `restore` | 删掉 apk + metadata |
| 整个摘掉（含新装的） | `uninstall` | 父目录表改回 + 删应用目录下所有 inode |

**`update` 是这四个里最轻的**：不管原来那个 apk 是 5 MB 还是 200 MB，都只重写一个文件，几秒钟完事。

#### 换完之后应用崩溃怎么办

换 apk 会让同目录 `oat/<abi>/` 下的旧 `.odex/.vdex` 全部失效。正常情况下 ART 会校验 apk 的 checksum，不匹配就忽略旧产物、重新编译到 `/data`。

如果开机后这个应用直接闪退、`logcat` 里有 `dex2oat` 或 `oat file` 相关报错，说明这套引擎没做校验，还拿着旧 odex 在用。处理办法是把那个应用目录整个删掉重装（`uninstall` 再 `install`），或者干脆换一个本来就带正确 oat 的包。



### 替换 jar（framework.jar / services.jar）

jar 在系统分区里就是普通文件，`replace` 的写入机制和换 APK 完全一样（原位覆写或追加，xattr 保留）。区别在于 **jar 写错不会报错，只会开不了机**，所以工具对 `.jar` 目标多做四件事：

1. 校验新文件是合法 zip（jar 本质就是 zip）；
2. 读 dex 头，比较新旧 **dex 版本号**（新版本高于原版本必然失败）；
3. 比较 dex 的 **压缩方式**，原包是 STORED 而新包是 DEFLATED 时自动按原包形态重打包；
4. 找同目录和 `oat/<abi>/` 下的 **配套 AOT 产物**（`.odex/.vdex/.oat/.art`）并提示，`--drop-oat` 可一并作废。

```bash
# 1. 定位：列出所有 jar（带完整路径和 dex 版本）
python3 ropatch.py fs.7z jars --part system --name framework

# 2. 导出原 jar，先看清楚它长什么样
python3 ropatch.py fs.7z extract --part system --path /framework/framework.jar -o old.jar
unzip -l old.jar | head

# 3. 演练：只做检查，不写盘
python3 ropatch.py fs.7z replace --part system \
      --path /framework/framework.jar 新framework.jar

# 4. 执行（必要时加 --drop-oat 作废旧 AOT 产物）
python3 ropatch.py fs.7z replace --part system \
      --path /framework/framework.jar 新framework.jar --yes --drop-oat

# 5. 复验
python3 ropatch.py fs.7z verify --part system
```

演练输出（样本实测）：

```
解析目标 : /framework/framework.jar = 条目 2
目标     : 条目 2  /framework/framework.jar  4.3 KB
新文件   : /tmp/new1.jar  6.4 KB

[JAR 检查]
原 jar  4.3 KB      成员 2     dex 1 个，版本 035，STORED
新 jar  6.4 KB      成员 3     dex 2 个，版本 035，混合
[提示] 比原 jar 多 1 个成员：classes2.dex
[重打包] 原包 dex 是 STORED，新包是 DEFLATED -> 已按原包形态改成 STORED

[AOT] 找到 1 个配套产物（都是按旧 jar 编译的）：
       /framework/oat/arm64/framework.odex    1008 B    条目 5     子目录
```

#### dex 版本对照（新 jar 不能高于这个）

| dex 版本 | Android |
|---|---|
| 035 | 5.0 – 7.1 |
| 037 | 8.0 – 8.1 |
| 038 | 9 |
| 039 | 10 – 13 |
| 040 | 14 – 15 |

拿不准就直接用原 jar 的版本：工具会把两个版本都打出来，`新 dex 版本 039 高于原 dex 035` 这种 Warning 出现时不要写盘。

#### 为什么还要管 AOT

`framework.jar` 不是单独生效的，它旁边还有一套按**旧 dex 的校验和**预编译的产物：

| 文件 | 位置 | 作用 |
|---|---|---|
| `framework.odex` / `.vdex` | `/framework/oat/<abi>/` | 该 jar 的 AOT 结果与 dex 缓存 |
| `boot.art` / `boot.oat` | `/framework/<abi>/` | boot 镜像，内嵌 framework 的类 |

换了 jar 之后这些产物的校验和对不上，ART 会忽略它们、退到解释执行并在首次开机重新编译。多数 ROM 能自愈（慢几分钟），锁死 boot 镜像的 ROM 会卡开机。

- `--drop-oat`：把同名的 `.odex/.vdex/.oat/.art` 置零（不是删除），强制 ART 重新编译；**不碰 `boot.art`/`boot.oat`**。
- boot 镜像仍在时，工具会单独提示。真卡开机的话，需要连 `/framework/<abi>/boot.art`、`boot.oat` 一起换（这两个得从目标 ROM 的原版取，或整目录替换）。

#### 四条硬规则

1. **jar 里必须有 `classes.dex`**（或 `classes2.dex`…）。`framework.jar` 少了 dex 就是块砖，工具会警告但不阻止。
2. **不要给平台 jar 加签名**。带 `META-INF/*.SF` 的新 jar 会让工具跳过重打包，且部分 ROM 校验签名后拒绝加载。
3. **dex 不能压缩处理错**。原包是 STORED 就用 STORED；要保留新包原样加 `--keep-zip`，但部分 ROM 的 dex2oat 会因此失败。
4. **不要用 `replace` 改符号链接**。`/framework/xxx.jar` 若是链接，工具直接拒绝，改用 `--path` 指向真实文件。

#### 新 jar 怎么来

最省事的做法是**只换 `classes*.dex`，保留原 jar 的其它成员**（资源、`META-INF/MANIFEST.MF` 等）：

```bash
# 1. 从原 jar 抽骨架
unzip -o old.jar -d jar_src

# 2. 编译出 dex：--min-api 决定 dex 版本（29 -> 039，34 -> 040）
d8 --min-api 29 --output jar_src/ classes/**/*.class

# 3. 重新打包：dex 用 -0 不压缩，其余成员照旧
cd jar_src && zip -X -r ../new.jar . && cd ..
python3 ropatch.py fs.7z replace --part system --path /framework/framework.jar new.jar
```

如果只是改 smali：`baksmali` 反编译 `classes.dex` → 改 → `smali` 回编译 → 用上面的第 3 步重新打包。工具会自动检查压缩方式，`zip` 默认压缩 dex 也没关系（只要原包是 STORED，它会在写入前重打包成 STORED）。

#### 换的是普通 jar（非 boot classpath）

`/system/framework/*.jar` 里也有不参与启动的（如 `ethernet-service.jar`、厂商 SDK 的 jar），它们没有 AOT 产物，替换更简单：`--path` 指过去、`--yes` 即可，工具查不到 AOT 时会明确打印「没有同名 .odex」。APK 内部的 `libs/*.jar` 属于 APK 自己的内容，要改就整个 APK 换。

---

## 9. apksync.py —— 批量换 APK（按包名自动匹配）

### 何时用

一次要换十几个系统 APK。用 `ropatch.py` 得先 `apks` 查包名、再抄条目号、再 `replace`，来回十几遍。`apksync` 把流程压成一条命令：给一个 APK 文件夹，它自己读包名、自己找条目、全部替换、只写回一次压缩包。

### 命令

```bash
python3 apksync.py fs.7z ./apks                  # 演练：只匹配，不写盘
python3 apksync.py fs.7z ./apks --yes            # 真替换（一次写回）
python3 apksync.py fs.7z ./apks --part system --yes     # 只扫 system，省解压时间
python3 apksync.py system/readonly.bin ./apks --yes     # 直接改解出来的镜像
```

### 参数

| 参数 | 含义 |
|---|---|
| `--yes` | 真的写入；不加只演练 |
| `--part` | 只处理该分区；默认扫描包内**所有**只读层 |
| `--first` | 同包名在多个位置时只替换第一个 |
| `--jobs` | 读 manifest 的线程数，默认 8 |
| `--fresh` | 丢弃索引缓存，重新扫描 |
| `--only` / `--exclude` | 按路径子串过滤输入 APK |
| `--no-recursive` | 只扫目录第一层 |
| `--min-entry` | 镜像里小于该体积的 `.apk` 忽略，默认 `1K`（低于它的多是空壳占位文件） |
| `--report` | 报告 TSV 路径，默认 `apksync-report-<时间>.tsv` |
| `--verify` | 写回后再做一次全量 CRC 校验（多花几分钟，重要交付建议加） |
| `--keep` / `--no-writeback` | 同 `ropatch` |

### 输出长什么样

```
[1/4] 扫描输入目录 /root/apks
  发现 14 个 APK，可识别 13 个
    Settings.apk                 com.android.settings               10.0.2(12031)  40.9 MB
    ...

[3/4] 索引分区里的 APK（首次较慢，之后走缓存）
  分区 system（1243.8 MB，条目 51234）
  非目录条目 48210 个，其中 .apk 1187 个（遍历 12.4s），开始读 manifest…
  解析完成：可用 1181 个（38.2s）
  索引已缓存：/tmp/apksync_index/system-d0b7f02f130ce054.json

[4/4] 按包名匹配

状态    输入 APK                  包名                              ROM 位置                                  版本
待替换  Settings.apk              com.android.settings              system:/app/Settings/Settings.apk         10.0.1(12030) ↑ 10.0.2(12031)
待替换  改名了.apk                com.android.launcher3             system:/priv-app/Launcher/Launcher.apk    10.0(500) ↑ 10.1(501)
相同    Phone.apk                 com.android.phone                 system:/app/Phone/Phone.apk               10.0(3000)  内容与镜像一致，跳过
未命中  NewApp.apk                com.example.newapp                -                                         -  镜像里没有这个包

汇总：待替换 12 处，内容相同 1 处，未命中 1 个包，读不出包名 1 个
```

### 匹配规则

| 情况 | 行为 |
|---|---|
| 包名相同 | 命中，替换（**不看文件名**，改名也能对） |
| 同包名多处（`/app` 与 `/priv-app`、system 与 product） | 默认全部替换，`--first` 只换第一个 |
| 目录里有 `base.apk` 的 split 包 | 只替换 `base.apk`，分包成员跳过并提示 |
| 输入目录里同包名多份 | 只保留 `versionCode` 最大的，其余在 `[去重]` 里列出 |
| 新 APK 与镜像内条目 CRC 相同 | 标记「相同」跳过，不重复写 |
| 镜像里没有该包 | 标记「未命中」，需要新增就走 `ropatch.py add --from`（见 11 节 G） |
| 读不出包名（不是 zip / 没 manifest） | `[跳过]` 并给出原因，不影响其它 APK |

### 索引缓存与耗时

第一次要遍历目录树、读上千个 `AndroidManifest.xml`，1.2 GB 的层大约 1 分钟（解压本身另算几分钟）。结果缓存在 `/tmp/apksync_index/<分区>-<hash>.json`，只要镜像大小和 mtime 没变，第二次直接命中，秒级。

所以推荐节奏：先不加 `--yes` 跑一遍（顺便把 7z 解压和索引都做掉，缓存在 `/tmp/ropatch_cache` 和 `/tmp/apksync_index`），核对表格后再加 `--yes`，此时几乎只花写回的时间。

### 和 ropatch 的关系

`apksync` 是 `ropatch` 的批处理外壳：写入仍调用同一套 `write_entry`（原位覆写 / 追加、xattr 保留、CRC 重算），只是把「一个文件一次写回」变成「N 个文件一次写回」。所以 `ropatch verify` 仍然是最终复验手段。

---

## 10. TitanFS格式分析与还原报告.md —— 参考文档

不是工具，是逆向结论：四种魔数的字段表（`SFDA` / `BPUS` / `ATEM` / `ATIT`）、只读层（ATIT 线性表）与可写层（桶文件 + 文本目录项）两套格式的对比、`7.1–7.10` 各工具的原理与排障记录。

什么时候翻它：

- 想搞清 `gen=4527` 是什么、为什么改动后会变；
- 要判断某个文件在哪个层（桶 / 记录池 / 只读层）；
- 遇到 `ANDROIDFS_CORRUPTED`、导入失败这类错误，查第 7 节的排障表；
- 需要自己写工具，想直接拿结构表。

---

## 11. 七条完整工作流

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

### D. 预置新应用（`/system/app`）

> **先看这一行**：如果包里已经有覆盖层（`androidfs_10.0.0/system/0000000N/` 这种八位十六进制目录），下面这套 `add` 写进 `readonly.bin` 的做法**不会生效**——引擎只读覆盖层。那种包用 `install`，一条命令：
>
> ```bash
> python3 ropatch.py fs.7z install --part system --dir /system/priv-app NewApp.apk --yes
> ```
>
> 只有完全没有任何覆盖层的纯净包，才用下面的 `add`。判断方法：`7z l fs.7z | grep "system/0000"`，有输出就是有覆盖层。

```bash
# 1. 确认目标目录，看它现在有什么
python3 ropatch.py fs.7z tree --part system / --depth 2

# 2. 演练，核对条目表与目录项变化
python3 ropatch.py fs.7z add --part system /system/app/NewApp NewApp.apk ./NewApp.apk --mkdir

# 3. 执行（1.2 GB 的层约几分钟，需 2 倍磁盘空间）
python3 ropatch.py fs.7z add --part system /system/app/NewApp NewApp.apk ./NewApp.apk --mkdir --yes

# 3'. 一次加一整批，只写回一次
python3 ropatch.py fs.7z add --part system /system/app --from ./新应用 --mkdir --yes

# 4. 体检：路径/目录项/排序/内容/缓存逐项核对（这一步能提前发现「装进去却看不见」）
python3 ropatch.py fs.7z check --part system --path /system/app/NewApp/NewApp.apk

# 5. 复验：目录里能看到，且 CRC 全通过
python3 ropatch.py fs.7z tree --part system /app --fresh
python3 ropatch.py fs.7z verify --part system

# 6. 导入后应用仍不出现、且 check 报「排序 ✗」
python3 ropatch.py fs.7z sortdir --part system --path /system/app --yes
```

`add` 会把新条目**按名字插到正确位置**（真实 ROM 的目录项是升序数组，追加到末尾会被引擎跳过）。第 4 步的 `check` 就是确认这一点。

要特权应用就换成 `/system/priv-app`，但要额外把白名单 XML 放到 `/system/etc/permissions/`，否则开机时 PMS 会拒绝安装。细节见 8 节「放到哪个目录」。

### E. 换 framework.jar / services.jar（只读层里的 jar）

```bash
# 1. 定位 jar 的完整路径（还能看到原包 dex 版本，新包必须 ≤ 它）
python3 ropatch.py fs.7z jars --part system --name framework

# 2. 导出原 jar 备查，确认成员结构
python3 ropatch.py fs.7z extract --part system --path /framework/framework.jar -o old.jar

# 3. 演练：dex 版本、STORED/DEFLATED、成员差异、AOT 产物全部列出来
python3 ropatch.py fs.7z replace --part system --path /framework/framework.jar 新.jar

# 4. 执行；--drop-oat 把旧 .odex/.vdex 置零，逼 ART 重编译
python3 ropatch.py fs.7z replace --part system --path /framework/framework.jar 新.jar --yes --drop-oat

# 5. 复验
python3 ropatch.py fs.7z verify --part system
```

判断要不要 `--drop-oat`：演练输出里出现 `[AOT] 找到 N 个配套产物` 就建议加；只打印「没有同名 .odex」的普通 jar（如 `ethernet-service.jar`）不需要。细节和 dex 版本对照表见 8 节「替换 jar」。

### F. 批量换十几个 APK

```bash
# 1. 把要换的 APK 全丢进一个目录（文件名随便，靠包名匹配）
ls apks/

# 2. 演练：自动解析包名 + 自动找条目 + 列出升级/降级/相同/未命中
python3 apksync.py fs.7z ./apks

# 3. 核对表格，确认后执行（一次写回，不用重复等 7z 重建）
python3 apksync.py fs.7z ./apks --yes

# 4. 复验
python3 ropatch.py fs.7z verify --part system
```

只看某个分区（比如只换 system，不动 vendor）加 `--part system`；同包名在 `/app` 和 `/priv-app` 都有、但只想换一处就加 `--first`。第二次执行会命中索引缓存，秒级出结果。

### G. 一批 APK 里既有替换也有新增

十几个 APK 里，有些是覆盖已有应用（升级），有些是 ROM 里没有的新应用。两步走，各自批量：

```bash
# 1. 先整体演练：命中的是"要替换"，未命中的是"要新增"
python3 apksync.py fs.7z ./apks

# 2. 把上一步标「未命中」的那几个单独挑出来
mkdir -p 新应用
cp ./apks/NewApp1.apk ./apks/NewApp2.apk 新应用/

# 3. 命中的一次替换（未命中的会显示"未命中"并跳过，不影响其它）
python3 apksync.py fs.7z ./apks --yes

# 4. 未命中的一次新增（平铺进 /system/app，不建子目录也能被 PMS 扫到）
python3 ropatch.py fs.7z add --part system /system/app --from 新应用 --yes

# 5. 复验
python3 ropatch.py fs.7z verify --part system
```

两次写回 = 两遍 1.2 GB 重建，顺序无所谓，但都省不掉：`apksync` 只能替换已存在的包，新增必须走 `ropatch add`。

如果新增这一步想按 `包名/包名.apk` 的标准布局放，就逐个指定（`--from` 是把整个目录平铺进一个目标目录，不能用来建多个子目录）：

```bash
python3 ropatch.py fs.7z add --part system /system/app/NewApp1 NewApp1.apk 新应用/NewApp1.apk --mkdir --yes
```

---

## 12. 常见错误对照

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
| `add` 后导入看不到新应用（旧版症状） | 目录项被追加到末尾，不再是升序，引擎按名字查跳过它 | 跑 `check`，若报 `排序 ✗` 就 `sortdir --path <父目录> --yes` |
| `check` 报「其余 N 项本来有序，只有这一项破坏顺序」 | 同上，这是唯一可精确诊断的「消失」原因 | `sortdir` 重排，不必重打 1.2 GB |
| `check` 的缓存行「同目录老条目全命中、新路径不命中」 | 只是 dentry 缓存较完整；它按路径索引，不拦新路径 | 不用处理，先看同一份输出的「排序」行 |
| `check` 全 ✓ 但新应用不出现 | 排序已排除，多为 PMS 未扫描、`minSdk`/ABI 不匹配、或缺 priv-app 白名单 | 核对 APK 的 `minSdk`/ABI；priv-app 补白名单 XML；重启后看 `logcat` 里 PackageManager 的报错 |
| `check` 显示的包名不是你的应用（如 `com.android.settings.icon`） | 旧版包名探测取字符串池里最短的 `com.*`，被组件名/资源名误导 | 用 rev2 及以后版本重跑 `check`；或用 `apksync.parse_axml` 独立核对（命令见第 13 节） |
| `add` 后同目录其它应用打不开 | 用了 `--sort`，目录项被按错误布局重排 | 回滚原包，重做时别加 `--sort` |
| `add` 报「自检不通过：目录 N 的子项对不上」 | 该目录的项布局和工具推断的不一致 | 原文件未改动；改用占位法，或把 `tree` 输出发我 |
| 换了 jar/APK，解包出来 sha256 和原来一样 | ① 写回到了别的成员（多个同名 `readonly.bin`）；② 看的是旧缓存或上次的导出目录；③ 只是压缩方式变了 | 先跑 `digest --expect`；真没进包就加 `--member` 重做 |
| `digest` 说「逻辑内容完全一致」 | 写回时按原包形态把 dex 重打成了 STORED | 正常，内容已经在包里了，别再看整包 sha256 |
| 包里有多个 `readonly.bin`，改完没生效 | 默认选中了备份/另一份成员 | `info` 看成员列表，加 `--member 路径` 锁定 |
| `add` 报「路径不存在」 | 路径写的是 Android 视角，镜像里要少一层；或该目录真没有 | 工具已自动降级；确实要新建就加 `--mkdir` |
| `priv-app` 里的应用开机被拒 | Android 9+ 需要 privapp 权限白名单 | 见 8 节，放 XML 到 `/system/etc/permissions/` |
| `[错误] 新文件不是合法 zip 容器` | 传进来的 jar 其实是目录/只改了后缀 | `unzip -l 新.jar` 先验证，确认后再来 |
| `[警告] 新 dex 版本 039 高于原 dex 035` | 新 jar 用高版本 ART 编译，ROM 不认 | 用与 ROM 同版本的工具链重新生成 dex |
| 换 jar 后卡开机 / 反复重启 | 旧 AOT 产物或 boot 镜像与 jar 不匹配 | 重做并加 `--drop-oat`；仍不行就同步 `boot.art/boot.oat` |
| 换 jar 后开机极慢但能进系统 | ART 在重建 AOT | 正常现象，等它编译完，别中途断电 |
| `apksync` 全部显示「未命中」 | 传的目录不是 APK 所在目录，或用了 `--only` 过滤 | 看 `[1/4]` 段列出的 APK 与包名是否正确 |
| `apksync` 提示「只有 split 分包」 | ROM 里该包只有 `config.*.apk`，没有 `base.apk` | 换整个 split 目录，或用 `ropatch replace` 指定条目 |
| `apksync` 某个 APK「读不出包名」 | 文件不是 zip / 被截断 / 不是 APK | `unzip -l` 验证，重新导出一份 |
| `apksync` 卡在「开始读 manifest」 | 首次索引上千个 APK，正常 | 等它跑完；下次命中 `/tmp/apksync_index` 缓存 |
| 升级工具后第一次跑 `apksync` 又要重扫 | 缓存键含脚本版本与 `--min-entry`，参数一变即失效 | 正常，重扫一次之后恢复秒级 |
| `add --from` 报「里没有 .apk/.jar 文件」 | 只取该目录顶层，不递归子目录 | 把文件摊平，或对每个子目录各跑一次 |
| `add --from` 里某个 APK 被跳过「不是 zip/APK」 | 文件头不是 `PK`，多半是下载残缺或改了后缀 | `unzip -l` 验证，重新导出一份 |

---

## 13. 版本变更记录

只记会影响「怎么用」的改动。查当前版本：`python3 ropatch.py --version`。

### rev11（2026-11-03，当前）

> 版本串为 `2026.11.11-rev11`。这一版只加「换内容」的能力，`install` 的行为不变。

| 改动 | 原因 | 对使用的影响 |
|---|---|---|
| 新增 `update`：换已有应用的 apk | 实测确认引擎是**按 inode 命中覆盖层**的：`SAN OS` 把 `PrebuiltGmsCore.apk`(178 MB) 和 `webview.apk`(134 MB) 换掉，覆盖层里只有 `00000f09` / `00000f94` 两个裸文件，**没有目录表** | 换应用只写一个文件，目录表/顺序/水位/只读层全不动；5 MB 和 200 MB 的 apk 都是一次重写、几秒完成 |
| 新增 `restore`：撤回单个 apk 的覆盖 | 试错要能低成本回退 | 删掉覆盖层那个文件就回到只读层原内容；`install` 装的新应用会被拦住并提示改用 `uninstall` |
| `install --force` 改走 `update` 的逻辑 | 旧实现 `--force` 会重写应用目录表，而且只写 `apk` 一项 —— 原来目录里的 `oat/` 等子项**会整个丢掉**；换只读层自带的系统应用时会直接把应用弄坏 | `--force` 现在等价于 `update`，只换 apk 内容；旧版 `--force` 生成过的包建议用原始包重做 |

`update` 的核心证据（同一个包的覆盖层与只读层对照）：

| 覆盖层文件 | 大小 | 只读层里对应条目 | 覆盖层里有目录表吗 |
|---|---|---|---|
| `system/00000000/00000f09` | 178,900,032 B | `/system/priv-app/PrebuiltGmsCore/PrebuiltGmsCore.apk`（`size=0` 空占位） | 没有 |
| `system/00000000/00000f94` | 134,450,148 B | `/system/product/app/webview/webview.apk`（`size=0` 空占位） | 没有 |

只读层那两条是空的、覆盖层里是真货 —— 这就是「换内容不动结构」的直接证明。

### rev10（2026-11-03）

> 版本串为 `2026.11.11-rev10`。**加应用的方式变了**：不再改 `readonly.bin`，改为写引擎的覆盖层。

| 改动 | 原因 | 对使用的影响 |
|---|---|---|
| 新增 `install` / `ovl` / `uninstall` 三个子命令，走 `androidfs_<ver>/<分区>/<桶>/<inode>` 覆盖层 | 实测确认：覆盖层里出现某目录的目录表后，引擎**只读它**，`readonly.bin` 同目录那份被整个忽略。之前 `add` 把应用写进只读层，进包了但引擎没读，所以「应用凭空消失」 | 加应用改用 `install`；`add` 仍保留，只适合没有覆盖层的纯净包 |
| 覆盖层目录表按引擎格式写：`名字=<小写hex inode>\r\n`，inode 索引分桶 `inode >> 12` | 从第三方改过的包 `SAN OS 5.5` 反推出格式，它的 `readonly.bin` 与官方原包逐条零差异，全靠覆盖层加的应用 | 新 inode 从 `只读层条数` 起分配，绝不撞原有 inode |
| `ATEM` 元数据头按系统应用规范生成：`mode 0755/0644`、`uid/gid 0:0`、`u:object_r:system_file:s0` | 那个第三方包里新应用的 SELinux 是 `u:r:untrusted_app:s0`（直接 `cp` 了 `/data/app` 的目录），强制模式下会被拒 | 本工具写的更规范，能过 SELinux 检查 |
| `Rom` 支持向 7z **新增**成员（`7z u`），并逐个回读核对字节数 | `install` 要产生原来不存在的小文件 | 压缩包模式也能直接改，不用先手工解包；包会按默认参数重建，体积可能变大 |
| 同步 `superblock.bin` 的 inode 水位 | 实测该字段是「下一个可分配 inode」：原包 4518 条时是 `0x11a6`，改过的包用到 `0x12a5` 时是 `0x12a6`。不同步，引擎可能认为新 inode 不存在 | `install` 自动推到「新 inode 最大值 + 1」，只增不减 |

**为什么这条路安全**：`readonly.bin` 一个字节都不动，CRC、`used_end`、头部 `+16/+20`、条目表全保持原样。写入失败或应用不想要，删掉 `system/0000000N/` 下那几个文件就回到出厂状态——不会出现「改坏只读层、连导入文件都失败」这种不可逆的后果。

### rev9（2026-11-03）

> 版本串为 `2026.11.11-rev9`，`--version` 可直接区分。rev6–rev9 的改动合并记在下面。

| 改动 | 原因 | 对使用的影响 |
|---|---|---|
| 写回时**原样保留**头部 `+16` / `+20`（rev6） | 旧版把 `+16` 改写成「最后一个数据块的偏移」，但这个 ROM 里它是 `0x398f74f7`，不对齐任何数据块，是镜像级标识 | rev5 及以前生成的包头部是坏的，**必须从原始包重做** |
| `info` 增加「条目表末 / 数据起点」自检（rev7） | `count` 与真实条目数不一致时，新增条目会压到数据区头部 | 两行差值必须是 `+0`（这个格式实测无间隙），否则工具会直接报 `count 偏大/偏小` |
| 不再盲信头部 `count`，用数据起点反推（rev8） | 改过的 ROM 可能在表尾追加条目却没更新计数，工具照 `count` 走会覆盖这些「看不见」的条目 | `info` 出现 `[修正]` 行时，后续新增都以修正后的条数为准 |
| 多分区包默认选 `system`（rev8） | 旧逻辑按「路径最短」排序，`root/readonly.bin`（1.7 MB）永远排第一，`path /system` 全落到 root 分区 | 不用每次加 `--part` 也不会选错；仍可 `--member <包内路径>` 指定 |
| `digest --expect` 可直接给摘要值（rev9） | 原来只认本地文件路径，想对一下哈希还得先 `sha256sum` 存成文件 | `--expect <64 位 sha256>` 或 `<8 位 crc>` 均可；传别的才当路径 |
| `add` 演练输出显示新建目录的条目号（rev9） | 新增目录的 inode 是设备端核对的锚点 | 输出 `[新建] /system/priv-app/SFM（条目 4518）`、`+ 条目 4519 SFM.apk`，进系统可 `ls -di` 对号 |

新增目录与文件各占一个条目号，顺序是「先目录、后文件」。原包 `count = 4518` 时，SFM 目录 = 条目 4518、SFM.apk = 条目 4519，演练输出应为：

```
[新建] /system/priv-app/SFM（条目 4518）

目标目录 : 新建 /system/priv-app/SFM
  + 条目 4519  SFM.apk                                      5.0 MB
新增     : 文件 1 个、目录 1 个
           条目 4518    新建目录，1 项
改写     : 目录项 1 个目录
           条目 3673    51 -> 52 项，按名字插到对应位置
条目表   : 4518 -> 4520 项（数据区整体后移 104 B）
```

### rev5（2026-11-03）

| 改动 | 原因 | 对使用的影响 |
|---|---|---|
| `--mkdir` 新建目录不再无条件剥 `system/` 前缀，改为「原样路径父目录能解析就用它」 | system-as-root 镜像根下同时有空的 `/priv-app` 和真正的 `/system/priv-app`；旧逻辑既会剥前缀，也会因为 `/priv-app/xxx` 碰巧已存在而直接命中它 | **之前用 `--mkdir` 新增到 `/system/priv-app` 的包全部位置错误**，必须从原始包重做；演练时确认输出是 `[新建] /system/priv-app/xxx`，且改写的是条目 3673（51→52 项）而不是条目 0 |
| 新增 `fscache` 子命令 | 判定分区根下的 `fscache.bin` 是完整清单还是热点缓存 | 用真实 4521 条目的 system 镜像实测：文件命中 3.7%、目录命中 1.8%，确认是部分缓存，不拦新增 |
| `titanunpack` 增加归档指纹比对 + `--fresh` | 旧行为只要 `out/raw/` 非空就直接复用，改了包再解包看到的还是改动前的内容 | 归档大小/时间变了会自动重解；`--fresh` 强制清空 `raw/`、`by-partition/`、`merged/` |

`fscache` 的实测输出（可作为基线）：

```
文件      : fscache.bin  393.3 KB
镜像      : 文件 4067 个，目录 453 个
命中      : 文件 151/4067 (3.7%)，目录 8/453 (1.8%)
结论      : ✓ 缓存只覆盖少数路径 —— 是部分缓存，缺失会回落目录遍历。
```

### rev2（2026-11-11）

> 版本串为 `2026.11.11-rev2`，`--version` 可直接区分。

| 改动 | 原因 | 对使用的影响 |
|---|---|---|
| `check` / `apks` 的包名改按 AXML 根节点 `<manifest package>` 解析 | 旧实现取字符串池里**最短**的 `com.*`，manifest 里只要有 `com.android.settings.icon` 这类更短的组件名就会报错 | 包名显示可能与之前不同；以新结果为准，`apksync` 的匹配逻辑未变 |
| `fscache.bin` 相关提示改为「不拦新增」 | 它是按路径名索引的 dentry 缓存，不是 inode 号表 | 看到「不含该路径」不必改用占位法，先查排序 |
| `sortdir` 文档澄清变长时的写入行为 | 重排后总长可能变化 | size 不变则原位覆写，变长则追加、原数据成空洞 |

包名的独立核对（不依赖 `check`）：

```bash
python3 ropatch.py fs.7z extract --part system --path /system/priv-app/SFM/SFM.apk -o /tmp/SFM.apk
python3 - <<'PY'
import zipfile, sys
sys.path.insert(0, '.')
import apksync
z = zipfile.ZipFile('/tmp/SFM.apk')
print(apksync.parse_axml(z.read('AndroidManifest.xml')))
PY
```

### 2026-11-11（初版）

| 改动 | 原因 | 对使用的影响 |
|---|---|---|
| `add` 默认按名字**插入**目录项，新增 `--sort` / `--append` | 真实 ROM 的目录项是升序数组，追加到末尾会被引擎按名字查找时跳过 | 新增应用不再「凭空消失」；旧包可用 `sortdir` 就地修 |
| 新增 `sortdir` 子命令 | 补救旧版 `append` 写出的无序目录 | 不必重打 1.2 GB |
| `add` 自检改按**原布局**解析 | 自动探测布局时，写反了也会「自洽」，自检漏判 | 布局不符会直接拒绝写入，不会产出坏包 |

### 2026-11-08

| 改动 | 原因 | 对使用的影响 |
|---|---|---|
| `add` 不再整块重排父目录项，改为追加 + 自检 | 旧版按错误布局重排，导致新 APK 变成同目录另一个 APK（5 MB 变 1.1 MB 的浏览器） | 这类坏包无法局部修复，只能从原始包重做 |
| `Rom.writeback()` 写前校验成员名、写后回读比对 size/CRC | `7z u` 遇到不存在的成员会新增而不是替换，造成「写回成功但 sha256 没变」 | 写回失败会直接报错，不再静默 |
| 新增 `digest` 子命令 | 需要一个绕开导出环节的独立验证 | 判断「改动到底进没进包」以它为准 |

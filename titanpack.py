#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RO=/root`/workspace/unpacked/raw/androidfs_10.0.0/system/readonly.bin`

python3 ropatch.py info $RO                          # 条目数、数据区、尾部空闲
python3 ropatch.py find $RO "com.android.settings"   # 定位是第几个条目
python3 ropatch.py extract $RO <条目号> -o old.apk    # 导出确认 + CRC 校验
python3 ropatch.py replace $RO <条目号> 改好的.apk --yes --backup
python3 ropatch.py verify $RO                        # 全量 CRC 复验

titanpack.py —— VPhoneGaga Titan FS 打包 / 回灌工具

把解包出来的目录重新打成可导入虚拟机的压缩包，或者把改过的文件写回镜像。

三种用法
--------
1) 原样重打包（最安全，改文件后最常用）
   python titanpack.py out/raw -o fs.7z

2) 带覆盖层回灌（把修改后的文件按 Android 路径放好，自动找到对应 inode 写回）
   python titanpack.py out -o fs.7z --overlay mypatch/
   其中 mypatch/ 里放 data/misc/wifi/WifiConfigStore.xml 这类相对路径文件

3) 仅回灌不打包
   python titanpack.py out --overlay mypatch/ --apply-only

4) 已经误改了 by-partition 导出副本，一键收养回镜像
   python titanpack.py out -o fs.7z --adopt-unpacked out
   （省略目录值则自动向上查找 by-partition；加 --dry-run 只看不改）

默认行为
--------
- 符号链接：跳过并从压缩包里排除（绝不跟随读取宿主机文件），明细写 links.tsv
- 路径穿越：压缩包内条目名统一规范化，拒绝 ".." / 绝对路径
- 打包后自动校验：重新打开压缩包核对条目数与字节数
"""

VERSION = "2026.11.02"

import os
import re
import sys
import csv
import stat
import time
import shutil
import struct
import hashlib
import subprocess
import zlib
import argparse
import zipfile
import tarfile
import tempfile

MAGIC_SUPER = b"BPUS"
MAGIC_ATIT = b"ATIT"
MAGIC_ATEM = b"ATEM"
PART_NAMES = {"root", "system", "vendor", "product", "system_ext", "odm",
              "data", "cache", "metadata", "persist", "sdcard", "apex"}

WIN_BAD = re.compile(r'[<>:"|?*\\/\x00-\x1f]')

LINKS = []          # (相对路径, 目标, 处理)
TMPDIRS = []        # 需要清理的临时目录
STATE = {"files": 0, "bytes": 0, "dirs": 0, "skipped_links": 0, "errors": 0}


def log(msg=""):
    print(msg, flush=True)


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


# ---------------------------------------------------------------- 镜像定位

def find_fs_roots(root):
    """在解包输出里找到所有 Titan FS 根目录（含 androidfs.bin 或 superblock.bin）"""
    hits = []
    for cur, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in
                   ("by-partition", "merged", "__pycache__")]
        if "androidfs.bin" in files or "superblock.bin" in files:
            hits.append(cur)
            dirs[:] = []
            continue
        # 分区目录本身
        subs = set(os.listdir(cur))
        if subs & PART_NAMES and any(
                os.path.exists(os.path.join(cur, s, "superblock.bin"))
                or os.path.exists(os.path.join(cur, s, "fscache.bin"))
                or os.path.exists(os.path.join(cur, s, "readonly.bin"))
                for s in subs & PART_NAMES):
            hits.append(cur)
            dirs[:] = []
    return hits


def auto_prefix(fsroot):
    """从 fs 根往上找一层名为 raw 的目录，返回它到 fsroot 的相对路径。

    解包产物 out/raw/androidfs_10.0.0 -> "androidfs_10.0.0"（保留原包目录层）
    解包产物 out/raw                 -> ""（原包就是扁平的）
    直接给 fs 根                     -> ""
    """
    cur = fsroot
    while True:
        parent = os.path.dirname(cur)
        if parent == cur:
            return ""
        if os.path.basename(parent) == "raw":
            rel = os.path.relpath(fsroot, parent).replace(os.sep, "/")
            return "" if rel == "." else rel
        cur = parent


def pick_fs_root(path):
    """把用户给的路径收敛成一个 fs 根目录"""
    path = os.path.abspath(path)
    if os.path.exists(os.path.join(path, "androidfs.bin")):
        return path
    if any(os.path.exists(os.path.join(path, s, "superblock.bin"))
           or os.path.exists(os.path.join(path, s, "fscache.bin"))
           or os.path.exists(os.path.join(path, s, "readonly.bin"))
           for s in PART_NAMES):
        return path
    cands = []
    for sub in ("raw", "."):
        base = os.path.join(path, sub)
        if os.path.isdir(base):
            cands += find_fs_roots(base)
    if cands:
        return cands[0]
    return None


# ---------------------------------------------------------------- 安全遍历

def walk_safe(root, exclude=()):
    """遍历目录，产出 (绝对路径, 相对路径, 类型)。

    类型: file / dir / link / special。不跟随任何符号链接。
    """
    root = os.path.abspath(root)
    for cur, dirs, files in os.walk(root, followlinks=False):
        # 不进入指向目录的符号链接
        keep = []
        for d in sorted(dirs):
            full = os.path.join(cur, d)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if any(p and (rel == p or rel.startswith(p + "/")) for p in exclude):
                continue
            if os.path.islink(full):
                yield full, rel, "link"
                continue
            keep.append(d)
            yield full, rel, "dir"
        dirs[:] = keep
        for f in sorted(files):
            full = os.path.join(cur, f)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if any(p and (rel == p or rel.startswith(p + "/")) for p in exclude):
                continue
            if os.path.islink(full):
                yield full, rel, "link"
            elif os.path.isfile(full):
                yield full, rel, "file"
            else:
                yield full, rel, "special"


def safe_rel(rel, on_bad):
    """规范化压缩包内路径，拒绝穿越"""
    rel = rel.replace("\\", "/").strip("/")
    if not rel or rel == ".":
        return None
    parts = []
    for seg in rel.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            on_bad("路径含 .. ：%s" % rel)
            return None
        parts.append(WIN_BAD.sub("_", seg))
    return "/".join(parts)


def link_target_text(path, limit=4096):
    try:
        t = os.readlink(path)
    except OSError:
        return ""
    return t[:limit]


# ---------------------------------------------------------------- 打包

SEVENZ_NAMES = ("7z", "7za", "7zz", "7zr", "7z.exe", "7za.exe", "7zz.exe")
SEVENZ_PATHS = (
    "/usr/bin/7z", "/usr/local/bin/7z", "/usr/lib/p7zip/7z",
    "/usr/libexec/p7zip/7z", "/opt/homebrew/bin/7z", "/opt/homebrew/bin/7zz",
    "/usr/local/bin/7zz", "/usr/bin/7zz", "/snap/bin/7z",
)


def sevenz_exe():
    """找 7z 可执行文件：先查 PATH，再试各发行版的固定安装位置"""
    for e in SEVENZ_NAMES:
        p = shutil.which(e)
        if p and os.access(p, os.X_OK):
            return p
    for p in SEVENZ_PATHS:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def sevenz_version(exe):
    try:
        r = subprocess.run([exe], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=15)
        for line in r.stdout.decode("utf-8", "replace").splitlines():
            if "7-Zip" in line or "p7zip" in line:
                return line.strip()
    except Exception:
        pass
    return ""


def _tail(b, n=800):
    return b.decode("utf-8", "replace")[-n:]


def cli_layout(fsroot, prefix):
    """能不能“就地打包”（cwd + 参数，不建临时目录）。

    返回 (cwd, 归档根参数) 或 None。只有 prefix 恰好等于 fsroot 相对某个祖先的路径时才行；
    对不上就只能退回硬链接 staging。
    """
    if not fsroot or not os.path.isdir(fsroot):
        return None
    fsroot = os.path.abspath(fsroot)
    if not prefix:
        return fsroot, "."
    parts = [x for x in prefix.split("/") if x]
    anc = fsroot
    for _ in parts:
        anc = os.path.dirname(anc)
    if os.path.relpath(fsroot, anc).replace(os.sep, "/") != prefix:
        return None
    return anc, prefix


def make_7z_cli(dest, entries, prefix, volume=None, level=5,
                fsroot=None, excludes=(), allow_inplace=True):
    """用系统 7z 命令行打包。

    1.7 GB 这种镜像必须避免“先复制一份到临时目录”，所以优先就地打包。
    """
    exe = sevenz_exe()
    if not exe:
        log("  [错误] 没找到 7z 可执行文件（7z / 7za / 7zz / 7zr 都不在 PATH）")
        log("         当前 PATH: %s" % (os.environ.get("PATH") or "(空)"))
        log("         Alpine: apk add p7zip    Debian/Ubuntu: apt install p7zip-full")
        log("         macOS : brew install sevenzip")
        return False

    # 7z a 是“添加到已有档案”，目标已存在就会变成更新，必须先删掉
    if os.path.exists(dest):
        try:
            os.unlink(dest)
        except OSError:
            pass

    mx = max(0, min(9, int(level)))
    base_cmd = [exe, "a", "-t7z", "-y", "-bd", "-mx=%d" % mx]
    if mx:
        base_cmd.append("-mmt=on")          # LZMA2 多线程
    if volume:
        base_cmd.append("-v%d" % volume)

    # 就地打包会整目录打包，绕过条目级的链接过滤；
    # 镜像里只要出现过链接，就不许就地，改走 staging（staging 只放真实文件）
    lay = cli_layout(fsroot, prefix) if allow_inplace else None
    if lay:
        cwd, arg = lay
        cmd = base_cmd + ["-xr!%s" % x for x in excludes if x] + [dest, arg]
        log("  就地打包（不复制文件）: cwd=%s  arg=%s" % (cwd, arg))
        if not allow_inplace:
            log("  [内部错误] 不应走到这里")
        r = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT)
        if r.returncode != 0 and "-mmt=on" in cmd:
            cmd.remove("-mmt=on")
            log("  多线程失败，改单线程重试")
            r = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT)
        if r.returncode != 0:
            log("  [错误] 7z 打包失败: %s" % _tail(r.stdout))
            return False
        return True

    # 前缀层级对不上：用硬链接树过渡（同盘零拷贝，跨盘才会真的复制）
    stage = tempfile.mkdtemp(prefix="titanpack_")
    TMPDIRS.append(stage)
    copied = 0
    try:
        for full, rel, kind in entries:
            arc = (prefix + "/" + rel) if prefix else rel
            dst = os.path.join(stage, *arc.split("/"))
            os.makedirs(os.path.dirname(dst) or stage, exist_ok=True)
            if kind == "dir":
                os.makedirs(dst, exist_ok=True)
                continue
            try:
                os.link(full, dst)
            except OSError:
                shutil.copy2(full, dst)
                copied += 1
        if copied:
            log("  提示: %d 个文件被复制到临时目录（包内前缀与镜像根层级不匹配）" % copied)
        cmd = base_cmd + [dest, "."]
        r = subprocess.run(cmd, cwd=stage, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT)
        if r.returncode != 0:
            log("  [错误] 7z 打包失败: %s" % _tail(r.stdout))
            return False
        return True
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def make_7z(dest, entries, prefix, volume=None, level=5, fsroot=None, excludes=()):
    """优先系统 7z（快、省内存），没有才用 py7zr"""
    exe = sevenz_exe()
    if exe:
        log("  后端: %s  %s" % (exe, sevenz_version(exe)))
        allow_inplace = not STATE.get("have_links")
        if not allow_inplace:
            log("  镜像内含符号链接，改用安全模式打包（只打实体文件）")
        return make_7z_cli(dest, entries, prefix, volume, level, fsroot,
                           excludes, allow_inplace)

    try:
        import py7zr
    except ImportError:
        py7zr = None
    if py7zr is None:
        log("  [错误] 没有可用的 7z 后端")
        log("         Alpine: apk add p7zip      Debian: apt install p7zip-full")
        log("         或: python3 -m venv venv && . venv/bin/activate && pip install py7zr")
        return False

    log("  后端: py7zr %s（未找到 7z 命令行，大镜像会很慢）"
        % getattr(py7zr, "__version__", "?"))
    filters = None
    if level == 0:
        filters = [{"id": py7zr.FILTER_COPY}]
    with py7zr.SevenZipFile(dest, "w", filters=filters) as z:
        for full, rel, kind in entries:
            arc = (prefix + "/" + rel) if prefix else rel
            if kind == "dir":
                try:
                    z.write(full, arcname=arc)
                except Exception:
                    pass
                continue
            try:
                z.write(full, arcname=arc)
            except Exception as e:
                STATE["errors"] += 1
                log("    [跳过] %s (%s)" % (arc, e))
    return True


def make_zip(dest, entries, prefix):
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        for full, rel, kind in entries:
            arc = (prefix + "/" + rel) if prefix else rel
            if kind == "dir":
                z.writestr(arc.rstrip("/") + "/", b"")
                continue
            try:
                z.write(full, arc)
                STATE["files"] += 1
                STATE["bytes"] += os.path.getsize(full)
            except OSError as e:
                STATE["errors"] += 1
                log("    [跳过] %s (%s)" % (arc, e))
    return True


def make_tar(dest, entries, prefix, comp="gz"):
    mode = "w:" + comp if comp else "w"
    with tarfile.open(dest, mode) as t:
        for full, rel, kind in entries:
            arc = (prefix + "/" + rel) if prefix else rel
            if kind == "dir":
                t.add(full, arcname=arc, recursive=False)
                continue
            try:
                t.add(full, arcname=arc, recursive=False)
                STATE["files"] += 1
                STATE["bytes"] += os.path.getsize(full)
            except OSError as e:
                STATE["errors"] += 1
                log("    [跳过] %s (%s)" % (arc, e))
    return True


def verify_archive(path, fmt, expect_files, expect_bytes):
    """重新打开压缩包核对条目"""
    n = 0
    size = 0
    try:
        if fmt == "7z":
            try:
                import py7zr
            except ImportError:
                py7zr = None
            if py7zr is not None:
                with py7zr.SevenZipFile(path, "r") as z:
                    for f in z.list():
                        if not f.is_directory:
                            n += 1
                            size += f.uncompressed or 0
            else:
                exe = sevenz_exe()
                r = subprocess.run([exe, "l", "-slt", path], stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT)
                blocks = []
                cur = None
                for line in r.stdout.decode("utf-8", "replace").splitlines():
                    if line.startswith("Path = "):
                        if cur:
                            blocks.append(cur)
                        cur = {"Path": line[7:].strip()}
                    elif cur is not None and " = " in line:
                        k, v = line.split(" = ", 1)
                        cur[k.strip()] = v.strip()
                if cur:
                    blocks.append(cur)
                for b in blocks:
                    if not b.get("Attributes"):      # 档案信息块，不是条目
                        continue
                    if b.get("Path", "") in ("", ".") or b.get("Path", "").endswith("/"):
                        continue
                    if b.get("Folder") == "+" or "D" in b.get("Attributes", ""):
                        continue
                    try:
                        size += int(b.get("Size", "0"))
                    except ValueError:
                        pass
                    n += 1
        elif fmt == "zip":
            with zipfile.ZipFile(path) as z:
                for i in z.infolist():
                    if not i.is_dir():
                        n += 1
                        size += i.file_size
        else:
            with tarfile.open(path, "r:*") as t:
                for m in t:
                    if m.isfile():
                        n += 1
                        size += m.size
    except Exception as e:
        log("  [警告] 校验失败: %s" % e)
        return False
    ok = (n == expect_files)
    log("  校验：条目 %d（期望 %d）%s，解压后 %s（期望 %s）%s" %
        (n, expect_files, "✓" if ok else "✗",
         human(size), human(expect_bytes), "✓" if size == expect_bytes else "≈"))
    return ok


def split_file(path, chunk):
    """按字节切分成 .001/.002...（7-Zip 的 split 格式，cat 即可合并）"""
    total = os.path.getsize(path)
    n = 0
    with open(path, "rb") as f:
        while True:
            left = f.read(chunk)
            if not left:
                break
            n += 1
            part = "%s.%03d" % (path, n)
            with open(part, "wb") as o:
                o.write(left)
    os.unlink(path)
    return n, total


def volume_bytes(s):
    if not s:
        return None
    m = re.match(r"^\s*([\d.]+)\s*([kKmMgG]?)[bB]?\s*$", s)
    if not m:
        return None
    v = float(m.group(1))
    unit = m.group(2).lower()
    mul = {"": 1, "k": 1 << 10, "m": 1 << 20, "g": 1 << 30}[unit]
    return int(v * mul)


# ---------------------------------------------------------------- 回灌（写回镜像）

def load_manifest(path):
    """读取解包时生成的 manifest.tsv，得到 path -> 分区/inode"""
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f, delimiter="\t"):
            rows.append(r)
    return rows


def find_readme_manifest(packroot):
    for name in ("manifest.tsv",):
        p = os.path.join(packroot, name)
        if os.path.exists(p):
            return p
    return None


def locate_inode_file(part_dir, ino):
    """在 <pe part>/<bucket>/<inode> 中定位内容文件"""
    b = ino & 0xFFFFFFFF
    cands = [
        os.path.join(part_dir, "%08x" % (b & ~0xFF), "%08x" % b),
        os.path.join(part_dir, "%08x" % b, "%08x" % b),
    ]
    for c in cands:
        if os.path.exists(c):
            return c
    # 兜底：全桶扫描
    if os.path.isdir(part_dir):
        for entry in os.listdir(part_dir):
            if not re.match(r"^[0-9a-fA-F]{8}$", entry):
                continue
            c = os.path.join(part_dir, entry, "%08x" % b)
            if os.path.exists(c):
                return c
    return None


def patch_atem_size(meta_path, new_size):
    """更新 .metadata 里的 size 字段（偏移 40）"""
    if not os.path.exists(meta_path):
        return False
    with open(meta_path, "r+b") as f:
        head = f.read(8)
        if head[:4] != MAGIC_ATEM:
            return False
        f.seek(40)
        f.write(struct.pack("<I", new_size & 0xFFFFFFFF))
    return True


def patch_pool_size(pool_path, ino, new_size):
    """在 ATIT 记录池里把指定 inode 的 size 改掉（记录头 +28）"""
    if not os.path.exists(pool_path):
        return False
    data = bytearray(open(pool_path, "rb").read())
    if bytes(data[:4]) != MAGIC_ATIT:
        return False
    pos = 0
    hit = 0
    while pos + 84 <= len(data):
        if bytes(data[pos:pos + 4]) != MAGIC_ATIT:
            pos += 4
            continue
        rlen = struct.unpack_from("<I", data, pos + 4)[0]
        if rlen < 84 or pos + rlen > len(data):
            pos += 4
            continue
        rec_ino = struct.unpack_from("<Q", data, pos + 20)[0]
        if rec_ino == ino:
            struct.pack_into("<Q", data, pos + 28, new_size)
            hit += 1
        pos += rlen
    if hit:
        with open(pool_path, "wb") as f:
            f.write(data)
    return bool(hit)


def apply_overlay(fsroot, overlay, manifest_path, dry=False):
    """把覆盖层文件写回镜像内的 inode 内容文件"""
    rows = load_manifest(manifest_path) if manifest_path else []
    by_path = {}
    for r in rows:
        by_path[(r.get("partition", ""), r.get("path", ""))] = r

    if not rows:
        log("  [警告] 没有 manifest.tsv，按 <分区>/<相对路径> 直接匹配")
    log("  覆盖层: %s" % overlay)
    changed = 0
    skipped = []
    for full, rel, kind in walk_safe(overlay):
        if kind == "dir":
            continue
        if kind == "link":
            skipped.append((rel, "覆盖层里的符号链接（不跟随）"))
            continue
        if kind != "file":
            skipped.append((rel, "特殊文件"))
            continue
        vpath = "/" + rel
        seg = rel.split("/")
        part = seg[0] if len(seg) > 1 else ""
        row = None
        # 1) 覆盖层按 “分区名/分区内路径” 组织
        if part:
            row = by_path.get((part, "/" + "/".join(seg[1:])))
        # 2) 直接按分区内绝对路径匹配任意分区
        if row is None:
            for (p, vp), r in by_path.items():
                if vp == vpath:
                    row, part = r, p
                    break
        # 3) 覆盖层直接用镜像内绝对路径（不含分区名前缀）
        if row is None:
            for (p, vp), r in by_path.items():
                if vp == "/" + "/".join(seg[1:]) and p == part:
                    row = r
                    break
        if row is None:
            skipped.append((rel, "镜像里没有该路径（新增文件暂不支持：可先在虚拟机内建同名空文件再回灌）"))
            continue
        typ = row.get("type", "")
        if typ != "file":
            skipped.append((rel, "原镜像里不是普通文件(%s)" % typ))
            continue
        ino = int(row.get("inode") or 0)
        part_dir = os.path.join(fsroot, part)
        tgt = locate_inode_file(part_dir, ino)
        if not tgt:
            if os.path.exists(os.path.join(part_dir, "readonly.bin")):
                skipped.append((rel, "只读层内联在 readonly.bin（带 CRC32），不支持直接回灌"))
            else:
                skipped.append((rel, "找不到 inode %d 的内容文件" % ino))
            continue
        size = os.path.getsize(full)
        old = os.path.getsize(tgt)
        if not dry:
            with open(full, "rb") as src, open(tgt, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            patch_atem_size(tgt + ".metadata", size)
            for pool in (os.path.join(part_dir, "fscache.bin"),
                         os.path.join(part_dir, "00000000", "fscache.bin")):
                if os.path.exists(pool):
                    patch_pool_size(pool, ino, size)
        changed += 1
        log("    %-52s inode=%-6d %s -> %s" % (vpath, ino, human(old), human(size)))
    log("  回灌%s：%d 个文件，跳过 %d 个" % ("(演练)" if dry else "", changed, len(skipped)))
    for rel, why in skipped[:20]:
        log("    [跳过] %s（%s）" % (rel, why))
    return changed


# ---------------------------------------------------------------- 镜像完整性检查

MUST_HAVE = ("androidfs.bin",)


def _read(path, n=None):
    with open(path, "rb") as f:
        return f.read() if n is None else f.read(n)


def check_image(fsroot, fix=False):
    """检查镜像是否自洽，返回 (问题列表, 提示列表)"""
    problems = []
    notes = []

    afs = os.path.join(fsroot, "androidfs.bin")
    if not os.path.exists(afs):
        problems.append("缺 androidfs.bin（ROM 描述符）")
    else:
        b = _read(afs, 16)
        if b[:4] != b"SFDA":
            problems.append("androidfs.bin 魔数不是 SFDA")
        else:
            ver = struct.unpack_from("<I", b, 4)[0]
            notes.append("ROM 版本 %d.%d.%d / build %d" %
                         (ver // 100, (ver // 10) % 10, ver % 10,
                          struct.unpack_from("<I", b, 8)[0]))
    for f in ("config.gz", "locales.bin"):
        if not os.path.exists(os.path.join(fsroot, f)):
            notes.append("缺 %s（引擎可能自带，不影响挂载）" % f)

    parts = sorted(d for d in os.listdir(fsroot)
                   if os.path.isdir(os.path.join(fsroot, d)) and d in PART_NAMES)
    if not parts:
        problems.append("一个分区都没找到")

    for p in parts:
        pdir = os.path.join(fsroot, p)
        sb = os.path.join(pdir, "superblock.bin")
        if not os.path.exists(sb):
            problems.append("%s: 缺 superblock.bin" % p)
        else:
            b = _read(sb)
            if b[:4] != MAGIC_SUPER:
                problems.append("%s: superblock 魔数不是 BPUS" % p)
            elif len(b) != 16:
                notes.append("%s: superblock 长度 %d（常见 16）" % (p, len(b)))
            else:
                notes.append("%s: 代次 gen=%d" % (p, struct.unpack_from("<I", b, 8)[0]))

        # 只读层：逐条校验 CRC
        ro = os.path.join(pdir, "readonly.bin")
        if os.path.exists(ro):
            d = _read(ro)
            if d[:4] != MAGIC_ATIT:
                problems.append("%s: readonly.bin 魔数不是 ATIT" % p)
            else:
                cnt = struct.unpack_from("<I", d, 8)[0]
                esz = struct.unpack_from("<I", d, 24)[0] or 52
                bad = 0
                for i in range(cnt):
                    o = 0x40 + i * esz
                    size, off = struct.unpack_from("<II", d, o + 36)
                    crc = struct.unpack_from("<I", d, o + 44)[0]
                    if zlib.crc32(d[off:off + size]) & 0xFFFFFFFF != crc:
                        bad += 1
                if bad:
                    problems.append("%s: readonly.bin 有 %d 条 CRC 不符（引擎会判损坏）" % (p, bad))

        # 记录池：size 与内容文件是否一致
        fc = os.path.join(pdir, "fscache.bin")
        if os.path.exists(fc):
            b = _read(fc, 4)
            if b != MAGIC_ATIT:
                problems.append("%s: fscache.bin 魔数不是 ATIT" % p)
            else:
                fixed = 0
                bad = 0
                for ino, sz in pool_records(fc):
                    tgt = locate_inode_file(pdir, ino)
                    if not tgt:
                        continue
                    real = os.path.getsize(tgt)
                    if real != sz:
                        bad += 1
                        if fix:
                            patch_pool_size(fc, ino, real)
                            fixed += 1
                if fixed:
                    notes.append("%s: 已修正 %d 条记录池 size" % (p, fixed))
                elif bad:
                    problems.append("%s: 有 %d 条记录池 size 与内容文件不一致" % (p, bad))
    return problems, notes


def pool_records(pool_path, limit=100000):
    """遍历 ATIT 记录池，产出 (inode, size)"""
    data = _read(pool_path)
    pos = 0
    n = 0
    while pos + 84 <= len(data) and n < limit:
        if data[pos:pos + 4] != MAGIC_ATIT:
            pos += 4
            continue
        rlen = struct.unpack_from("<I", data, pos + 4)[0]
        if rlen < 84 or pos + rlen > len(data):
            pos += 4
            continue
        ino = struct.unpack_from("<Q", data, pos + 20)[0]
        size = struct.unpack_from("<Q", data, pos + 28)[0]
        yield ino, size
        n += 1
        pos += rlen


def write_import_note(path, fsroot, parts, prefix):
    text = """VPhoneGaga 虚拟机 fs 包 —— 导入说明
=====================================

包内结构（与 App 原始 fs 目录一致）:
  %sandroidfs.bin         ROM 描述符
  %s<分区>/superblock.bin  分区代次
  %s<分区>/<桶>/<inode>    文件内容
  %s<分区>/fscache.bin     inode 记录池

分区: %s

导入方式（任选其一）:
1. App 内「导入实例 / 恢复备份」入口，直接选择本 7z 包；
2. 手动方式：关闭虚拟机 -> 解压本包 ->
   用解压结果整个覆盖 App 私有目录下该实例的 fs 文件夹
   （通常为 /sdcard/Android/data/<包名>/files/backup/vphone/files/vm<N>/fs）
   -> 清掉同名 .bak/.tmp 残留 -> 重新启动实例。

注意:
- 不要在虚拟机运行中替换文件，引擎会持有 fd 并拒绝写入。
- 若 App 提示 ENGINE_ERROR_ANDROIDFS_CORRUPTED，说明元数据与内容不一致，
  用 titanpack.py --check --strict 复查后再打包。
""" % (prefix, prefix, prefix, prefix, ", ".join(parts) if parts else "(未识别)")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


# ---------------------------------------------------------------- 主流程

def hint_overlay(missing, fsroot, src):
    """覆盖层目录不存在时，给出可执行的修复建议"""
    log("")
    log("  覆盖层是用来“改文件后回灌”的，目录必须真实存在，且按 Android 路径组织。")
    log("  两种处理方式：")
    log("")
    log("  A) 只是把镜像原样打回压缩包 —— 去掉 --overlay 即可：")
    log("       python3 titanpack.py %s -o fs.7z --strict" % src)
    log("")
    log("  B) 确实要改文件 —— 先建目录，把要改的文件按原路径拷进去：")
    log("       mkdir -p %s/<分区>/<路径>" % missing)
    if os.path.isdir(fsroot):
        parts = sorted(d for d in os.listdir(fsroot)
                       if os.path.isdir(os.path.join(fsroot, d)) and d in PART_NAMES)
        ro = [d for d in parts if os.path.exists(os.path.join(fsroot, d, "readonly.bin"))]
        if parts:
            log("     本镜像的分区: %s" % ", ".join(parts))
        if ro:
            log("     其中只读层（不可回灌）: %s" % ", ".join(ro))
        writable = [d for d in parts if d not in ro]
        if writable:
            ex = "data" if "data" in writable else writable[0]
            log("     可回灌分区: %s" % ", ".join(writable))
            log("     例如要改 /data 下的文件：")
            log("       mkdir -p %s/%s/misc/wifi" % (missing, ex))
            log("       复制 out/by-partition/%s/... 下的原文件过去，改完再执行本命令" % ex)
    log("")


def doctor():
    """环境自检：一次性看清 7z / py7zr / 磁盘空间，避免反复试错"""
    log("=== 环境自检（titanpack %s）===" % VERSION)
    log("Python  : %s" % sys.version.split()[0])
    log("平台    : %s" % sys.platform)
    exe = sevenz_exe()
    if exe:
        log("7z 命令 : %s" % exe)
        v = sevenz_version(exe)
        if v:
            log("           %s" % v)
    else:
        log("7z 命令 : 未找到")
        log("          已尝试 7z / 7za / 7zz / 7zr，以及 /usr/bin、/usr/lib/p7zip 等常见位置")
        log("          Alpine: apk add p7zip    Debian: apt install p7zip-full")
    try:
        import py7zr
        log("py7zr   : %s" % getattr(py7zr, "__version__", "已安装"))
    except ImportError:
        log("py7zr   : 未安装（有 7z 命令就不需要它）")
    try:
        du = shutil.disk_usage(os.getcwd())
        log("磁盘    : 当前目录剩余 %s / 共 %s" % (human(du.free), human(du.total)))
    except OSError:
        pass
    log("")
    if exe:
        log("结论: 可以直接打包 —— python3 titanpack.py <镜像目录> -o fs.7z --strict")
    else:
        log("结论: 缺 7z 后端，先装 p7zip 再来")
    return 0


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def find_by_partition(src):
    """从任意一层输入目录向上找 by-partition"""
    seen = []
    b = os.path.abspath(src)
    for _ in range(4):
        if b in seen:
            break
        seen.append(b)
        bp = os.path.join(b, "by-partition")
        if os.path.isdir(bp):
            return b, bp
        b = os.path.dirname(b)
    return None, None


def scan_export_changes(unpack_dir, fsroot, manifest_path=""):
    """对比 by-partition 导出副本与镜像内 inode 内容，找出被改过的文件

    返回 [(分区, 镜像内路径, inode, 导出文件, 镜像文件, 旧大小, 新大小)]
    判定：大小不同 → 改过；大小相同但副本 mtime 更新 → 再比 sha256。
    """
    bp = os.path.join(unpack_dir, "by-partition")
    mf = manifest_path or os.path.join(unpack_dir, "manifest.tsv")
    rows = load_manifest(mf) if os.path.exists(mf) else []
    if not rows:
        return [], "缺少 manifest.tsv，无法把导出路径映射回 inode", []
    by_path = {}
    for r in rows:
        part = r.get("partition", "")
        p = r.get("path", "")
        by_path[(part, p)] = r
        # 再登记一份“分区内相对路径”，与 by-partition/<分区>/ 的目录布局对齐
        pref = "/" + part
        if p == pref or p.startswith(pref + "/"):
            by_path.setdefault((part, p[len(pref):] or "/"), r)
    out = []
    ro_hits = []
    for part in sorted(os.listdir(bp)):
        pdir = os.path.join(bp, part)
        if not os.path.isdir(pdir) or part not in PART_NAMES:
            continue
        part_root = os.path.join(fsroot, part)
        for cur, dirs, files in os.walk(pdir):
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(cur, d))]
            for f in files:
                full = os.path.join(cur, f)
                if os.path.islink(full):
                    continue
                rel = os.path.relpath(full, pdir).replace(os.sep, "/")
                row = by_path.get((part, "/" + rel))
                if not row or row.get("type") != "file":
                    continue
                try:
                    ino = int(row.get("inode") or 0)
                except ValueError:
                    continue
                img = locate_inode_file(part_root, ino)
                if not img:
                    ro = os.path.join(part_root, "readonly.bin")
                    if os.path.exists(ro):
                        try:
                            if os.path.getmtime(full) > os.path.getmtime(ro):
                                ro_hits.append("/" + part + "/" + rel)
                        except OSError:
                            pass
                    continue
                try:
                    es, isz = os.path.getsize(full), os.path.getsize(img)
                except OSError:
                    continue
                vpath = "/" + part + "/" + rel
                if es != isz:
                    out.append((part, vpath, ino, full, img, isz, es))
                elif os.path.getmtime(full) > os.path.getmtime(img) + 1:
                    try:
                        if _sha256(full) != _sha256(img):
                            out.append((part, vpath, ino, full, img, isz, es))
                    except OSError:
                        pass
    return out, "", ro_hits


def adopt_from_unpacked(fsroot, unpack_dir, manifest_path="", dry=False, limit=20):
    """把 by-partition 里改过的文件自动写回镜像（等价于原地做 overlay）"""
    changes, why, ro_hits = scan_export_changes(unpack_dir, fsroot, manifest_path)
    if why:
        log("  [跳过] " + why)
        return 0
    if not changes:
        log("  ✓ by-partition 与镜像内容一致，没有需要收养的改动")
        return 0
    log("  发现 %d 个在 by-partition 里被改过的文件：" % len(changes))
    for part, vpath, ino, full, img, old, new in changes[:limit]:
        log("    %-56s %s -> %s" % (vpath, human(old), human(new)))
    if len(changes) > limit:
        log("    ... 另有 %d 个" % (len(changes) - limit))
    if ro_hits:
        log("  [注意] %d 个文件位于 readonly.bin 只读层（带 CRC32），无法直接回灌：" % len(ro_hits))
        for v in ro_hits[:5]:
            log("           %s" % v)
        log("         这类文件要靠 --overlay 之外的手段处理，先用 apkcheck.py 确认它到底在不在只读层。")
    if dry:
        log("  [演练] 未写入镜像")
        return 0
    ok = 0
    for part, vpath, ino, full, img, old, new in changes:
        part_root = os.path.join(fsroot, part)
        try:
            with open(full, "rb") as s, open(img, "wb") as d:
                shutil.copyfileobj(s, d, 1 << 20)
            patch_atem_size(img + ".metadata", new)
            for pool in (os.path.join(part_root, "fscache.bin"),
                         os.path.join(part_root, "00000000", "fscache.bin")):
                if os.path.exists(pool):
                    patch_pool_size(pool, ino, new)
            ok += 1
        except OSError as e:
            log("    [失败] %s: %s" % (vpath, e))
    log("  ✓ 已写回镜像 %d 个文件（原文件被覆盖，不再需要 --overlay）" % ok)
    return ok


def warn_export_dirs(src, fsroot):
    """改了导出目录却来打包原始镜像，是最常见的“改了没生效”"""
    b, bp = find_by_partition(src)
    if not bp:
        return False
    changes, why, ro_hits = scan_export_changes(b, fsroot)
    if changes:
        log("  [警告] %s/by-partition 里有 %d 个文件比镜像新（你改的应该是这份副本）" % (b, len(changes)))
        for part, vpath, ino, full, img, old, new in changes[:5]:
            log("           %s" % vpath)
        if len(changes) > 5:
            log("           ... 另有 %d 个" % (len(changes) - 5))
        log("         那是解包导出的副本，打包读的是 raw/ 镜像，直接改它不会进包。")
        log("         两种改法任选其一：")
        log("           A) 自动收养（推荐，改好的文件原样写回镜像）：")
        log("              python3 titanpack.py %s -o fs.7z --adopt-unpacked %s --strict"
            % (os.path.basename(src) or ".", b))
        log("           B) 走覆盖层：把文件按 system/... 放到 patch/ 下，再加 --overlay patch")
        return True
    log("  [提示] 发现 %s/by-partition（解包导出产物，不会被直接打包；当前内容与镜像一致）" % b)
    return False


def main():
    ap = argparse.ArgumentParser(
        description="VPhoneGaga Titan FS 打包 / 回灌工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python titanpack.py out/raw -o fs.7z\n"
               "  python titanpack.py out -o fs.7z --overlay patch/\n"
               "  python titanpack.py out -o fs.7z --adopt-unpacked out\n"
               "  python titanpack.py out --overlay patch/ --apply-only\n"
               "  python titanpack.py out/raw -o fs.7z --prefix backup/vphone/files/vm1/fs --volume 20m")
    ap.add_argument("src", nargs="?", default="",
                    help="解包输出目录（含 raw/ 或直接是 fs 根）")
    ap.add_argument("-o", "--out", default="", help="输出压缩包路径")
    ap.add_argument("-f", "--format", default="auto", choices=["auto", "7z", "zip", "tar"],
                    help="压缩格式（默认按 -o 后缀判断，否则 7z）")
    ap.add_argument("--prefix", default="", help="压缩包内的前缀路径")
    ap.add_argument("--volume", default="", help="7z 分卷大小，如 20m / 4g")
    ap.add_argument("--level", type=int, default=5, help="0=仅存储不压缩，5=默认压缩")
    ap.add_argument("--exclude", default="", help="排除路径，逗号分隔（相对 fs 根）")
    ap.add_argument("--links", default="skip", choices=["skip", "text"],
                    help="符号链接处理：skip=跳过(默认)；text=写成 .link.txt 文本")
    ap.add_argument("--overlay", default="", help="覆盖层目录（按 Android 路径组织）")
    ap.add_argument("--adopt-unpacked", nargs="?", const="auto", default="",
                    metavar="DIR",
                    help="把解包输出目录 by-partition 里被改过的文件自动写回镜像；"
                         "省略 DIR 则自动向上查找 by-partition")
    ap.add_argument("--manifest", default="", help="manifest.tsv 路径（默认自动找）")
    ap.add_argument("--apply-only", action="store_true", help="只回灌/只收养，不打包")
    ap.add_argument("--dry-run", action="store_true", help="演练，不实际写入")
    ap.add_argument("--no-check", action="store_true", help="跳过打包前的完整性检查")
    ap.add_argument("--no-fix", action="store_true", help="只报告不自动修复记录池 size")
    ap.add_argument("--strict", action="store_true", help="检查不通过则中止打包")
    ap.add_argument("--check-only", action="store_true", help="只做完整性检查，不打包")
    ap.add_argument("--doctor", action="store_true", help="检查 7z / py7zr / 磁盘环境")
    ap.add_argument("--version", action="version", version="titanpack " + VERSION)
    args = ap.parse_args()

    if args.doctor:
        return doctor()
    if not args.src:
        ap.print_help()
        return 2

    src = os.path.abspath(args.src)
    if not os.path.exists(src):
        log("[错误] 输入不存在: %s" % src)
        return 2

    fsroot = pick_fs_root(src)
    if not fsroot:
        log("[错误] 没找到 Titan FS 根目录（缺 androidfs.bin / */superblock.bin）")
        return 1

    log("=== Titan FS 打包  v%s ===" % VERSION)
    log("镜像根: %s" % fsroot)
    parts = sorted(s for s in os.listdir(fsroot)
                   if os.path.isdir(os.path.join(fsroot, s)) and s in PART_NAMES)
    log("分区: %s" % (", ".join(parts) or "(未识别)"))

    step = [0]
    total_steps = 3 + (1 if args.overlay else 0) + (1 if args.adopt_unpacked else 0)

    def phase(name):
        step[0] += 1
        log("[%d/%d] %s" % (step[0], total_steps, name))

    # ---- 收养 by-partition 里改过的文件
    if args.adopt_unpacked:
        b = None
        if args.adopt_unpacked == "auto":
            b, _bp = find_by_partition(src)
            if not b:
                log("[错误] 没找到 by-partition，请用 --adopt-unpacked <解包输出目录> 指定")
                return 2
        else:
            b = os.path.abspath(args.adopt_unpacked)
            if not os.path.isdir(os.path.join(b, "by-partition")):
                log("[错误] %s 下没有 by-partition 目录" % b)
                return 2
        mf = args.manifest or os.path.join(b, "manifest.tsv")
        phase("收养 by-partition 改动（manifest: %s）" % (mf if os.path.exists(mf) else "无"))
        adopt_from_unpacked(fsroot, b, "" if os.path.exists(mf) else "", args.dry_run)
        if args.apply_only:
            log("完成（仅收养）。")
            return 0

    warn_export_dirs(src, fsroot)

    # ---- 回灌
    if args.overlay:
        overlay = os.path.abspath(args.overlay)
        if not os.path.isdir(overlay):
            log("[错误] 覆盖层目录不存在: %s" % overlay)
            hint_overlay(overlay, fsroot, src)
            return 2
        mf = args.manifest or find_readme_manifest(os.path.dirname(fsroot)) \
            or find_readme_manifest(src)
        if not mf:
            for base in (os.getcwd(), os.path.dirname(fsroot), os.path.dirname(os.path.dirname(fsroot))):
                p = os.path.join(base, "manifest.tsv")
                if os.path.exists(p):
                    mf = p
                    break
        phase("回灌覆盖层（manifest: %s）" % (mf or "无"))
        apply_overlay(fsroot, overlay, mf, args.dry_run)
        if args.apply_only:
            log("完成（仅回灌）。")
            return 0

    # ---- 完整性检查（打包前必过）
    if not args.no_check:
        phase("完整性检查")
        problems, notes = check_image(fsroot, fix=not args.no_fix)
        for n in notes:
            log("  · " + n)
        for pb in problems:
            log("  ✗ " + pb)
        if problems:
            if args.strict:
                log("  [中止] --strict 模式下拒绝打包")
                return 3
            log("  [警告] 以上问题可能导致引擎报 ANDROIDFS_CORRUPTED，可加 --strict 查看详情")
        else:
            log("  ✓ 结构自洽，可直接导入")
        if args.check_only:
            log("完成（仅检查）。")
            return 3 if (problems and args.strict) else 0

    prefix = args.prefix.strip("/") or auto_prefix(fsroot)
    if prefix and not args.prefix:
        log("  包内前缀（自动）: %s/" % prefix)

    # ---- 收集条目
    excludes = [x.strip().strip("/") for x in args.exclude.split(",") if x.strip()]
    excludes += ["manifest.tsv", "links.tsv", "report.txt",
                 "pack-links.tsv", ".links_tmp", ".links"]
    entries = []
    dirs = 0
    for full, rel, kind in walk_safe(fsroot, excludes):
        arc = safe_rel(rel, lambda m: log("  [拒绝] " + m))
        if arc is None:
            continue
        if kind == "dir":
            dirs += 1
            entries.append((full, arc, "dir"))
        elif kind == "link":
            STATE["skipped_links"] += 1
            STATE["have_links"] = True      # 有链接就不允许“就地整目录打包”
            LINKS.append((rel, link_target_text(full), "skipped"))
        elif kind == "file":
            entries.append((full, arc, "file"))
        else:
            STATE["skipped_links"] += 1
            STATE["have_links"] = True
            LINKS.append((rel, "", "special"))

    if args.links == "text" and LINKS:
        # 把链接目标落成文本文件，绝不跟随读取
        txdir = tempfile.mkdtemp(prefix="titanlinks_")
        TMPDIRS.append(txdir)
        staged = []
        for i, (rel, t, _) in enumerate(LINKS):
            name = "%05d_" % i + rel.replace("/", "__") + ".link.txt"
            p = os.path.join(txdir, name)
            with open(p, "w", encoding="utf-8") as f:
                f.write(t)
            staged.append((p, rel + ".link.txt", "file"))
        entries += staged
        log("  链接目标已写成文本：%d 个 .link.txt" % len(staged))

    entries = [(f, r, k) for f, r, k in entries if k in ("file", "dir")]
    n_dirs = sum(1 for _, _, k in entries if k == "dir")
    n_files = sum(1 for _, _, k in entries if k == "file")
    n_bytes = 0
    for f, _, k in entries:
        if k == "file":
            try:
                n_bytes += os.path.getsize(f)
            except OSError:
                pass
    phase("收集完成：目录 %d，文件 %d，链接跳过 %d，合计 %s" %
          (n_dirs, n_files, STATE["skipped_links"], human(n_bytes)))

    if not args.out:
        log("[错误] 未指定 -o 输出文件")
        return 2

    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    fmt = args.format
    if fmt == "auto":
        low = out.lower()
        fmt = "zip" if low.endswith(".zip") else ("tar" if re.search(r"\.(tar|tgz|tar\.gz)$", low) else "7z")

    phase("打包 -> %s（%s%s）" %
        (out, fmt, "，分卷 " + args.volume if args.volume else ""))
    t0 = time.time()
    if fmt == "7z":
        ok = make_7z(out, entries, prefix, None, args.level, fsroot, excludes)
    elif fmt == "zip":
        ok = make_zip(out, entries, prefix)
    else:
        comp = "gz" if out.lower().endswith((".tar.gz", ".tgz")) else ""
        ok = make_tar(out, entries, prefix, comp)
    if not ok:
        return 2

    log("  完成：%d 个文件，%s，耗时 %.1fs" % (n_files, human(n_bytes), time.time() - t0))
    base = os.path.basename(out)
    total = 0
    for f in os.listdir(os.path.dirname(out) or "."):
        if f == base or (f.startswith(base + ".") and re.match(r"^\d{3}$", f[len(base) + 1:])):
            total += os.path.getsize(os.path.join(os.path.dirname(out) or ".", f))
    log("  压缩包大小：%s" % human(total))

    verify_archive(out, fmt, n_files, n_bytes)

    vol = volume_bytes(args.volume)
    if vol:
        n, _ = split_file(out, vol)
        log("  分卷：%d 片，每片 %s" % (n, human(vol)))
        log("  合并：cat %s.??? > %s" % (base, base))
        if n > 999:
            log("        片数超过 999，请改用：cat $(ls %s.??? | sort -V) > %s" % (base, base))

    note = os.path.join(os.path.dirname(out) or ".", "IMPORT.txt")
    try:
        write_import_note(note, fsroot, parts, (prefix + "/") if prefix else "")
        log("  导入说明：%s" % note)
    except OSError:
        pass

    for d in TMPDIRS:
        shutil.rmtree(d, ignore_errors=True)

    if LINKS:
        lp = os.path.join(os.path.dirname(out) or ".", "pack-links.tsv")
        with open(lp, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter="\t")
            w.writerow(["path", "target", "action"])
            w.writerows(LINKS)
        log("  符号链接明细：%s（%d 条，均未打进包）" % (lp, len(LINKS)))
    else:
        log("  自检：0 个符号链接，无穿透风险")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
titanunpack.py —— VPhoneGaga / 虚拟大师 "Titan FS" 一键解包器

一条命令走完：7z(含分卷)/zip/tar 解压 → 自动定位虚拟机 fs 根 → 自动发现全部分区
（data、cache、root、system、vendor、product …有多少认多少）→ 还原成真实目录树
+ 生成 manifest 清单。

用法:
    python3 titanunpack.py <输入> [-o 输出目录] [--list] [--only data,vendor]
                          [--no-raw] [--no-merge] [--keep-meta]

    输入可以是: xxx.7z / xxx.7z.001(分卷) / xxx.zip / xxx.tar.gz / 已解压的目录

输出结构:
    out/
      raw/                     原始解压内容（--no-raw 可关）
      by-partition/<分区>/      各分区还原后的目录树
      merged/                  Android 视角的合并树（/, /system, /vendor, /data …）
      manifest.tsv            每个文件的 分区/路径/inode/权限/uid/gid/大小/时间
      report.txt              识别结果与控制台日志

依赖:
    7z 支持需要 py7zr:  pip install py7zr
    （没有 py7zr 时，若系统装有 7z/7za 会自动改用命令行）
"""

VERSION = "2026.11.03"

import io
import os
import re
import stat
import posixpath
import sys
import csv
import json
import time
import mmap
import shutil
import struct
import tarfile
import zipfile
import argparse
import tempfile

MAGIC_SFDA = b"SFDA"
MAGIC_BPUS = b"BPUS"
MAGIC_ATIT = b"ATIT"
MAGIC_ATEM = b"ATEM"

RE_BUCKET = re.compile(r"^[0-9a-fA-F]{8}$")
RE_INODE = re.compile(r"^[0-9a-fA-F]{1,16}$")

# 已知分区 -> Android 挂载点
MOUNT = {
    "root": "/", "system": "/system", "vendor": "/vendor",
    "product": "/product", "system_ext": "/system_ext", "odm": "/odm",
    "data": "/data", "cache": "/cache", "metadata": "/metadata",
    "persist": "/persist", "sdcard": "/sdcard",
}

WIN_BAD = re.compile(r'[<>:"|?*\\/\x00-\x1f]')

# 链接策略：materialize=实体化(默认) / native=真符号链接 / skip=完全不落地
LINK_POLICY = "materialize"
LINK_RECORDS = []      # (分区, 路径, 目标, 处理方式, 说明)
_KEEP_OPEN = []        # 保持 mmap/文件句柄存活，避免 src 悬空


# ---------------------------------------------------------------- 日志

class Log:
    def __init__(self):
        self.lines = []

    def __call__(self, msg=""):
        print(msg, flush=True)
        self.lines.append(str(msg))

    def save(self, path):
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(self.lines))


log = Log()


# ---------------------------------------------------------------- 通用

def u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def u64(b, o):
    return struct.unpack_from("<Q", b, o)[0]


def open_buf(path):
    """大文件用 mmap 只读映射，小文件直接读。返回 (buffer, closer)"""
    f = open(path, "rb")
    size = os.fstat(f.fileno()).st_size
    if size == 0:
        f.close()
        return b"", lambda: None
    try:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)

        def closer():
            try:
                mm.close()
            finally:
                f.close()
        return mm, closer
    except (ValueError, OSError):
        data = f.read()
        f.close()
        return data, lambda: None


def magic_of(path, n=4):
    try:
        with open(path, "rb") as f:
            return f.read(n)
    except OSError:
        return b""


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0


def safe_name(name):
    name = WIN_BAD.sub("_", name).rstrip(" .")
    if name in ("", ".", ".."):
        name = "_" + name.replace(".", "")
    return name


# ---------------------------------------------------------------- 解压

def detect_archive(path):
    low = path.lower()
    if low.endswith((".7z", ".7z.001")) or re.search(r"\.7z\.\d{3}$", low):
        return "7z"
    if low.endswith(".zip"):
        return "zip"
    if low.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
        return "tar"
    # 按魔数兜底
    m = magic_of(path, 6)
    if m.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z"
    if m.startswith(b"PK\x03\x04"):
        return "zip"
    if m.startswith(b"\x1f\x8b") or m.startswith(b"BZh") or m.startswith(b"\xfd7zXZ"):
        return "tar"
    return None


SEVENZ_NAMES = ("7z", "7za", "7zz", "7zr", "7z.exe", "7za.exe", "7zz.exe")
SEVENZ_PATHS = (
    "/usr/bin/7z", "/usr/local/bin/7z", "/usr/lib/p7zip/7z",
    "/usr/libexec/p7zip/7z", "/opt/homebrew/bin/7z", "/opt/homebrew/bin/7zz",
    "/usr/local/bin/7zz", "/usr/bin/7zz", "/snap/bin/7z",
)


def sevenz_exe():
    for e in SEVENZ_NAMES:
        p = shutil.which(e)
        if p and os.access(p, os.X_OK):
            return p
    for p in SEVENZ_PATHS:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def split_parts(path):
    """xxx.7z.001 -> 同目录下按序排好的全部分片；不是分卷则返回 None

    7z 原生分卷和本工具切出来的 .001/.002 都是纯字节切片，直接拼接即可，
    所以两种来源用同一套处理。
    """
    m = re.match(r"^(.*\.7z)\.(\d{3,})$", path, re.I)
    if not m:
        return None
    base = os.path.basename(m.group(1))
    folder = os.path.dirname(os.path.abspath(path)) or "."
    found = []
    try:
        for f in os.listdir(folder):
            mm = re.match(re.escape(base) + r"\.(\d{3,})$", f, re.I)
            if mm and os.path.isfile(os.path.join(folder, f)):
                found.append((int(mm.group(1)), os.path.join(folder, f)))
    except OSError:
        return None
    if not found:
        return None
    found.sort()
    nums = [n for n, _ in found]
    if nums != list(range(1, len(nums) + 1)):
        log("  [警告] 分卷编号不连续: %s" % nums[:10])
    return [p for _, p in found]


def join_parts(parts, workdir):
    out = os.path.join(workdir, "_joined.7z")
    with open(out, "wb") as o:
        for p in parts:
            with open(p, "rb") as f:
                shutil.copyfileobj(f, o, 1 << 20)
    return out


def extract_7z(path, dest):
    parts = split_parts(path)
    work = None
    src = path
    try:
        if parts and len(parts) > 1:
            log("  检测到分卷: %d 片（%s … %s）" % (
                len(parts), os.path.basename(parts[0]), os.path.basename(parts[-1])))
            work = tempfile.mkdtemp(prefix="titanjoin_")
            src = join_parts(parts, work)
            log("  已拼接为 %.1f MB 的临时文件" % (os.path.getsize(src) / 1048576.0))

        exe = sevenz_exe()
        errors = []
        if exe:
            log("  使用命令行 %s 解压…" % exe)
            import subprocess
            r = subprocess.run([exe, "x", "-y", "-bd", "-o" + dest, src],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            if r.returncode == 0:
                return True
            errors.append("%s: %s" % (exe, r.stdout.decode("utf-8", "replace")[-400:]))
            log("  %s 解压失败，尝试 py7zr…" % exe)

        try:
            import py7zr
        except ImportError:
            py7zr = None
        if py7zr is not None:
            log("  使用 py7zr 解压…")
            with py7zr.SevenZipFile(src, mode="r") as z:
                log("  归档条目: %d" % len(z.getnames()))
                z.extractall(path=dest)
            return True
        errors.append("py7zr 未安装")

        log("  [错误] 7z 解压失败")
        for e in errors:
            log("         " + e.replace("\n", " ")[:300])
        log("         装 7z 命令行即可: apk add p7zip / apt install p7zip-full")
        return False
    except Exception as e:
        log("  [错误] 解压异常: %s: %s" % (type(e).__name__, e))
        return False
    finally:
        if work:
            shutil.rmtree(work, ignore_errors=True)


def extract_zip(path, dest):
    with zipfile.ZipFile(path) as z:
        bad = z.testzip()
        if bad:
            log("  [警告] zip 内 %s 校验失败，继续解压" % bad)
        z.extractall(dest)
        log("  归档条目: %d" % len(z.namelist()))
    return True


def extract_tar(path, dest):
    with tarfile.open(path) as t:
        t.extractall(dest)
        log("  归档条目: %d" % len(t.getnames()))
    return True


def unpack(src, dest):
    kind = detect_archive(src)
    if kind is None:
        log("[错误] 无法识别的归档格式: %s" % src)
        return False
    log("[1/4] 解压 %s（%s）" % (os.path.basename(src), kind))
    os.makedirs(dest, exist_ok=True)
    t0 = time.time()
    ok = {"7z": extract_7z, "zip": extract_zip, "tar": extract_tar}[kind](src, dest)
    if ok:
        log("  完成，用时 %.1fs" % (time.time() - t0))
    return ok


# ---------------------------------------------------------------- FS 识别

def find_fs_roots(root):
    """在解压目录里找虚拟机 fs 根：含 androidfs.bin 或含 >=2 个分区特征的目录"""
    cands = []
    for cur, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ("__MACOSX", ".git")]
        depth = cur[len(root):].count(os.sep)
        if depth > 6:
            dirs[:] = []
            continue
        if "androidfs.bin" in files and magic_of(os.path.join(cur, "androidfs.bin")) == MAGIC_SFDA:
            cands.append(cur)
            dirs[:] = []
            continue
        parts = 0
        for d in dirs:
            p = os.path.join(cur, d)
            sb = os.path.join(p, "superblock.bin")
            if os.path.exists(sb) and magic_of(sb) == MAGIC_BPUS:
                parts += 1
            elif os.path.exists(os.path.join(p, "fscache.bin")) or \
                 os.path.exists(os.path.join(p, "readonly.bin")):
                parts += 1
        if parts >= 2:
            cands.append(cur)
            dirs[:] = []
    # 去掉被包含的候选
    out = []
    for c in sorted(cands, key=len):
        if not any(c.startswith(o + os.sep) for o in out):
            out.append(c)
    return out


def is_partition_dir(p):
    if not os.path.isdir(p):
        return False
    sb = os.path.join(p, "superblock.bin")
    if os.path.exists(sb) and magic_of(sb) == MAGIC_BPUS:
        return True
    if os.path.exists(os.path.join(p, "fscache.bin")):
        return True
    if os.path.exists(os.path.join(p, "readonly.bin")):
        return True
    try:
        names = os.listdir(p)
    except OSError:
        return False
    for n in names:
        if RE_BUCKET.match(n) and os.path.isdir(os.path.join(p, n)):
            try:
                sub = os.listdir(os.path.join(p, n))
            except OSError:
                continue
            if any(RE_INODE.match(x.split(".")[0]) for x in sub):
                return True
    return False


def discover_partitions(fsroot):
    """返回 [(分区名, 路径)]，支持嵌套一层（如 vm1/fs/data）"""
    parts = []
    for entry in sorted(os.listdir(fsroot)):
        p = os.path.join(fsroot, entry)
        if not os.path.isdir(p):
            continue
        if is_partition_dir(p):
            parts.append((entry, p))
    if not parts:                     # 兜底：递归一层
        for entry in sorted(os.listdir(fsroot)):
            p = os.path.join(fsroot, entry)
            if not os.path.isdir(p):
                continue
            for sub in sorted(os.listdir(p)):
                q = os.path.join(p, sub)
                if is_partition_dir(q):
                    parts.append((os.path.join(entry, sub).replace(os.sep, "_"), q))
    return parts


# ---------------------------------------------------------------- 数据结构

class Node:
    __slots__ = ("ino", "mode", "uid", "gid", "nlink", "size", "mtime",
                 "ctime", "atime", "xattrs", "children", "src", "origin")

    def __init__(self, ino, mode=0):
        self.ino = ino
        self.mode = mode
        self.uid = self.gid = 0
        self.nlink = 1
        self.size = 0
        self.mtime = self.ctime = self.atime = 0
        self.xattrs = []
        self.children = {}
        self.src = None          # (buf, off, size) 或 bytes
        self.origin = ""

    @property
    def isdir(self):
        return stat.S_ISDIR(self.mode)

    @property
    def islink(self):
        return stat.S_ISLNK(self.mode)

    def read(self, limit=None):
        n = self.size if limit is None else min(self.size, limit)
        if isinstance(self.src, (bytes, bytearray)):
            return bytes(self.src[:n])
        if isinstance(self.src, tuple):
            buf, off, size = self.src
            n = min(n, size)
            if isinstance(buf, (bytes, bytearray, memoryview)):
                return bytes(buf[off:off + n])
            # buf 是文件路径（普通桶分区）：按需读，避免把整个文件读进内存
            if isinstance(buf, str):
                try:
                    with open(buf, "rb") as f:
                        f.seek(off)
                        return f.read(n)
                except OSError:
                    return b""
            return b""
        return b""


# ---------------------------------------------------------------- 结构解析

def _walk_entries(buf, q, elen):
    ents = []
    used = 0
    while used + 12 <= elen:
        try:
            cino = u64(buf, q)
            nl = u32(buf, q + 8)
        except struct.error:
            return None, 0
        if nl < 2 or nl > 256 or used + 12 + nl > elen + 3:
            return None, 0
        nm = bytes(buf[q + 12:q + 12 + nl - 1])
        if b"\0" in nm or any(c < 0x20 for c in nm):
            return None, 0
        ents.append((nm.decode("utf-8", "replace"), cino))
        q += 12 + nl
        used += 12 + nl
    if used + 3 < elen:
        return None, 0
    return ents, q


def parse_atem(blob):
    n = Node(-1, u32(blob, 8))
    n.uid, n.gid, n.nlink = u32(blob, 12), u32(blob, 16), u32(blob, 20)
    n.mtime, n.ctime = u32(blob, 24), u32(blob, 32)
    n.size = u32(blob, 40)
    xcnt = u32(blob, 48)
    p = 52
    b = bytes(blob)
    for _ in range(xcnt if xcnt < 64 else 0):
        e = b.find(b"\0", p)
        if e < 0:
            break
        name = b[p:e].decode("utf-8", "replace")
        p = e + 1
        vl = u32(b, p)
        p += 4
        n.xattrs.append((name, b[p:p + max(0, vl - 1)]))
        p += vl
    n.origin = "ATEM"
    return n


def parse_atit_pool(buf, total=None):
    """ATIT 记录池（分区里的 fscache.bin）"""
    end = total if total is not None else len(buf)
    nodes = {}
    pos = 0
    while pos + 84 <= end:
        if bytes(buf[pos:pos + 4]) != MAGIC_ATIT:
            pos += 4
            continue
        rlen = u32(buf, pos + 4)
        if rlen < 84 or pos + rlen > end:
            pos += 4
            continue
        mode = u32(buf, pos + 36)
        if mode == 0:                       # 池头/池尾超级块
            pos += rlen
            continue
        ino = u64(buf, pos + 20)
        n = Node(ino, mode)
        n.size = u64(buf, pos + 28)
        n.uid, n.gid, n.nlink = u32(buf, pos + 40), u32(buf, pos + 44), u32(buf, pos + 48)
        n.mtime, n.ctime, n.atime = u32(buf, pos + 52), u32(buf, pos + 60), u32(buf, pos + 76)
        xcnt = u32(buf, pos + 80)
        elen = u32(buf, pos + 68)
        xlen = u32(buf, pos + 64)
        p = pos + 84
        for _ in range(min(xcnt, 64)):
            e = bytes(buf[p:p + 512]).find(b"\0")
            if e < 0:
                break
            name = bytes(buf[p:p + e]).decode("utf-8", "replace")
            p += e + 1
            vl = u32(buf, p)
            p += 4
            n.xattrs.append((name, bytes(buf[p:p + max(0, vl - 1)])))
            p += vl
        if elen:
            cands = [pos + 80 + xlen]
            base = pos + rlen - elen
            cands += [base - k for k in range(4)]
            for q in cands:
                ents, endpos = _walk_entries(buf, q, elen)
                if ents is not None and endpos <= pos + rlen:
                    for nm, cino in ents:
                        n.children[nm] = cino
                    break
        n.origin = "ATIT"
        nodes[ino] = n
        pos += rlen
    return nodes


def parse_atit_image(path):
    """ATIT 索引镜像（readonly.bin）：64B 头 + N*52B 条目 + 数据/元数据区"""
    buf, closer = open_buf(path)
    nodes = {}
    try:
        if bytes(buf[:4]) != MAGIC_ATIT:
            return nodes
        count = u32(buf, 8)
        esz = u32(buf, 24) or 52
        if count > 1 << 22 or esz > 4096:
            count = 0
        for i in range(count):
            o = 0x40 + i * esz
            if o + esz > len(buf):
                break
            mode = u32(buf, o)
            n = Node(i, mode)
            n.uid, n.gid, n.nlink = u32(buf, o + 4), u32(buf, o + 8), u32(buf, o + 12)
            n.mtime, n.ctime = u32(buf, o + 16), u32(buf, o + 24)
            size, off, _crc, mlen = struct.unpack_from("<IIII", buf, o + 36)
            if off + size > len(buf):
                size = max(0, len(buf) - off)
            n.size = size
            n.src = (buf, off, size)
            n.origin = "readonly"
            if n.isdir and size:
                data = bytes(buf[off:off + size])
                q = 0
                while q + 12 <= len(data):
                    nl = u32(data, q)
                    if nl == 0 or q + 4 + nl + 8 > len(data):
                        break
                    nm = data[q + 4:q + 4 + nl - 1].decode("utf-8", "replace")
                    n.children[nm] = u64(data, q + 4 + nl)
                    q += 4 + nl + 8
            if mlen:
                xb = bytes(buf[off + size:off + size + mlen])
                if len(xb) >= 4:
                    xc = u32(xb, 0)
                    p = 4
                    for _ in range(min(xc, 64)):
                        e = xb.find(b"\0", p)
                        if e < 0:
                            break
                        name = xb[p:e].decode("utf-8", "replace")
                        p = e + 1
                        if p + 4 > len(xb):
                            break
                        vl = u32(xb, p)
                        p += 4
                        n.xattrs.append((name, xb[p:p + max(0, vl - 1)]))
                        p += vl
            nodes[i] = n
    finally:
        # mmap 仍被各节点的 src 引用，不能立刻 close；挂到全局列表等到进程退出
        _KEEP_OPEN.append((buf, closer))
    return nodes


# ---------------------------------------------------------------- 分区装载

class Partition:
    def __init__(self, name, path):
        self.name = name
        self.path = path
        self.nodes = {}
        self.nbytes = 0
        self.kind = "?"

    def load(self):
        # 1) 只读层镜像
        ro = os.path.join(self.path, "readonly.bin")
        if os.path.exists(ro) and magic_of(ro) == MAGIC_ATIT:
            self.nodes.update(parse_atit_image(ro))
            self.kind = "readonly"
        # 2) ATIT 记录池
        fc = os.path.join(self.path, "fscache.bin")
        if os.path.exists(fc) and magic_of(fc) == MAGIC_ATIT:
            buf, closer = open_buf(fc)
            try:
                recs = parse_atit_pool(buf, len(buf))
            finally:
                closer()
            for k, v in recs.items():
                self.nodes[k] = v
            self.kind = "record-pool" if self.kind == "?" else self.kind + "+pool"
        # 3) <bucket>/<inode>(.metadata)
        # 分两遍：先读 .metadata 拿到真实类型，再读内容文件——
        # 否则目录内容文件会先被当成普通文件占位，导致子项解析不出来。
        for want_meta in (True, False):
            for entry in sorted(os.listdir(self.path)):
                bdir = os.path.join(self.path, entry)
                if not (RE_BUCKET.match(entry) and os.path.isdir(bdir)):
                    continue
                for fn in sorted(os.listdir(bdir)):
                    fpath = os.path.join(bdir, fn)
                    if not os.path.isfile(fpath):
                        continue
                    base, ext = os.path.splitext(fn)
                    if ext == ".bak":
                        continue
                    if want_meta:
                        if ext != ".metadata":
                            continue
                    elif ext != "":
                        continue
                    try:
                        ino = int(base, 16)
                    except ValueError:
                        continue

                    if want_meta:
                        if magic_of(fpath) != MAGIC_ATEM:
                            continue
                        try:
                            n = parse_atem(open(fpath, "rb").read())
                        except Exception:
                            continue
                        n.ino = ino
                        old = self.nodes.get(ino)
                        if old is None:
                            self.nodes[ino] = n
                        else:                   # 记录池优先，元数据补空
                            if old.origin == "content":
                                # 内容文件阶段只是占位猜测，真实类型以元数据为准
                                old.mode = n.mode
                                old.mtime = n.mtime or old.mtime
                                old.ctime = n.ctime or old.ctime
                                old.nlink = n.nlink or old.nlink
                            old.uid = old.uid or n.uid
                            old.gid = old.gid or n.gid
                            if not old.xattrs:
                                old.xattrs = n.xattrs
                            if not old.children:
                                old.children = n.children
                    else:
                        n = self.nodes.get(ino)
                        if n is None:
                            n = Node(ino, stat.S_IFREG | 0o644)
                            n.origin = "content"
                            self.nodes[ino] = n
                        sz = os.path.getsize(fpath)
                        n.size = n.size or sz
                        n.src = (fpath, 0, sz)
                        self.nbytes += sz
                        if n.isdir and not n.children:
                            raw = open(fpath, "rb").read(1 << 20)
                            if b"=" in raw:
                                for line in raw.splitlines():
                                    if b"=" in line:
                                        nm, _, iv = line.partition(b"=")
                                        try:
                                            n.children[nm.decode("utf-8", "replace")] = int(iv)
                                        except ValueError:
                                            pass
        if self.kind == "?":
            self.kind = "plain"
        return self

    # 目录内容缺失时，用父记录的子表反推
    def root(self):
        return self.nodes.get(0)


def overlay(lower, upper):
    """lower 为只读层，upper 覆盖同名路径（并集语义）"""
    out = {}
    for k, v in lower.items():
        out[k] = v

    def merge(l_children, u_children):
        r = dict(l_children)
        r.update(u_children)
        return r

    def apply(ln, un, prefix, seen):
        for name, cino in un.children.items():
            child = upper.get(cino)
            if child is None:
                continue
            if name in ln.children:
                lc = lower.get(ln.children[name])
                if lc is not None and lc.isdir and child.isdir:
                    lc.children = merge(lc.children, child.children)
                    if prefix + "/" + name not in seen:
                        seen.add(prefix + "/" + name)
                        apply(lc, child, prefix + "/" + name, seen)
                    continue
            ln.children[name] = cino
            out[cino] = child

    lower_root = out.get(0)
    upper_root = upper.get(0)
    if lower_root and upper_root and lower_root.isdir:
        apply(lower_root, upper_root, "", set())
    return out


def resolve(nodes, ino=0):
    """把 inode 树整理成 path -> Node，并补全缺失目录"""
    res = {}

    def walk(i, prefix, seen):
        n = nodes.get(i)
        if n is None or not n.isdir or i in seen:
            return
        seen.add(i)
        for name, c in sorted(n.children.items()):
            child = nodes.get(c)
            p = prefix + "/" + name
            res[p] = child if child is not None else Node(c, stat.S_IFREG | 0o644)
            if child is not None and child.isdir:
                walk(c, p, seen)

    walk(ino, "", set())
    return res


# ---------------------------------------------------------------- 导出

MAX_LINK_HOPS = 8


def resolve_link(vpath, target, index):
    """把镜像内的链接目标换算成镜像内绝对路径。

    只在镜像内部换算，绝不落到宿主机路径：镜像里的 "/vendor/lib/x.so" 指的是
    本分区根下的 /vendor/lib/x.so，而不是宿主机的 /vendor。
    返回 (虚拟绝对路径 或 None, 失败原因)。
    """
    t = target.strip().replace("\\", "/")
    if not t:
        return None, "空目标"
    if "\x00" in t:
        return None, "目标含 NUL"
    if posixpath.isabs(t):
        cand = posixpath.normpath(t)
    else:
        cand = posixpath.normpath(posixpath.join(posixpath.dirname(vpath) or "/", t))
    if cand == "/":
        return "/", ""
    if cand == ".." or cand.startswith("../"):
        return None, "目标越出镜像根: %s" % t
    if not cand.startswith("/"):
        cand = "/" + cand
    return cand, ""


def write_file_data(n, path):
    """把节点内容写到磁盘（普通文件与实体化链接共用）"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    src = n.src
    if isinstance(src, tuple) and isinstance(src[0], str):
        # 延迟读取：内容还在分区的 inode 文件里
        fpath, off, size = src
        with open(fpath, "rb") as fin, open(path, "wb") as fout:
            fin.seek(off)
            left = size
            while left > 0:
                chunk = fin.read(min(left, 1 << 20))
                if not chunk:
                    break
                fout.write(chunk)
                left -= len(chunk)
        return
    with open(path, "wb") as f:
        if isinstance(src, tuple):
            buf, off, size = src
            left = size
            pos = off
            while left > 0:
                chunk = bytes(buf[pos:pos + min(left, 1 << 20)])
                if not chunk:
                    break
                f.write(chunk)
                pos += len(chunk)
                left -= len(chunk)
        else:
            f.write(n.read())


def handle_link(n, path, vpath, stats, index, part_name):
    """链接落地。默认 materialize：产出普通文件/目录，永不产出符号链接。

    - 目标解析到镜像内普通文件 -> 复制内容成普通文件
    - 目标解析到目录           -> 建真实目录
    - 解析不到/越界/特殊文件    -> 不落盘，只记进 links.tsv
    """
    target = n.read().decode("utf-8", "replace").rstrip("\x00")
    if LINK_POLICY == "skip":
        LINK_RECORDS.append((part_name, vpath, target, "skipped", "策略=skip"))
        stats["links"] += 1
        stats["link_skipped"] += 1
        return

    if LINK_POLICY == "native":
        # 显式要求保留链接时才做，且把绝对目标相对化，避免生成指向宿主机 /vendor 的穿透指针
        cand, why = resolve_link(vpath, target, index)
        if cand is None:
            LINK_RECORDS.append((part_name, vpath, target, "skipped", why))
            stats["links"] += 1
            stats["link_skipped"] += 1
            return
        rel = posixpath.relpath(cand, posixpath.dirname(vpath) or "/")
        try:
            if os.path.lexists(path):
                os.unlink(path)
            os.symlink(rel, path)
            LINK_RECORDS.append((part_name, vpath, target, "symlink", "相对化: " + rel))
            stats["links"] += 1
        except OSError as e:
            LINK_RECORDS.append((part_name, vpath, target, "skipped", str(e)))
            stats["links"] += 1
            stats["link_skipped"] += 1
        return

    # ---- materialize（默认）
    cur, hops, why = target, 0, ""
    cand = None
    while hops < MAX_LINK_HOPS:
        cand, why = resolve_link(vpath, cur, index)
        if cand is None:
            break
        tn = index.get(cand)
        if tn is None:
            why = "目标不在镜像内: %s" % cand
            break
        if not tn.islink:
            break
        cur = tn.read().decode("utf-8", "replace").rstrip("\x00")
        hops += 1
    tn = index.get(cand) if cand else None
    if tn is None:
        LINK_RECORDS.append((part_name, vpath, target, "skipped", why or "无法解析"))
        stats["links"] += 1
        stats["link_skipped"] += 1
        return
    if tn.isdir:
        os.makedirs(path, exist_ok=True)
        LINK_RECORDS.append((part_name, vpath, target, "dir", "实体化为目录 -> %s" % cand))
        stats["links"] += 1
    elif stat.S_ISREG(tn.mode):
        if os.path.lexists(path):
            os.unlink(path)
        write_file_data(tn, path)
        LINK_RECORDS.append((part_name, vpath, target, "file", "实体化为文件 -> %s" % cand))
        stats["links"] += 1
        stats["link_files"] += 1
        stats["bytes"] += tn.size
    else:
        LINK_RECORDS.append((part_name, vpath, target, "skipped",
                             "目标是特殊文件 %s" % oct(tn.mode)))
        stats["links"] += 1
        stats["link_skipped"] += 1


def write_node(n, path, stats, index=None, vpath="", part_name=""):
    try:
        if n.isdir:
            os.makedirs(path, exist_ok=True)
            stats["dirs"] += 1
        elif n.islink:
            handle_link(n, path, vpath, stats, index or {}, part_name)
            return
        elif stat.S_ISREG(n.mode):
            write_file_data(n, path)
            stats["files"] += 1
            stats["bytes"] += n.size
        else:
            stats["special"] += 1
            return
    except OSError as e:
        stats["errors"] += 1
        log("    [跳过] %s (%s)" % (path, e))
        return
    try:
        os.chmod(path, stat.S_IMODE(n.mode))
    except OSError:
        pass


def export_partition(nodes, dest, stats, part_name=""):
    paths = resolve(nodes, 0)
    for p in sorted(paths):
        n = paths[p]
        rel = "/".join(safe_name(x) for x in p.strip("/").split("/"))
        write_node(n, os.path.join(dest, rel), stats, paths, p, part_name)


def merge_view(mounts, dest, stats):
    """把各分区挂到 Android 视角的路径下（硬链接优先，失败则复制）。

    只处理真实文件：源里若有符号链接（native 策略产物）一律跳过，
    避免 os.link 顺着链接把宿主机上的文件硬链进输出目录。
    """
    for mnt, src in mounts.items():
        target = dest if mnt == "/" else os.path.join(dest, mnt.strip("/"))
        if not os.path.isdir(src):
            continue
        for cur, dirs, files in os.walk(src, followlinks=False):
            rel = os.path.relpath(cur, src)
            tdir = target if rel == "." else os.path.join(target, rel)
            os.makedirs(tdir, exist_ok=True)
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(cur, d))]
            for f in files:
                s = os.path.join(cur, f)
                if os.path.islink(s):
                    stats["skipped_links"] = stats.get("skipped_links", 0) + 1
                    continue
                d = os.path.join(tdir, f)
                if os.path.exists(d):
                    continue
                try:
                    os.link(s, d, follow_symlinks=False)
                    stats["links"] += 1
                except OSError:
                    try:
                        shutil.copy2(s, d)
                        stats["files"] += 1
                    except OSError:
                        pass


def scan_symlinks(root):
    """自检：统计输出目录里残留的符号链接"""
    bad = []
    for cur, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            p = os.path.join(cur, name)
            if os.path.islink(p):
                bad.append((p, os.readlink(p)))
    return bad


def dump_links(path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["partition", "path", "target", "action", "detail"])
        for r in LINK_RECORDS:
            w.writerow(r)


def dump_manifest(rows, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["partition", "path", "inode", "type", "mode", "uid", "gid",
                    "size", "mtime", "xattr_count", "selinux"])
        for r in rows:
            w.writerow(r)


# ---------------------------------------------------------------- 主流程

def process_fs(fsroot, outdir, args):
    log("[2/4] 定位虚拟机文件系统: %s" % fsroot)
    parts = discover_partitions(fsroot)
    if not parts:
        log("  [错误] 没找到任何 Titan FS 分区（缺 superblock.bin/fscache.bin）")
        return 1
    log("  发现 %d 个分区: %s" % (len(parts), ", ".join(n for n, _ in parts)))

    afs = os.path.join(fsroot, "androidfs.bin")
    if os.path.exists(afs):
        b = open(afs, "rb").read(64)
        log("  ROM 描述符: 版本 %d.%d.%d / build %d" %
            (u32(b, 4) // 100, (u32(b, 4) // 10) % 10, u32(b, 4) % 10, u32(b, 8)))

    log("[3/4] 解析分区")
    loaded = []
    for name, p in parts:
        if args.only and name not in args.only:
            continue
        t0 = time.time()
        part = Partition(name, p).load()
        nd = sum(1 for n in part.nodes.values() if n.isdir)
        nf = len(part.nodes) - nd
        log("  %-12s %-14s inode=%-7d 目录=%-5d 文件=%-6d 内容=%s (%.1fs)" %
            (name, part.kind, len(part.nodes), nd, nf, human(part.nbytes), time.time() - t0))
        loaded.append(part)

    if not loaded:
        log("  [错误] 指定 --only 后没有可处理的分区")
        return 1
    if args.list:
        log("[4/4] --list 模式：仅列目录，不写文件")
        for part in loaded:
            log("\n=== %s ===" % part.name)
            paths = resolve(part.nodes, 0)
            for p in sorted(paths):
                n = paths[p]
                log("  %s %s%s" % (stat.filemode(n.mode), p, "/" if n.isdir else ""))
            if not paths:
                log("  (空)")
        return 0

    log("[4/4] 导出（链接策略：%s）" % LINK_POLICY)
    rows = []
    mounts = {}
    for part in loaded:
        pdir = os.path.join(outdir, "by-partition", safe_name(part.name))
        stats = {"dirs": 0, "files": 0, "links": 0, "link_files": 0, "link_skipped": 0,
                 "bytes": 0, "errors": 0, "special": 0}
        export_partition(part.nodes, pdir, stats, part.name)
        log("  %-12s -> %s  目录=%d 文件=%d 链接=%d(实体化%d/跳过%d) 错误=%d" %
            (part.name, pdir, stats["dirs"], stats["files"], stats["links"],
             stats["link_files"], stats["link_skipped"], stats["errors"]))
        mounts[MOUNT.get(part.name.lower(), "/" + part.name)] = pdir
        for p, n in resolve(part.nodes, 0).items():
            sel = ""
            for k, v in n.xattrs:
                if k == "security.selinux":
                    sel = v.decode("utf-8", "replace")
            typ = "dir" if n.isdir else ("link" if n.islink else
                                         ("file" if stat.S_ISREG(n.mode) else "special"))
            rows.append([part.name, p, n.ino, typ, oct(n.mode), n.uid, n.gid,
                         n.size, time.strftime("%Y-%m-%d %H:%M:%S",
                                               time.localtime(n.mtime or 0)) if n.mtime else "",
                         len(n.xattrs), sel])

    if not args.no_merge:
        mstats = {"files": 0, "links": 0}
        merge_view(mounts, os.path.join(outdir, "merged"), mstats)
        log("  合并视图 -> merged/（%d 个硬链接）" % mstats["links"])

    dump_manifest(rows, os.path.join(outdir, "manifest.tsv"))
    log("  清单 -> manifest.tsv（%d 条）" % len(rows))
    if LINK_RECORDS:
        dump_links(os.path.join(outdir, "links.tsv"))
        acts = {}
        for r in LINK_RECORDS:
            acts[r[3]] = acts.get(r[3], 0) + 1
        log("  链接 -> links.tsv（%s）" %
            ", ".join("%s=%d" % kv for kv in sorted(acts.items())))

    # 自检：输出目录里不允许出现任何符号链接（防止指向虚拟机外的宿主路径）
    leaks = scan_symlinks(outdir)
    if leaks:
        log("  [警告] 输出目录存在 %d 个符号链接，已清理：" % len(leaks))
        for p, t in leaks[:10]:
            log("     %s -> %s" % (os.path.relpath(p, outdir), t))
            try:
                os.unlink(p)
            except OSError:
                pass
    else:
        log("  自检：输出目录 0 个符号链接（无穿透风险）")
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="VPhoneGaga Titan FS 一键解包（自动识别 7z/zip/tar 与全部分区）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n  python titanunpack.py fs.7z -o out\n"
               "  python titanunpack.py fs.7z.001 -o out --list\n"
               "  python titanunpack.py 已解压的fs目录 -o out --only data,vendor")
    ap.add_argument("src", help="7z/7z.001/zip/tar 或已解压目录")
    ap.add_argument("-o", "--out", default="unpacked", help="输出目录（默认 ./unpacked）")
    ap.add_argument("--version", action="version", version="titanunpack " + VERSION)
    ap.add_argument("--list", action="store_true", help="只列出目录树，不导出文件")
    ap.add_argument("--only", default="", help="只处理指定分区，逗号分隔")
    ap.add_argument("--no-raw", action="store_true", help="解压后删除 raw/ 原始内容")
    ap.add_argument("--no-merge", action="store_true", help="不生成 merged/ 合并视图")
    ap.add_argument("--keep-meta", action="store_true", help="保留 .metadata 等原始文件到 raw/")
    ap.add_argument("--fresh", action="store_true",
                    help="丢弃 raw/ 与旧输出，强制重新解压（默认会比对归档大小/时间，变了自动重解）")
    ap.add_argument("--links", default="materialize",
                    choices=["materialize", "native", "skip"],
                    help="符号链接策略：materialize=复制成真实文件(默认，不产生符号链接)；"
                         "native=生成相对化符号链接；skip=不落地仅记录")
    args = ap.parse_args()
    global LINK_POLICY
    LINK_POLICY = args.links
    args.only = [x.strip() for x in args.only.split(",") if x.strip()]

    src = os.path.abspath(args.src)
    outdir = os.path.abspath(args.out)
    if not os.path.exists(src):
        log("[错误] 输入不存在: %s" % src)
        return 2
    os.makedirs(outdir, exist_ok=True)
    log("=== Titan FS 一键解包  v%s ===" % VERSION)
    log("输入: %s" % src)
    log("输出: %s" % outdir)

    raw = os.path.join(outdir, "raw")
    stamp = os.path.join(outdir, ".archive.stamp")
    if os.path.isdir(src):
        log("[1/4] 输入为目录，跳过解压")
        workroot = src
    else:
        sig = "%s|%d|%d" % (os.path.basename(src), os.path.getsize(src),
                            int(os.path.getmtime(src)))
        old = ""
        if os.path.exists(stamp):
            try:
                old = open(stamp, encoding="utf-8").read().strip()
            except OSError:
                old = ""
        reuse = False
        if os.path.isdir(raw) and os.listdir(raw):
            if args.fresh:
                log("[1/4] --fresh：丢弃旧 raw/ 与旧输出，重新解压")
                shutil.rmtree(raw, ignore_errors=True)
                for sub in ("by-partition", "merged", "vm1", "vm2", "vm3"):
                    p = os.path.join(outdir, sub)
                    if os.path.isdir(p):
                        shutil.rmtree(p, ignore_errors=True)
            elif old and old != sig:
                log("[1/4] 归档已变化，丢弃旧 raw/ 重新解压")
                log("      %s" % (old or "（无记录）"))
                log("   -> %s" % sig)
                shutil.rmtree(raw, ignore_errors=True)
            elif not old:
                log("[1/4] raw/ 存在但无归档指纹，按旧行为复用（--fresh 强制重解）")
                reuse = True
            else:
                log("[1/4] 复用已有 raw/（归档未变）")
                reuse = True
        if not reuse and not unpack(src, raw):
            return 2
        if not reuse:
            try:
                with open(stamp, "w", encoding="utf-8") as f:
                    f.write(sig)
            except OSError:
                pass
        workroot = raw

    roots = find_fs_roots(workroot)
    if not roots:
        log("[错误] 没找到 fs 根（未见 androidfs.bin / superblock.bin）")
        log("       请确认压缩包里是 App 私有目录下的 fs 文件夹")
        return 1
    log("  候选 fs 根: %d 个" % len(roots))

    rc = 0
    for i, r in enumerate(roots):
        sub = outdir if len(roots) == 1 else os.path.join(outdir, "vm%d" % (i + 1))
        log("\n--- 实例 %d/%d: %s" % (i + 1, len(roots), r))
        rc = process_fs(r, sub, args) or rc

    if not args.keep_meta and not args.no_raw and os.path.isdir(raw) and not args.list:
        pass  # raw 保留，便于二次分析；用户可 --no-raw 关闭

    log.save(os.path.join(outdir, "report.txt"))
    log("\n完成。输出: %s" % outdir)
    return rc


if __name__ == "__main__":
    sys.exit(main())

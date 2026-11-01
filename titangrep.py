#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
titangrep.py —— 在 Titan FS 镜像 / 7z 包 / 解包目录里搜字符串（自动穿透 APK）

用来回答一个问题：我改的那处文字，到底进包了没有？

用法
----
  python3 titangrep.py fs.7z "存储ZZ"
  python3 titangrep.py unpacked/raw "存储空间管理器"
  python3 titangrep.py fs.7z "存储ZZ" --apk Settings     # 只搜文件名含 Settings 的
  python3 titangrep.py fs.7z "存储ZZ" --max-mb 0         # 不限单文件大小

说明
----
- 同时按 UTF-8 / UTF-16LE / UTF-16BE / GBK 四种编码匹配，中文不会漏；
- 遇到 APK/ZIP 会递归进内部成员（resources.arsc、classes.dex、res/...）；
- 命中 0 次 = 改动确实没进包；命中多次 = 有多份副本，系统可能读了另一份。

退出码：0 有命中，1 无命中，2 出错。
"""

VERSION = "2026.11.01"

import os
import sys
import shutil
import argparse
import zipfile
import tempfile
import subprocess

ENCODINGS = ("utf-8", "utf-16-le", "utf-16-be", "gbk")
ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06")
SEVENZ_NAMES = ("7z", "7za", "7zz", "7zr")


def log(msg=""):
    print(msg, flush=True)


def human(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return ("%d %s" % (n, u)) if u == "B" else ("%.1f %s" % (n, u))
        n /= 1024.0


def find_7z():
    for n in SEVENZ_NAMES:
        p = shutil.which(n)
        if p:
            return p
    for p in ("/usr/bin/7z", "/usr/lib/p7zip/7z", "/opt/homebrew/bin/7zz"):
        if os.path.exists(p):
            return p
    return None


def unpack(src, work):
    """把 7z/zip 解到临时目录，返回镜像根"""
    low = src.lower()
    if low.endswith(".7z") or low.endswith(".7z.001"):
        exe = find_7z()
        if exe:
            log("解压中（7z）…")
            r = subprocess.run([exe, "x", src, "-o" + work, "-y", "-bso0", "-bsp0"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if r.returncode != 0:
                raise RuntimeError(r.stderr.decode("utf-8", "replace")[:400])
            return work
        try:
            import py7zr
        except ImportError:
            raise RuntimeError("没有 7z 命令也没有 py7zr：apk add p7zip / apt install p7zip-full")
        log("解压中（py7zr）…")
        with py7zr.SevenZipFile(src) as z:
            z.extractall(work)
        return work
    if low.endswith(".zip"):
        log("解压中（zip）…")
        with zipfile.ZipFile(src) as z:
            z.extractall(work)
        return work
    raise RuntimeError("不认识的输入：%s" % src)


def encode_needles(words):
    out = []
    seen = set()
    for w in words:
        for enc in ENCODINGS:
            try:
                b = w.encode(enc)
            except UnicodeEncodeError:
                continue
            if b not in seen:
                seen.add(b)
                out.append((b, enc))
    return out


def context(data, pos, enc="utf-8", n=56):
    a = max(0, pos - n)
    b = min(len(data), pos + n)
    if enc.startswith("utf-16"):
        a -= a % 2
        b -= b % 2
    seg = data[a:b]
    for e in (enc, "utf-8", "gbk"):
        try:
            s = seg.decode(e)
        except (UnicodeDecodeError, LookupError):
            continue
        s = "".join(ch if ch.isprintable() else " " for ch in s).strip()
        if s:
            return s[:100]
    s = "".join(chr(c) if 32 <= c < 127 else " " for c in seg)
    return s.strip()[:100]


def scan_blob(data, label, needles, hits, limit=6):
    found = 0
    for nd, enc in needles:
        start = 0
        while True:
            i = data.find(nd, start)
            if i < 0:
                break
            hits.append((label, i, context(data, i, enc), nd))
            found += 1
            start = i + len(nd)
            if found >= limit * len(needles):
                return
    return


CHUNK = 8 << 20


def scan_stream(f, label, needles, hits):
    """大文件流式搜索：内存恒定，跨块边界不漏"""
    keep = max(len(n) for n, _ in needles) + 16
    buf = b""
    base = 0
    while True:
        chunk = f.read(CHUNK)
        if not chunk:
            break
        data = buf + chunk
        for nd, enc in needles:
            i = data.find(nd)
            while i >= 0:
                hits.append((label, base + i, context(data, i, enc), nd))
                i = data.find(nd, i + len(nd))
        newbuf = data[-keep:]
        base += len(data) - len(newbuf)
        buf = newbuf


def scan_file(path, label, needles, hits, depth=0, max_member=None):
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    try:
        with open(path, "rb") as f:
            head = f.read(4)
            if head[:2] == b"PK" and depth < 3:
                f.seek(0)
                try:
                    z = zipfile.ZipFile(f)
                except zipfile.BadZipFile:
                    z = None
                if z is not None:
                    with z:
                        for info in z.infolist():
                            if info.file_size > (max_member or (512 << 20)):
                                continue
                            try:
                                sub = z.read(info)
                            except Exception:
                                continue
                            scan_blob(sub, "%s!%s" % (label, info.filename),
                                      needles, hits)
                    return
            f.seek(0)
            if size <= CHUNK * 2:
                scan_blob(f.read(), label, needles, hits)
            else:
                log("  流式搜索 %s（%.0f MB）…" % (label, size / 1048576.0))
                scan_stream(f, label, needles, hits)
    except OSError:
        return


def walk(root, filt):
    for cur, dirs, files in os.walk(root):
        dirs.sort()
        for fn in sorted(files):
            full = os.path.join(cur, fn)
            if os.path.islink(full):
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if filt and filt.lower() not in rel.lower():
                continue
            yield full, rel


def main():
    ap = argparse.ArgumentParser(description="Titan FS 镜像字符串搜索（穿透 APK）")
    ap.add_argument("src", help="fs.7z / 解包目录 / raw 镜像目录")
    ap.add_argument("words", nargs="+", help="要搜的文字，可多个")
    ap.add_argument("--apk", default="", help="只搜路径含该关键字的文件，如 Settings")
    ap.add_argument("--max-mb", type=int, default=0,
                    help="APK 内部单个成员读取上限（MB），0=不限；大文件一律流式搜索")
    ap.add_argument("--version", action="version", version="titangrep " + VERSION)
    args = ap.parse_args()

    src = os.path.abspath(args.src)
    if not os.path.exists(src):
        log("[错误] 不存在: %s" % src)
        return 2

    needles = encode_needles(args.words)
    log("=== Titan FS 字符串搜索  v%s ===" % VERSION)
    log("输入: %s" % src)
    log("关键词: %s（%d 种编码变体）" % (" / ".join(args.words), len(needles)))

    work = None
    root = src
    try:
        if os.path.isfile(src):
            work = tempfile.mkdtemp(prefix="titangrep_")
            root = unpack(src, work)
        # 若只给了包根，自动下钻到镜像根
        for cand in ("raw", "androidfs_10.0.0"):
            p = os.path.join(root, cand)
            if os.path.isdir(p) and cand == "raw":
                root = p
                break

        total = 0
        files = 0
        for full, rel in walk(root, args.apk):
            files += 1
            total += os.path.getsize(full)
        log("待搜: %d 个文件，%s" % (files, human(total)))
        if args.apk:
            log("过滤: 路径含 %r" % args.apk)
        log("")

        hits = []
        for full, rel in walk(root, args.apk):
            scan_file(full, rel, needles, hits,
                      max_member=(args.max_mb << 20) if args.max_mb else None)

        if not hits:
            log("[结果] 0 命中 —— 这些文字不在这个包里。")
            log("       说明你的改动没有进包：大概率改的是 by-partition 导出副本，")
            log("       或者改的文件在 readonly.bin 只读层里（那里没有独立文件可改）。")
            return 1

        log("[结果] %d 处命中：" % len(hits))
        seen = set()
        for label, off, ctx, _nd in hits:
            key = (label, off)
            if key in seen:
                continue
            seen.add(key)
            where = "" if off < 0 else " @%d" % off
            log("  %s%s" % (label, where))
            if ctx:
                log("      …%s…" % ctx)
        log("")
        log("提示：命中多处 = 存在多份副本，系统实际加载的可能不是你以为的那一份。")
        return 0
    except Exception as e:
        log("[错误] %s" % e)
        return 2
    finally:
        if work:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

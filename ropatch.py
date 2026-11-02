#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ropatch.py —— 改写 readonly.bin（ATIT 只读层镜像），可直接作用于 fs.7z

/system 这类分区整个内联在 readonly.bin 里，没有独立文件可改。
本工具直接操作 ATIT 容器：定位条目、导出、替换、校验 CRC。

用法
----
直接对 ROM 压缩包（推荐，不用先解包）：
  python3 ropatch.py fs.7z info                      # 列出包内所有只读层
  python3 ropatch.py fs.7z info --part system        # 看 system 的条目统计
  python3 ropatch.py fs.7z find --part system "com.android.settings"
  python3 ropatch.py fs.7z extract --part system 12 -o old.apk
  python3 ropatch.py fs.7z replace --part system 12 改好的.apk --yes
  python3 ropatch.py fs.7z verify --part system      # 全量 CRC 复验

也可以直接对解包出来的文件操作：
  python3 ropatch.py info    system/readonly.bin
  python3 ropatch.py replace system/readonly.bin 12 改好的.apk --yes

ROM 模式说明
------------
7z 是固实压缩流，不能随机读写。所以流程是：把 readonly.bin 解到临时目录 →
改 → 用 `7z u` 写回（会重建整个压缩包，1.2 GB 级别约几分钟）。
临时文件在结束后自动清理。加 --keep 可保留，--no-writeback 只改不留。

条目结构（52 字节，来自 root/readonly.bin 实测）
------------------------------------------------
  +0  mode  +4 uid  +8 gid  +12 nlink
  +16 mtime +20 mtime2 +24 ctime +28 ctime2
  +32 flag(0x50000000)  +36 size  +40 data_off  +44 crc32  +48 meta_len

替换策略
--------
新文件 ≤ 原文件 → 原位覆写 + 尾部补零，文件总大小不变（最安全）
新文件 > 原文件 → 追加到末尾，原 xattr 块一起搬过去，更新条目与头部字段
"""

VERSION = "2026.11.06"

import os
import io
import sys
import zlib
import stat
import bisect
import argparse
import struct
import shutil
import zipfile
import time
import hashlib
import tempfile
import subprocess

MAGIC = b"ATIT"
HDR_LEN = 0x40
DEF_ESZ = 52
CHUNK = 8 << 20


def log(msg=""):
    print(msg, flush=True)


def human(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return ("%d %s" % (n, u)) if u == "B" else ("%.1f %s" % (n, u))
        n /= 1024.0


def parse_size(s, default=0):
    """'1M' / '512k' / '2G' -> 字节数"""
    if not s:
        return default
    t = str(s).strip().upper()
    mul = 1
    for suf, m in (("K", 1 << 10), ("M", 1 << 20), ("G", 1 << 30)):
        if t.endswith(suf):
            mul = m
            t = t[:-1]
            break
    try:
        return int(float(t) * mul)
    except ValueError:
        return default


def u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


class Image:
    def __init__(self, path):
        self.path = path
        self.size = os.path.getsize(path)
        with open(path, "rb") as f:
            self.head = f.read(HDR_LEN + 64 * 1024)
        if self.head[:4] != MAGIC:
            raise ValueError("不是 ATIT 镜像（头部魔数 %r）" % self.head[:4])
        self.hdr_len = u32(self.head, 4) or HDR_LEN
        self.count = u32(self.head, 8)
        self.used_end = u32(self.head, 12)
        self.last_off = u32(self.head, 16)
        self.seed = u32(self.head, 20)
        self.esz = u32(self.head, 24) or DEF_ESZ
        need = self.hdr_len + self.count * self.esz
        if need > self.size:
            raise ValueError("条目表越界：需要 %d 字节，文件只有 %d" % (need, self.size))
        if len(self.head) < need:
            with open(path, "rb") as f:
                f.seek(0)
                self.head = f.read(need)

    def entry_off(self, i):
        return self.hdr_len + i * self.esz

    def entry(self, i):
        o = self.entry_off(i)
        b = self.head
        mode, uid, gid, nlink = struct.unpack_from("<IIII", b, o)
        mtime, mtime2, ctime, ctime2 = struct.unpack_from("<IIII", b, o + 16)
        flag, size, off, crc, mlen = struct.unpack_from("<IIIII", b, o + 32)
        return {"i": i, "mode": mode, "uid": uid, "gid": gid, "nlink": nlink,
                "mtime": mtime, "ctime": ctime, "flag": flag, "size": size,
                "off": off, "crc": crc, "mlen": mlen, "at": o}

    def entries(self):
        for i in range(self.count):
            e = self.entry(i)
            if e["off"] == 0 and e["size"] == 0:
                continue
            yield e

    def read(self, e):
        with open(self.path, "rb") as f:
            f.seek(e["off"])
            return f.read(e["size"])

    def xattr(self, e):
        if not e["mlen"]:
            return b""
        with open(self.path, "rb") as f:
            f.seek(e["off"] + e["size"])
            return f.read(e["mlen"])


SEVENZ_NAMES = ("7z", "7za", "7zz", "7zr", "7z.exe", "7za.exe")
SEVENZ_PATHS = ("/usr/bin/7z", "/usr/lib/p7zip/7z", "/opt/homebrew/bin/7zz",
                "/data/data/com.termux/files/usr/bin/7z")


def find_7z():
    for n in SEVENZ_NAMES:
        p = shutil.which(n)
        if p:
            return p
    for p in SEVENZ_PATHS:
        if os.path.exists(p):
            return p
    return None


def is_archive(path):
    low = path.lower()
    return low.endswith(".7z") or low.endswith(".7z.001")


def list_archive(exe, archive):
    """返回 [{'path': 成员路径, 'size': 字节数}]"""
    r = subprocess.run([exe, "l", "-slt", archive],
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    items = []
    cur = None
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("Path = "):
            if cur and cur.get("path"):
                items.append(cur)
            cur = {"path": line[7:].strip(), "size": 0}
        elif line.startswith("Size = ") and cur is not None:
            try:
                cur["size"] = int(line[7:].strip())
            except ValueError:
                pass
    if cur and cur.get("path"):
        items.append(cur)
    return items


class Rom:
    """把 fs.7z 里的 readonly.bin 当虚拟文件用：按需解出、改完写回"""

    def __init__(self, path, part="", keep=False, no_writeback=False, fresh=False):
        self.path = os.path.abspath(path)
        self.part = part.strip("/")
        self.keep = keep
        self.no_writeback = no_writeback
        self.exe = find_7z()
        if not self.exe:
            raise ValueError("需要 7z 命令：Alpine 用 apk add p7zip，Debian 用 apt install p7zip-full")
        members = [it for it in list_archive(self.exe, self.path)
                   if os.path.basename(it["path"]).lower() == "readonly.bin"]
        if not members:
            raise ValueError("包内没有 readonly.bin，这个镜像可能没有只读层")
        if part:
            want = part.strip("/").lower()
            members = [m for m in members
                       if want in [s.lower() for s in m["path"].split("/")]]
        if not members:
            raise ValueError("包内没有分区 %r 的 readonly.bin" % part)
        self.members = members
        self.fresh = fresh
        self.written = False
        self.tmp = self._cache_dir(path)
        self.dirty = []

    def _cache_dir(self, path):
        """同一个包多次操作只解压一次：缓存 key 由路径+大小+mtime 决定"""
        self.cache_new = False
        try:
            key = "%s|%d|%d" % (os.path.abspath(path),
                                os.path.getsize(path),
                                int(os.path.getmtime(path)))
        except OSError:
            return tempfile.mkdtemp(prefix="ropatch_")
        h = hashlib.md5(key.encode("utf-8")).hexdigest()[:12]
        d = os.path.join(tempfile.gettempdir(), "ropatch_cache", h)
        self.cache_new = not os.path.isdir(d)
        if self.fresh and os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
            self.cache_new = True
        os.makedirs(d, exist_ok=True)
        return d

    @staticmethod
    def part_of(member_path):
        seg = member_path.split("/")
        return seg[-2] if len(seg) >= 2 else ""

    def show_members(self):
        log("%-10s %-12s %s" % ("分区", "原大小", "包内路径"))
        for m in self.members:
            log("%-10s %-12s %s" % (self.part_of(m["path"]),
                                    human(m["size"]), m["path"]))
        log("")
        log("共 %d 个只读层。用 --part <分区> 指定要操作哪个。" % len(self.members))
        return 0

    def pick(self):
        if self.part:
            return self.members[0]["path"]
        if len(self.members) > 1:
            raise ValueError("包内有 %d 个只读层，请用 --part 指定：%s" % (
                len(self.members), ", ".join(self.part_of(m["path"]) for m in self.members)))
        return self.members[0]["path"]

    def image(self, member):
        local = os.path.join(self.tmp, member)
        if not os.path.exists(local):
            log("解出 %s（%s，需要一点时间）…" % (
                member, human([m for m in self.members if m["path"] == member][0]["size"])))
            r = subprocess.run([self.exe, "x", self.path, "-o" + self.tmp, "-y", member],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if r.returncode != 0 or not os.path.exists(local):
                raise ValueError("解压失败：%s" % r.stderr.decode("utf-8", "replace")[:300])
            log("  -> %s" % local)
        return Image(local)

    def mark(self, member):
        if member not in self.dirty:
            self.dirty.append(member)

    def writeback(self):
        if not self.dirty or self.no_writeback:
            if self.dirty:
                log("[--no-writeback] 改动只留在临时目录：%s" % self.tmp)
            return 0
        for m in self.dirty:
            log("写回 %s（重建压缩包，1.2 GB 级别约几分钟）…" % m)
            r = subprocess.run([self.exe, "u", self.path, m], cwd=self.tmp,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if r.returncode != 0:
                log("[错误] 写回失败：%s" % r.stderr.decode("utf-8", "replace")[:300])
                return 1
            log("  ✓ 已更新 %s" % m)
        self.dirty = []
        self.written = True
        log("写回完成：%s（%s）" % (self.path, human(os.path.getsize(self.path))))
        log("建议复验：python3 ropatch.py %s verify --part %s"
            % (os.path.basename(self.path), self.part or "<分区>"))
        return 0

    def cleanup(self):
        if self.keep:
            log("临时目录保留：%s" % self.tmp)
            return
        if self.written:        # 已写回，缓存作废
            shutil.rmtree(self.tmp, ignore_errors=True)
            return
        size = 0
        for cur, _d, fs in os.walk(self.tmp):
            for f in fs:
                try:
                    size += os.path.getsize(os.path.join(cur, f))
                except OSError:
                    pass
        if size and self.cache_new:
            log("缓存已保留（%s），下次同一命令直接复用；清理用 --fresh 或删 %s"
                % (human(size), self.tmp))


def isdir(mode):
    return (mode & 0o170000) == 0o040000


def kind(mode):
    t = mode & 0o170000
    return {0o040000: "dir", 0o100000: "file", 0o120000: "link"}.get(t, "other")


def cmd_info(img):
    log("文件      : %s" % img.path)
    log("大小      : %s" % human(img.size))
    log("头长      : %#x" % img.hdr_len)
    log("条目数    : %d" % img.count)
    log("条目大小  : %d" % img.esz)
    log("已用末尾  : %#x (%s)" % (img.used_end, human(img.used_end)))
    log("最后数据  : %#x" % img.last_off)
    log("种子/校验 : %#x" % img.seed)
    log("数据区    : %#x .. %#x" % (img.hdr_len + img.count * img.esz, img.used_end))
    files = dirs = 0
    tot = 0
    for e in img.entries():
        if isdir(e["mode"]):
            dirs += 1
        else:
            files += 1
            tot += e["size"]
    log("内容      : 目录 %d，文件 %d，文件合计 %s" % (dirs, files, human(tot)))
    if img.used_end < img.size:
        log("尾部空闲  : %s（够不够放新文件，看这个）" % human(img.size - img.used_end))
    else:
        log("尾部空闲  : 0（替换会让文件变大）")
    return 0


def cmd_list(img, min_size, kindf):
    rows = [e for e in img.entries()]
    if min_size:
        rows = [e for e in rows if e["size"] >= min_size]
    if kindf:
        rows = [e for e in rows if kind(e["mode"]) == kindf]
    rows.sort(key=lambda e: -e["size"])
    log("%-6s %-6s %-10s %-10s %-6s %s" % ("idx", "类型", "大小", "偏移", "mlen", "crc32"))
    for e in rows:
        log("%-6d %-6s %-10s %#-10x %-6d %08x" % (
            e["i"], kind(e["mode"]), human(e["size"]), e["off"], e["mlen"], e["crc"]))
    log("")
    log("共 %d 条" % len(rows))
    return 0


def cmd_find(img, needle):
    """在数据区里搜字节串，报告命中的是哪一个条目"""
    pats = []
    for enc in ("utf-8", "utf-16-le"):
        try:
            pats.append((needle.encode(enc), enc))
        except UnicodeEncodeError:
            pass
    # 条目按数据偏移排序；排序键必须只含数字，否则 dict 之间无法比较
    ivs = sorted(((e["off"], e["off"] + e["size"], e) for e in img.entries()),
                 key=lambda t: (t[0], t[1]))
    starts = [t[0] for t in ivs]

    def locate(pos):
        k = bisect.bisect_right(starts, pos) - 1
        if k >= 0:
            a, b, e = ivs[k]
            if a <= pos < b:
                return e
        return None

    log("搜索 %r（条目 %d 个，文件 %s）…" % (needle, len(ivs), human(img.size)))
    hits = []
    cap = 200000
    stop = False
    with open(img.path, "rb") as f:
        base = 0
        buf = b""
        keep = 4096
        while not stop:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            data = buf + chunk
            for pat, _enc in pats:
                i = data.find(pat)
                while i >= 0:
                    e = locate(base + i)
                    if e is not None:
                        hits.append((base + i, e))
                        if len(hits) >= cap:
                            stop = True
                            break
                    i = data.find(pat, i + len(pat))
                if stop:
                    break
            nb = data[-keep:]
            base += len(data) - len(nb)
            buf = nb
    if stop:
        log("[提示] 命中数达到 %d 上限，已停止；换更长的关键字能缩小范围" % cap)
    if not hits:
        log("[结果] 0 命中 —— 这串字节不在这个只读层里")
        log("       最常见的原因：目标文件是 zip/deflate 压缩的（APK、jar），明文搜不到。")
        log("       改成按包名找：")
        log("         python3 ropatch.py <镜像> apks --part <分区> --pkg %s" % needle)
        return 1
    seen = {}
    for pos, e in hits:
        seen.setdefault(e["i"], (e, pos))
    log("[结果] %d 处命中，涉及 %d 个条目：" % (len(hits), len(seen)))
    for i, (e, pos) in sorted(seen.items()):
        log("  条目 %-4d  %-6s 大小 %-10s 偏移 %#-10x 文件内位置 %#x" % (
            i, kind(e["mode"]), human(e["size"]), e["off"], pos))
    return 0


class SubFile(io.RawIOBase):
    """把大文件里的 [start, start+size) 段包成一个独立可 seek 的文件对象"""

    def __init__(self, f, start, size):
        self.f = f
        self.start = start
        self.size = size
        self.pos = 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        if whence == 0:
            p = off
        elif whence == 1:
            p = self.pos + off
        else:
            p = self.size + off
        self.pos = max(0, min(p, self.size))
        return self.pos

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        n = min(n, self.size - self.pos)
        if n <= 0:
            return b""
        self.f.seek(self.start + self.pos)
        b = self.f.read(n)
        self.pos += len(b)
        return b

    def readinto(self, b):
        d = self.read(len(b))
        b[:len(d)] = d
        return len(d)


def _name_chars(ch):
    return (48 <= ch <= 57) or (65 <= ch <= 90) or (97 <= ch <= 122) or ch in (46, 95)


def guess_pkg(man):
    """从二进制 AndroidManifest.xml 的字符串池里捞 com.* 包名"""
    out = []
    for enc, step in (("utf-16-le", 2), ("utf-8", 1)):
        pat = b"com." if step == 1 else "com.".encode("utf-16-le")
        i = man.find(pat)
        while i >= 0:
            chars = []
            j = i
            while j + step <= len(man):
                if step == 2:
                    ch = man[j] | (man[j + 1] << 8)
                else:
                    ch = man[j]
                if not _name_chars(ch):
                    break
                chars.append(chr(ch))
                j += step
            name = "".join(chars)
            if name.count(".") >= 2 and len(name) >= 6 and name not in out:
                out.append(name)
            i = man.find(pat, i + step)
    out.sort(key=len)
    return out


def cmd_apks(img, min_size, max_probe, want, limit):
    """列出只读层里的 zip 容器（APK/JAR），可只解中央目录读包名"""
    ents = [e for e in img.entries()
            if not isdir(e["mode"]) and e["size"] >= min_size]
    if not ents:
        log("没有大于 %s 的文件条目，用 --min-size 调小" % human(min_size))
        return 1
    log("扫描 %d 个候选（>= %s，单个体积上限 %s）…"
        % (len(ents), human(min_size), human(max_probe)))
    hits, others = [], []
    zips = 0
    with open(img.path, "rb") as f:
        for e in ents:
            f.seek(e["off"])
            if f.read(2) != b"PK":
                continue
            zips += 1
            pkg = ""
            members = 0
            if e["size"] <= max_probe:
                try:
                    z = zipfile.ZipFile(SubFile(f, e["off"], e["size"]))
                    with z:
                        names = z.namelist()
                        members = len(names)
                        if "AndroidManifest.xml" in names:
                            cands = guess_pkg(z.read("AndroidManifest.xml"))
                            pkg = cands[0] if cands else ""
                except Exception:
                    pass
            row = (e["i"], e["size"], pkg, members)
            if want:

                if pkg and want.lower() in pkg.lower():
                    hits.append(row)
                elif not pkg:
                    others.append(row)
                continue
            others.append(row)
    rows = hits if want else others
    rows.sort(key=lambda r: -r[1])
    if want and not hits:
        log("[结果] 没有包名含 %r 的 zip 条目" % want)
        if others:
            log("       有 %d 个条目没能读出包名（体积超限或非标准 zip），下面列出来备查："
                % len(others))
            rows = others
    elif want:
        log("[结果] %d 个匹配：" % len(hits))
    else:
        log("[结果] %d 个 zip 容器（共扫描 %d 个候选）：" % (zips, len(ents)))
    log("")
    log("%-6s %-10s %-6s %s" % ("条目", "大小", "成员", "包名"))
    for i, size, pkg, members in rows[:limit]:
        log("%-6d %-10s %-6d %s" % (i, human(size), members, pkg or "（未探测）"))
    if len(rows) > limit:
        log("... 另有 %d 个，用 --limit 放宽" % (len(rows) - limit))
    if rows:
        log("")
        log("下一步：python3 ropatch.py <镜像> extract --part <分区> %d -o 导出.apk" % rows[0][0])
    return 0 if rows else 1


def cmd_extract(img, idx, out):
    e = img.entry(idx)
    if e["size"] == 0:
        log("[错误] 条目 %d 没有内容" % idx)
        return 2
    data = img.read(e)
    crc = zlib.crc32(data) & 0xFFFFFFFF
    if out:
        with open(out, "wb") as f:
            f.write(data)
        log("已导出条目 %d -> %s（%s）" % (idx, out, human(len(data))))
    if crc != e["crc"]:
        log("[警告] CRC 不符：条目 %08x，实算 %08x（原数据可能已损坏）" % (e["crc"], crc))
    else:
        log("CRC 校验通过：%08x" % crc)
    return 0


def _parse_layout(data, align, ino_first):
    """按一种布局尝试解析，返回 (子项, 已消费字节)"""
    out = []
    p = 0
    n = len(data)
    while p + 12 <= n:
        if ino_first:
            ino = struct.unpack_from("<Q", data, p)[0]
            nlen = struct.unpack_from("<I", data, p + 8)[0]
            npos = p + 12
        else:
            nlen = struct.unpack_from("<I", data, p)[0]
            npos = p + 4
            if nlen < 2 or nlen > 255 or npos + nlen + 8 > n:
                break
            ino = struct.unpack_from("<Q", data, npos + nlen)[0]
        if nlen < 2 or nlen > 255 or npos + nlen > n:
            break
        raw = data[npos:npos + nlen]
        try:
            name = raw.rstrip(b"\0").decode("utf-8")
        except UnicodeDecodeError:
            break
        if not name or "\0" in name or any(ord(c) < 32 for c in name):
            break
        out.append((ino, name))
        p = npos + nlen + (0 if ino_first else 8)
        if align:
            p = (p + (align - 1)) & ~(align - 1)
    return out, p


def parse_dirents_ex(data, align=4, ino_first=True):
    """解析目录项数组，返回 (子项, 对齐, 已消费字节, 是否 inode 在前)

    只读层里存在两种布局，必须都试：
      ino_first=True   u64 子inode + u32 名长(含\\0) + 名字   （记录池 fscache.bin 的写法）
      ino_first=False  u32 名长(含\\0) + 名字 + u64 子inode   （只读索引镜像 readonly.bin 的写法）
    """
    items, end = _parse_layout(data, align, ino_first)
    return items, align, end, ino_first


def pack_dirents(items, align=4, ino_first=True):
    """按指定布局重新编码目录项数组"""
    out = b""
    for ino, name in items:
        b = name.encode("utf-8") + b"\0"
        if ino_first:
            rec = struct.pack("<Q", ino) + struct.pack("<I", len(b)) + b
        else:
            rec = struct.pack("<I", len(b)) + b + struct.pack("<Q", ino)
        if align:
            rec += b"\0" * ((-len(rec)) % align)
        out += rec
    return out


def dir_children(img, e):
    """解析目录条目的子项，返回 (items, 对齐, 已消费字节, 是否 inode 在前)"""
    if not isdir(e["mode"]) or not e["size"]:
        return [], 4, 0, True
    data = img.read(e)
    best = None
    for ino_first in (True, False):
        for al in (4, 0):
            got, end = _parse_layout(data, al, ino_first)
            if not got:
                continue
            fit = 1 if end >= len(data) - 3 else 0
            sane = 1 if all(0 <= i < img.count for i, _ in got) else 0
            # 打平时优先 len+name+ino（readonly 索引镜像的实际布局）
            score = (fit, sane, len(got), 0 if ino_first else 1)
            if best is None or score > best[0]:
                best = (score, got, al, end, ino_first)
    if best is None:
        return [], 4, 0, True
    return best[1], best[2], best[3], best[4]


def detect_layout(img, limit=4096):
    """空目录没有数据可供判断，用分区内其它非空目录的布局作为默认值"""
    for i in range(min(img.count, limit)):
        e = img.entry(i)
        if isdir(e["mode"]) and e["size"]:
            items, al, _used, ino_first = dir_children(img, e)
            if items:
                return ino_first, al
    return True, 4


def resolve_path(img, path, root=0, quiet=False, trace=None):
    """把 /system/app 这类路径解析成条目号，返回 (idx, 实际走过的路径)"""
    segs = [s for s in path.replace("\\", "/").split("/") if s]
    if not segs:
        return root, "/"
    idx = root
    trail = ""
    for seg in segs:
        if not (0 <= idx < img.count):
            raise ValueError("条目 %d 越界（共 %d 条），路径 %r 不可达" % (idx, img.count, path))
        e = img.entry(idx)
        if not isdir(e["mode"]):
            raise ValueError("%r 指向 %s（条目 %d），不是目录"
                             % (trail or "/", kind(e["mode"]), idx))
        items = dir_children(img, e)[0]
        cand = [t for t in items if t[1] == seg]
        if not cand:
            cand = [t for t in items if t[1].lower() == seg.lower()]
            if cand and not quiet:
                log("  [提示] %r 实际写作 %r" % (seg, cand[0][1]))
        if not cand:
            near = [t[1] for t in items][:12]
            raise ValueError("目录 %r 里没有 %r%s"
                             % (trail or "/", seg,
                                "；现有子项：" + "、".join(near) if near else "（目录为空）"))
        idx = cand[0][0]
        trail += "/" + cand[0][1]
        if trace is not None and 0 <= idx < img.count:
            trace.append((trail, idx, img.entry(idx)))
    return idx, trail


def strip_part_prefix(img, path):
    """只读层的根就是分区根，manifest 里的 /system/app 在镜像里是 /app"""
    segs = [s for s in path.replace("\\", "/").split("/") if s]
    if len(segs) < 2:
        return ""
    return "/" + "/".join(segs[1:])


def locate_dir(img, val):
    """父目录参数：纯数字当条目号，其余当路径。返回 (idx, 说明)"""
    v = (val or "").strip()
    if not v:
        raise ValueError("父目录不能为空")
    if v.isdigit():
        i = int(v)
        if not (0 <= i < img.count):
            raise ValueError("条目 %d 越界（共 %d 条）" % (i, img.count))
        e = img.entry(i)
        if not isdir(e["mode"]):
            raise ValueError("条目 %d 是 %s，不是目录" % (i, kind(e["mode"])))
        return i, "条目 %d" % i
    path = v if v.startswith("/") else "/" + v
    try:
        idx, trail = resolve_path(img, path, quiet=True)
        return idx, "路径 %s" % trail
    except ValueError as e1:
        alt = strip_part_prefix(img, path)
        if alt:
            try:
                idx, trail = resolve_path(img, alt, quiet=True)
                log("  [提示] %s 不在层级里，按分区内路径 %s 解析（只读层的根就是该分区根）"
                    % (path, trail))
                return idx, "路径 %s" % trail
            except ValueError as e2:
                raise ValueError("路径不存在，两种解释都没命中：\n"
                                 "         完整路径 %s：%s\n"
                                 "         分区内   %s：%s\n"
                                 "       只读层的根就是该分区根，所以 /system/app 在这里是 /app"
                                 % (path, e1, alt, e2))
        raise ValueError(str(e1))


def cmd_path(img, path):
    """只解析路径，打印每一层和最终目录的子项"""
    p = path if path.startswith("/") else "/" + path
    log("[解析] %s" % p)
    trace = []
    try:
        idx, trail = resolve_path(img, p, quiet=True, trace=trace)
    except ValueError as e1:
        alt = strip_part_prefix(img, p)
        if not alt:
            log("[错误] %s" % e1)
            return 1
        log("  [提示] %s 不在层级里，按分区内路径 %s 解析（只读层的根就是该分区根）" % (p, alt))
        trace = []
        try:
            idx, trail = resolve_path(img, alt, quiet=True, trace=trace)
        except ValueError as e2:
            log("[错误] 路径不存在，两种解释都没命中：")
            log("         完整路径 %s：%s" % (p, e1))
            log("         分区内   %s：%s" % (alt, e2))
            return 1
    for t, ino, ch in trace:
        log("  %-32s -> 条目 %-5d %s  %s" % (t, ino, stat.filemode(ch["mode"]), human(ch["size"])))
    e = img.entry(idx)
    if not isdir(e["mode"]):
        log("")
        log("最终是文件，不是目录（%s，条目 %d）。" % (human(e["size"]), idx))
        log("要替换它：ropatch.py <镜像> replace --part <分区> %d <新文件> --yes" % idx)
        return 0
    items = dir_children(img, e)[0]
    log("")
    log("目录 %s = 条目 %d，子项 %d 个：" % (trail or "/", idx, len(items)))
    for ino, nm in items[:30]:
        ch = img.entry(ino) if 0 <= ino < img.count else None
        mark = "" if ch is None or not isdir(ch["mode"]) else "/"
        log("  %-36s ino=%-6d %s" % (nm + mark, ino, stat.filemode(ch["mode"]) if ch else "?"))
    if len(items) > 30:
        log("  ... 另有 %d 个" % (len(items) - 30))
    log("")
    log("下一步：ropatch.py <镜像> add --part <分区> %s <文件名> <本地文件> --yes" % (trail or "/"))
    return 0


def cmd_tree(img, path, depth, limit):
    """打印目录树（只读层没有名字索引，只能逐层解析）"""
    try:
        idx, trail = locate_dir(img, path or "/")
    except ValueError as e:
        log("[错误] %s" % e)
        return 2
    log("%s  (条目 %d)" % (trail or "/", idx))
    seen = set()

    def walk(i, prefix, d):
        if d >= depth or i in seen:
            return 0
        seen.add(i)
        e = img.entry(i)
        items = dir_children(img, e)[0]
        if len(items) > limit:
            shown = items[:limit]
        else:
            shown = items
        n = 0
        for k, (ino, nm) in enumerate(shown):
            last = (k == len(shown) - 1) and len(items) <= limit
            ch = img.entry(ino) if 0 <= ino < img.count else None
            if ch is None:
                log("%s%s%s  [缺失 inode %d]" % (prefix, "└─ " if last else "├─ ", nm, ino))
                continue
            enc = "目录" if isdir(ch["mode"]) else human(ch["size"])
            log("%s%s%s  (%s, ino=%d)" % (prefix, "└─ " if last else "├─ ", nm, enc, ino))
            n += 1
            if isdir(ch["mode"]):
                n += walk(ino, prefix + ("   " if last else "│  "), d + 1)
        if len(items) > limit:
            log("%s└─ ... 另有 %d 项（--limit 放宽）" % (prefix, len(items) - limit))
        return n

    total = walk(idx, "", 0)
    log("")
    log("共 %d 项（深度上限 %d）" % (total, depth))
    return 0


def cmd_show(img, idx, limit, maxx):
    """完整展示一个条目：权限、时间、crc、xattr 十六进制、目录子项"""
    e = img.entry(idx)
    log("条目 %d" % idx)
    log("  类型    : %s" % kind(e["mode"]))
    log("  权限    : %s  (mode=%#o)" % (stat.filemode(e["mode"]), e["mode"]))
    log("  uid/gid : %d / %d   nlink=%d" % (e["uid"], e["gid"], e["nlink"]))
    log("  大小    : %s" % human(e["size"]))
    log("  偏移    : %#x" % e["off"])
    log("  crc32   : %08x" % e["crc"])
    log("  flag    : %#x" % e["flag"])
    log("  mtime   : %d   ctime: %d" % (e["mtime"], e["ctime"]))
    log("  xattr   : %d 字节" % e["mlen"])
    xb = img.xattr(e)
    if xb:
        prev = None
        for i in range(0, min(len(xb), maxx), 16):
            chunk = xb[i:i + 16]
            hexs = " ".join("%02x" % b for b in chunk)
            txt = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            log("    %04x  %-47s  |%s|" % (i, hexs, txt))
            prev = i
        if len(xb) > maxx:
            log("    ... 共 %d 字节，只显示前 %d" % (len(xb), maxx))
        elif prev is not None:
            log("    （完整 xattr 块 %d 字节）" % len(xb))
    if isdir(e["mode"]) and e["size"]:
        data = img.read(e)
        got, al, used, ino_first = dir_children(img, e)
        layout = "ino+len+name" if ino_first else "len+name+ino"
        log("  子项    : %d 个（数据区 %d 字节，已解析 %d，布局 %s，对齐 %d）"
            % (len(got), len(data), used, layout, al))
        for ino, name in got[:limit]:
            log("    %-40s ino=%d" % (name, ino))
        if len(got) > limit:
            log("    ... 另有 %d 个，用 --limit 放宽" % (len(got) - limit))
        if not got:
            log("    （解析不出目录项，可能是别的编码，用 extract 导出后手工看）")
    return 0


def cmd_add(img, diridx, name, newfile, yes, backup, xattr_from):
    """往某个目录里挂一个新文件。会重写整个容器：条目表 +1，数据区整体后移。"""
    if not name or "/" in name or name in (".", ".."):
        log("[错误] 文件名非法：%r" % name)
        return 2
    d = img.entry(diridx)
    if not isdir(d["mode"]):
        log("[错误] 条目 %d 是 %s，不是目录" % (diridx, kind(d["mode"])))
        return 2
    try:
        fsize = os.path.getsize(newfile)
    except OSError as e:
        log("[错误] 读不到 %s：%s" % (newfile, e))
        return 2

    old = img.read(d) if d["size"] else b""
    items, al, used, ino_first = dir_children(img, d)
    if not items and not d["size"]:
        ino_first, al = detect_layout(img)
        log("  [推断] 目标目录为空，沿用分区内其它目录的布局 %s，对齐 %d"
            % ("ino+len+name" if ino_first else "len+name+ino", al))
    for ino, nm in items:
        if nm == name:
            log("[错误] 目录里已经有 %r（条目 %d），替换请用 replace" % (name, ino))
            return 2

    complete = (used >= len(old) - 3)
    ordered = all(items[k][1] <= items[k + 1][1] for k in range(len(items) - 1))
    newino = img.count
    if complete and ordered:
        merged = sorted(items + [(newino, name)], key=lambda t: t[1])
        newdir = pack_dirents(merged, al, ino_first)
        how = "保持有序，按名字插入"
    else:
        newdir = old + pack_dirents([(newino, name)], al, ino_first)
        how = "原目录项无法完整解析或无排序，追加到末尾"

    crc_new = 0
    with open(newfile, "rb") as nf:
        while True:
            b = nf.read(CHUNK)
            if not b:
                break
            crc_new = zlib.crc32(b, crc_new)
    crc_new &= 0xFFFFFFFF

    xb_dir = img.xattr(d)
    tpl = img.entry(xattr_from) if xattr_from >= 0 else None
    xb_new = img.xattr(tpl) if tpl is not None else xb_dir

    esz = img.esz
    old_start = img.hdr_len + img.count * esz
    end = img.used_end or img.size
    remain = max(0, min(end, img.size) - old_start)
    dir_off = end + esz
    file_off = dir_off + len(newdir) + len(xb_dir)
    used_end = file_off + fsize + len(xb_new)
    grow = used_end - img.size

    log("目标目录 : 条目 %d  %s  子项 %d 个，数据 %s"
        % (diridx, stat.filemode(d["mode"]), len(items), human(len(old))))
    log("新条目   : 条目 %d  %s  (%s)" % (newino, name, human(fsize)))
    log("目录项   : %d -> %d 个，数据 %s -> %s（%s）"
        % (len(items), len(items) + 1, human(len(old)), human(len(newdir)), how))
    log("          布局 %s，对齐 %d，已解析 %d/%d 字节"
        % ("ino+len+name" if ino_first else "len+name+ino", al, used, len(old)))
    log("条目表   : 条目表 %d -> %d 项（数据区整体后移 %d B）"
        % (img.count, img.count + 1, esz))
    log("         : 父目录数据 -> %#x，新文件 -> %#x" % (dir_off, file_off))
    log("容器     : %s -> %s（+%s）" % (human(img.size), human(used_end), human(grow)))
    log("xattr    : 新文件 %d 字节（%s）"
        % (len(xb_new), "来自条目 %d" % xattr_from if tpl else "复制父目录"))
    fsc = os.path.join(os.path.dirname(img.path), "fscache.bin")
    if os.path.exists(fsc):
        log("[注意]   同分区还有 fscache.bin（%s）"
            % human(os.path.getsize(fsc)))
        log("         它是 inode 记录池，可能是元数据的权威来源；本工具只改 readonly.bin，")
        log("         若导入后新文件不可见，需要再同步记录池（或改用引擎自己建目录的方式）。")
    if not yes:
        log("")
        log("[演练] 未写入。加 --yes 真正执行（会写一个 %s 的临时文件再原子替换）"
            % human(used_end))
        return 0

    if backup:
        bak = img.path + ".bak"
        log("备份 -> %s" % bak)
        shutil.copy2(img.path, bak)

    table = bytearray((img.count + 1) * esz)
    for i in range(img.count):
        o = img.entry_off(i)
        table[i * esz:(i + 1) * esz] = img.head[o:o + esz]
    for i in range(img.count):
        off = struct.unpack_from("<I", table, i * esz + 40)[0]
        if off:
            struct.pack_into("<I", table, i * esz + 40, off + esz)
    struct.pack_into("<I", table, diridx * esz + 36, len(newdir))
    struct.pack_into("<I", table, diridx * esz + 40, dir_off)
    struct.pack_into("<I", table, diridx * esz + 44, zlib.crc32(newdir) & 0xFFFFFFFF)

    now = int(time.time())
    uid, gid = (tpl["uid"], tpl["gid"]) if tpl else (d["uid"], d["gid"])
    o = img.count * esz
    struct.pack_into("<IIII", table, o, 0o100644, uid, gid, 1)
    struct.pack_into("<IIII", table, o + 16, now, 0, now, 0)
    struct.pack_into("<IIIII", table, o + 32, 0x50000000, fsize, file_off, crc_new, len(xb_new))

    hdr = bytearray(img.head[:img.hdr_len])
    struct.pack_into("<I", hdr, 8, img.count + 1)
    struct.pack_into("<I", hdr, 12, used_end)
    struct.pack_into("<I", hdr, 16, file_off)

    tmp = img.path + ".newadd"
    written = 0
    try:
        with open(img.path, "rb") as src, open(tmp, "wb") as dst:
            dst.write(hdr)
            dst.write(table)
            written = len(hdr) + len(table)
            src.seek(old_start)
            left = remain
            while left > 0:
                b = src.read(min(CHUNK, left))
                if not b:
                    break
                dst.write(b)
                written += len(b)
                left -= len(b)
            dst.write(newdir)
            dst.write(xb_dir)
            written += len(newdir) + len(xb_dir)
            with open(newfile, "rb") as nf:
                while True:
                    b = nf.read(CHUNK)
                    if not b:
                        break
                    dst.write(b)
                    written += len(b)
            dst.write(xb_new)
            written += len(xb_new)
        if written != used_end:
            log("[错误] 写出的长度 %d 与预期 %d 不符，已放弃" % (written, used_end))
            os.unlink(tmp)
            return 2
        os.replace(tmp, img.path)
    except OSError as e:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        log("[错误] 写入失败：%s" % e)
        return 2

    log("")
    log("✓ 已追加条目 %d（%s）到条目 %d" % (newino, name, diridx))
    log("  新文件大小 %s，容器 %s" % (human(fsize), human(os.path.getsize(img.path))))
    log("  建议复验：ropatch.py <镜像> show --part <分区> %d" % diridx)
    return 0


def cmd_replace(img, idx, newfile, yes, backup, align):
    e = img.entry(idx)
    if isdir(e["mode"]):
        log("[错误] 条目 %d 是目录，不能替换内容" % idx)
        return 2
    new = open(newfile, "rb").read()
    crc = zlib.crc32(new) & 0xFFFFFFFF
    xb = img.xattr(e)
    log("条目 %d：%s -> %s（xattr 块 %d 字节）" % (idx, human(e["size"]), human(len(new)), len(xb)))
    if not yes:
        log("[演练] 未写入。确认无误后加 --yes")
        return 0
    if backup:
        bak = img.path + ".bak"
        log("备份 -> %s" % bak)
        shutil.copy2(img.path, bak)

    if len(new) <= e["size"]:
        # 原位覆写：文件总大小不变，最安全（引擎若按固定 size 读也不会出错）
        with open(img.path, "r+b") as f:
            f.seek(e["off"])
            f.write(new)
            f.write(b"\0" * (e["size"] - len(new)))
            f.seek(e["at"] + 36)
            f.write(struct.pack("<II", len(new), e["off"]))
            f.seek(e["at"] + 44)
            f.write(struct.pack("<I", crc))
        log("✓ 原位覆写：off=%#x size=%d crc=%08x（文件大小不变，尾部补零）" % (
            e["off"], len(new), crc))
        log("")
        return 0

    with open(img.path, "r+b") as f:
        f.seek(0, 2)
        end = f.tell()
        new_off = end
        if align:
            pad = (-new_off) % align
            if pad:
                f.write(b"\0" * pad)
                new_off += pad
        f.seek(new_off)
        f.write(new)
        if xb:
            f.write(xb)
        # 条目：size(+36) data_off(+40) crc(+44) meta_len(+48)
        f.seek(e["at"] + 36)
        f.write(struct.pack("<IIII", len(new), new_off, crc, len(xb)))
        # 头：used_end(+12) last_data_off(+16)
        f.seek(12)
        f.write(struct.pack("<I", new_off + len(new) + len(xb)))
        f.seek(16)
        f.write(struct.pack("<I", new_off))
    log("✓ 已写入：data_off=%#x size=%d crc=%08x" % (new_off, len(new), crc))
    log("  原数据留在 %#x（空洞，不再被引用）" % e["off"])
    log("  文件大小：%s -> %s" % (human(img.size), human(os.path.getsize(img.path))))
    log("")
    return 0


def cmd_verify(img, limit):
    bad = 0
    n = 0
    for e in img.entries():
        if isdir(e["mode"]):
            continue
        n += 1
        data = img.read(e)
        crc = zlib.crc32(data) & 0xFFFFFFFF
        if crc != e["crc"]:
            bad += 1
            log("  ✗ 条目 %-4d 声明 %08x 实算 %08x  size=%d off=%#x" % (
                e["i"], e["crc"], crc, e["size"], e["off"]))
            if bad >= limit:
                log("  …（已达 --limit，停止）")
                break
    log("")
    if bad == 0:
        log("✓ 全部 %d 个文件条目 CRC 校验通过" % n)
        return 0
    log("✗ %d / %d 条不符" % (bad, n))
    return 1


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--part", default="",
                        help="ROM 模式：指定分区，如 system / vendor / data")
    common.add_argument("--keep", action="store_true", help="保留临时解出的 readonly.bin")
    common.add_argument("--no-writeback", action="store_true",
                        help="改完不写回 7z，只改临时副本")
    common.add_argument("--fresh", action="store_true",
                        help="丢弃解压缓存，重新从 7z 解出")

    ap = argparse.ArgumentParser(
        description="readonly.bin（ATIT 只读层）改写工具，可直接作用于 fs.7z",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join([
            "示例:",
            "  python3 ropatch.py fs.7z info",
            "  python3 ropatch.py fs.7z info --part system",
            "  python3 ropatch.py fs.7z apks --part system --pkg com.android.settings",
            "  python3 ropatch.py fs.7z tree --part system / --depth 2",
            "  python3 ropatch.py fs.7z path --part system /system/app",
            "  python3 ropatch.py fs.7z show --part system 1234",
            "  python3 ropatch.py fs.7z find --part system com.android.settings",
            "  python3 ropatch.py fs.7z replace --part system 123 改好的.apk --yes",
            "  python3 ropatch.py fs.7z add --part system /system/app NewApp.apk ./NewApp.apk --yes",
            "  python3 ropatch.py fs.7z verify --part system"]))
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("info", parents=[common], help="查看头部与条目统计（ROM 模式不带 --part 则列出所有只读层）")
    p.add_argument("image")

    p = sub.add_parser("list", parents=[common], help="按大小列出条目")
    p.add_argument("image")
    p.add_argument("--min-size", default="", help="只看大于该大小的条目，如 1M")
    p.add_argument("--kind", default="", choices=["", "file", "dir", "link"])

    p = sub.add_parser("find", parents=[common], help="在只读层里搜字符串，定位所属条目")
    p.add_argument("image")
    p.add_argument("needle")

    p = sub.add_parser("apks", parents=[common],
                       help="列出只读层里的 zip 容器（APK/JAR），可按包名筛")
    p.add_argument("image")
    p.add_argument("--min-size", default="1M", help="只看大于该大小的条目，默认 1M")
    p.add_argument("--max-probe", default="128M",
                   help="超过该大小的条目不做包名解析，默认 128M")
    p.add_argument("--pkg", default="", help="只列包名含该串的条目，如 com.android.settings")
    p.add_argument("--limit", type=int, default=40)

    p = sub.add_parser("extract", parents=[common], help="导出某个条目")
    p.add_argument("image")
    p.add_argument("idx", type=int)
    p.add_argument("-o", "--out", default="")

    p = sub.add_parser("show", parents=[common],
                       help="完整展示一个条目：权限、时间、xattr、目录子项")
    p.add_argument("image")
    p.add_argument("idx", type=int)
    p.add_argument("--limit", type=int, default=50, help="目录子项最多显示多少个")
    p.add_argument("--xattr-bytes", type=int, default=512, help="xattr 十六进制最多显示多少字节")

    p = sub.add_parser("path", parents=[common],
                       help="把 /system/app 这类路径解析成条目号")
    p.add_argument("image")
    p.add_argument("path")

    p = sub.add_parser("tree", parents=[common],
                       help="打印目录树（逐层解析目录项）")
    p.add_argument("image")
    p.add_argument("path", nargs="?", default="/")
    p.add_argument("--depth", type=int, default=2, help="最大深度，默认 2")
    p.add_argument("--limit", type=int, default=40, help="每层最多显示多少项")

    p = sub.add_parser("add", parents=[common],
                       help="在某个目录下新增一个文件条目（重写整个容器）")
    p.add_argument("image")
    p.add_argument("dir", help="父目录：条目号（如 12）或路径（如 /system/app）")
    p.add_argument("name", help="新文件名，如 NewApp.apk")
    p.add_argument("file", help="要写入的内容文件")
    p.add_argument("--yes", action="store_true", help="真的写入")
    p.add_argument("--backup", action="store_true", help="写前整文件备份为 .bak（ROM 模式不适用）")
    p.add_argument("--xattr-from", type=int, default=-1,
                   help="从该条目复制 xattr 块，默认用父目录的")

    p = sub.add_parser("replace", parents=[common], help="替换某个条目的内容")
    p.add_argument("image")
    p.add_argument("idx", type=int)
    p.add_argument("newfile")
    p.add_argument("--yes", action="store_true", help="真的写入")
    p.add_argument("--backup", action="store_true", help="写前整文件备份为 .bak（ROM 模式不适用）")
    p.add_argument("--align", type=int, default=0, help="追加前按该字节数对齐，如 4096")

    p = sub.add_parser("verify", parents=[common], help="全量 CRC 校验")
    p.add_argument("image")
    p.add_argument("--limit", type=int, default=20)

    ap.add_argument("--version", action="version", version="ropatch " + VERSION)
    # 允许 "ropatch.py fs.7z info" 这种写法：把开头的镜像路径挪到子命令参数末尾
    CMDS = ("info", "list", "find", "apks", "path", "tree", "show",
            "extract", "add", "replace", "verify")
    argv = sys.argv[1:]
    if argv and argv[0] not in CMDS and not argv[0].startswith("-"):
        argv = [argv[1], argv[0]] + argv[2:]
    # argparse 对 "nargs='?' 的 positional 出现在 optional 之后" 有已知缺陷：
    # tree --part system / 会把 "/" 丢进 extras，这里补回来。
    args, extra = ap.parse_known_args(argv)
    if extra and getattr(args, "cmd", "") in ("tree", "path") \
            and len(extra) == 1 and not extra[0].startswith("-"):
        args.path = extra[0]
        extra = []
    if extra:
        ap.error("unrecognized arguments: %s" % " ".join(extra))
    if not args.cmd:
        ap.print_help()
        return 2

    rom = None
    member = ""
    try:
        if is_archive(args.image):
            rom = Rom(args.image, args.part, args.keep, args.no_writeback, args.fresh)
            if args.cmd == "info" and not args.part:
                return rom.show_members()
            member = rom.pick()
            img = rom.image(member)
        else:
            img = Image(args.image)
    except (OSError, ValueError) as e:
        log("[错误] %s" % e)
        return 2

    rc = 2
    try:
        if args.cmd == "info":
            rc = cmd_info(img)
        elif args.cmd == "list":
            rc = cmd_list(img, parse_size(args.min_size), args.kind)
        elif args.cmd == "find":
            rc = cmd_find(img, args.needle)
        elif args.cmd == "apks":
            rc = cmd_apks(img, parse_size(args.min_size, 1 << 20),
                          parse_size(args.max_probe, 128 << 20),
                          args.pkg.strip(), args.limit)
        elif args.cmd == "show":
            rc = cmd_show(img, args.idx, args.limit, args.xattr_bytes)
        elif args.cmd == "path":
            rc = cmd_path(img, args.path)
        elif args.cmd == "tree":
            rc = cmd_tree(img, args.path, args.depth, args.limit)
        elif args.cmd == "extract":
            rc = cmd_extract(img, args.idx, args.out)
        elif args.cmd == "add":
            try:
                diridx, label = locate_dir(img, args.dir)
            except ValueError as e:
                log("[错误] 找不到目标目录：%s" % e)
                log("       先用 tree 看一眼：ropatch.py <镜像> tree --part %s --depth 2"
                    % (args.part or "<分区>"))
                rc = 2
            else:
                log("解析目录 : %s = 条目 %d" % (label, diridx))
                rc = cmd_add(img, diridx, args.name, args.file, args.yes,
                             args.backup and not rom, args.xattr_from)
                if rom and rc == 0 and args.yes:
                    rom.mark(member)
        elif args.cmd == "replace":
            rc = cmd_replace(img, args.idx, args.newfile, args.yes,
                             args.backup and not rom, args.align)
            if rom and rc == 0 and args.yes:
                rom.mark(member)
        elif args.cmd == "verify":
            rc = cmd_verify(img, args.limit)
    finally:
        if rom:
            if rom.dirty:
                rom.writeback()
            rom.cleanup()
    return rc


if __name__ == "__main__":
    sys.exit(main())

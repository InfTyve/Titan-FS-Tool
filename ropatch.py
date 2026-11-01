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

VERSION = "2026.11.03"

import os
import io
import sys
import zlib
import bisect
import argparse
import struct
import shutil
import zipfile
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
            "  python3 ropatch.py fs.7z find --part system com.android.settings",
            "  python3 ropatch.py fs.7z replace --part system 123 改好的.apk --yes",
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
    CMDS = ("info", "list", "find", "apks", "extract", "replace", "verify")
    argv = sys.argv[1:]
    if argv and argv[0] not in CMDS and not argv[0].startswith("-"):
        argv = [argv[1], argv[0]] + argv[2:]
    args = ap.parse_args(argv)
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
        elif args.cmd == "extract":
            rc = cmd_extract(img, args.idx, args.out)
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

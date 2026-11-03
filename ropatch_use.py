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

替换 jar（framework.jar / services.jar 这类）：
  python3 ropatch.py fs.7z jars --part system --name framework
  python3 ropatch.py fs.7z extract --part system --path /framework/framework.jar -o old.jar
  python3 ropatch.py fs.7z replace --part system \
        --path /framework/framework.jar new.jar --yes
  # dex 压缩方式、dex 版本、配套 .odex/.vdex 会自动检查；需要一并作废旧 AOT 加 --drop-oat

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

VERSION = "2026.11.11-rev10"

import os
import io
import re
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
    # 管道被 head / grep 截断时不要吐 traceback：直接安静退出
    try:
        print(msg, flush=True)
    except BrokenPipeError:
        os._exit(0)


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
        self.count_hdr = self.count
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
        self._fix_count()

    def _fix_count(self):
        """用「数据起点」反推真实条目数。

        这个格式里条目表后面紧跟着数据区（实测差值为 0）。有些改过的 ROM
        在表尾追加了条目却没更新头部 count，工具照着 count 走就会从末尾那几条
        开始覆盖——被覆盖的条目仍被目录项引用，于是新路径 lstat 出来是别人的 APK。
        这里只做一处保守修正：数据起点是 esz 的整数倍、算出的条目数更大、
        且多出来的每一条都长得像合法条目，才认。
        """
        if not self.esz or self.count == 0:
            return
        offs = []
        for i in range(self.count):
            o = self.entry_off(i)
            v = u32(self.head, o + 40)
            if v:
                offs.append(v)
        if not offs:
            return
        min_off = min(offs)
        d = min_off - self.hdr_len
        if d <= 0 or d % self.esz:
            return
        est = d // self.esz
        if est <= self.count or est > self.count + 2048:
            return
        # 多出来的条目在表尾，head 只读到旧 count 的位置，先补读
        need2 = self.hdr_len + est * self.esz
        if need2 > self.size:
            return
        if len(self.head) < need2:
            with open(self.path, "rb") as f:
                f.seek(0)
                self.head = f.read(need2)
        # 多出来的位置必须条条像样：类型合法、偏移落在数据区
        for k in range(self.count, est):
            o = self.entry_off(k)
            mode = u32(self.head, o)
            fsize = u32(self.head, o + 36)
            foff = u32(self.head, o + 40)
            if (mode & 0o170000) not in (0o040000, 0o100000, 0o120000):
                return
            if foff and foff < min_off:
                return
            if not foff and fsize:
                return
        self.count = est
        need = self.hdr_len + self.count * self.esz
        if len(self.head) < need:
            with open(self.path, "rb") as f:
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

    def refresh(self):
        """写完条目表后重新读头部，否则 entry() 还是旧值。

        条目数可能变大（add 之后），所以要先用新头部里的 count 重新算表长，
        否则 head 会被截断，取后面的条目直接 struct.error。
        """
        self.size = os.path.getsize(self.path)
        with open(self.path, "rb") as f:
            first = f.read(64)
        if len(first) >= 28:
            self.hdr_len = u32(first, 4) or HDR_LEN
            self.count = u32(first, 8)
            self.esz = u32(first, 24) or DEF_ESZ
        need = max(HDR_LEN, self.hdr_len + self.count * self.esz)
        with open(self.path, "rb") as f:
            f.seek(0)
            self.head = f.read(need)
        if len(self.head) >= 20:
            self.used_end = u32(self.head, 12)
            self.last_off = u32(self.head, 16)
        self.count_hdr = self.count
        self._fix_count()
        return self

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


def norm_member(p):
    """包内成员路径归一化：Windows 下建的包可能是反斜杠，比较时统一成 /"""
    return p.replace("\\", "/").strip("/")


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
    # -slt 的第一段描述压缩包本身（Path = xxx.7z，没有 Size 行），不是成员
    if items and items[0]["size"] == 0 and \
            os.path.basename(items[0]["path"]).lower() == os.path.basename(archive).lower():
        items = items[1:]
    return items


def crc32_file(path, chunk=1 << 20):
    """流式算文件 CRC32 与字节数，返回 (size, crc)"""
    crc = 0
    n = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            n += len(b)
            crc = zlib.crc32(b, crc)
    return n, crc & 0xFFFFFFFF


def stream_member(exe, archive, member, chunk=1 << 20):
    """把压缩包内某成员流式读出来，返回 (字节数, crc32, sha256)。

    用 x -so 而不是先解压到磁盘，1.2 GB 的成员也不会吃内存。
    若包内存在多个同名成员，7z 会把它们串起来输出，字节数就会对不上 ——
    这正是「写回其实新增了一条、旧条目没动」的特征，借这个函数能抓到。
    """
    p = subprocess.Popen([exe, "x", "-so", archive, member],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    crc = 0
    n = 0
    h = hashlib.sha256()
    try:
        while True:
            b = p.stdout.read(chunk)
            if not b:
                break
            n += len(b)
            crc = zlib.crc32(b, crc)
            h.update(b)
    finally:
        p.stdout.close()
        p.wait()
    return n, crc & 0xFFFFFFFF, h.hexdigest()


class Rom:
    """把 fs.7z 里的 readonly.bin 当虚拟文件用：按需解出、改完写回"""

    def __init__(self, path, part="", keep=False, no_writeback=False, fresh=False,
                 member=""):
        self.path = os.path.abspath(path)
        self.part = part.strip("/")
        self.keep = keep
        self.no_writeback = no_writeback
        self.exe = find_7z()
        if not self.exe:
            raise ValueError("需要 7z 命令：Alpine 用 apk add p7zip，Debian 用 apt install p7zip-full")
        self.all_members = list_archive(self.exe, self.path)
        members = [it for it in self.all_members
                   if os.path.basename(it["path"]).lower() == "readonly.bin"]
        if not members:
            raise ValueError("包内没有 readonly.bin，这个镜像可能没有只读层")
        if member:
            want_m = norm_member(member)
            members = [m for m in members if norm_member(m["path"]) == want_m]
            if not members:
                raise ValueError("包内没有成员 %r" % member)
        elif part:
            want = part.strip("/").lower()
            # 先按「倒数第二段 == 分区名」精确匹配，避免 backup/system/readonly.bin 混进来
            exact = [m for m in members if self.part_of(m["path"]).lower() == want]
            members = exact or [m for m in members
                                if want in [s.lower() for s in m["path"].split("/")]]
        if not members:
            raise ValueError("包内没有分区 %r 的 readonly.bin" % part)
        self.members = members
        self.fresh = fresh
        self.written = False
        self.tmp = self._cache_dir(path)
        self.dirty = []
        self.extra = []          # 覆盖层新文件（相对 tmp 的成员路径）

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
        if len(self.members) > 1:
            # 多个候选时按「路径更短、不像备份」排序，把最像主目录的放前面，
            # 并全部打印出来 —— 选错成员是「改了但没生效」的常见原因。
            def score(m):
                p = norm_member(m["path"]).lower()
                bad = any(k in p for k in ("backup", "_bak", "/bak", "orig", "copy", "old"))
                # 以前按路径短排，root/readonly.bin 永远排第一，直接 path /system
                # 会落到 1.7 MB 的 root 分区上，报「没有 system」。
                part = self.part_of(m["path"]).lower()
                pri = 0 if part == "system" else (1 if part in ("product", "vendor") else 2)
                return (1 if bad else 0, pri, -m["size"], p.count("/"), len(p))

            self.members.sort(key=score)
            log("[提示] 包内有 %d 个 readonly.bin，已按 system 优先排序：" % len(self.members))
            for m in self.members:
                log("         %-52s %s" % (m["path"], human(m["size"])))
            log("       将使用：%s" % self.members[0]["path"])
            log("       要用别的：加 --member <包内路径>")
        return self.members[0]["path"]

    def image(self, member):
        local = os.path.join(self.tmp, norm_member(member))
        want = [m for m in self.members if m["path"] == member]
        # 缓存里的副本若与包内成员大小不符，说明是写回前的旧文件，必须重解，
        # 否则后面所有读取都在看历史快照 —— 表现为「改了但 sha256 没变」。
        if os.path.exists(local) and want and os.path.getsize(local) != want[0]["size"]:
            log("[缓存] %s 本地副本 %s、包内 %s，不一致，重新解出…"
                % (member, human(os.path.getsize(local)), human(want[0]["size"])))
            try:
                os.remove(local)
            except OSError:
                pass
        if not os.path.exists(local):
            log("解出 %s（%s，需要一点时间）…" % (
                member, human(want[0]["size"]) if want else "大小未知"))
            r = subprocess.run([self.exe, "x", self.path, "-o" + self.tmp, "-y", member],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if r.returncode != 0 or not os.path.exists(local):
                raise ValueError("解压失败：%s" % r.stderr.decode("utf-8", "replace")[:300])
            log("  -> %s" % local)
        # 同分区还有 fscache.bin 的话一并解出来：池的优先级高于只读层，
        # 替换前必须能查 inode 是否被池占用，否则改了也读不到。
        d = os.path.dirname(norm_member(member))
        fcn = (d + "/fscache.bin") if d else "fscache.bin"
        for it in getattr(self, "all_members", []):
            if norm_member(it["path"]) == fcn:
                fl = os.path.join(self.tmp, fcn)
                if not os.path.exists(fl) and it["size"]:
                    log("解出 %s（%s，记录池）…" % (it["path"], human(it["size"])))
                    subprocess.run([self.exe, "x", self.path, "-o" + self.tmp, "-y", it["path"]],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                break
        return Image(local)

    def overlay_root(self, member):
        """把该分区除 readonly.bin 以外的覆盖层文件解到临时目录。

        返回 androidfs_<ver> 目录本身（OvlFS 要在这个下面按分区找）。
        覆盖层可能很大（有的包塞了几百 MB），但只解一次，后面都复用缓存。
        """
        prefix = os.path.dirname(norm_member(member))
        local = os.path.join(self.tmp, prefix)
        marker = os.path.join(local, ".ropatch_ovl")
        if os.path.exists(marker):
            return os.path.dirname(local)
        os.makedirs(local, exist_ok=True)
        log("解出覆盖层 %s/ …" % prefix)
        cmd = [self.exe, "x", self.path, "-o" + self.tmp, "-y",
               prefix + "/*", "-x!" + prefix + "/readonly.bin"]
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        # 7z 在没有匹配项时返回 1（警告），对空覆盖层是正常情况
        if r.returncode not in (0, 1):
            raise ValueError("解压覆盖层失败：%s"
                             % r.stderr.decode("utf-8", "replace")[:300])
        with open(marker, "w") as f:
            f.write("1")
        return os.path.dirname(local)

    def add_extra(self, local_path):
        """登记一个要写进压缩包的新成员（覆盖层文件）"""
        rel = os.path.relpath(local_path, self.tmp).replace("\\", "/")
        if rel not in self.extra:
            self.extra.append(rel)
        return rel

    def mark(self, member):
        if member not in self.dirty:
            self.dirty.append(member)

    def writeback(self):
        if not self.dirty and not self.extra:
            return 0
        if self.no_writeback:
            log("[--no-writeback] 改动只留在临时目录：%s" % self.tmp)
            return 0
        for m in self.dirty:
            local = os.path.join(self.tmp, m.replace("\\", "/"))
            if not os.path.exists(local):
                log("[错误] 临时文件不见了，无法写回：%s" % local)
                return 1
            want_size, want_crc = crc32_file(local)

            # 写回前先确认包里真有这一条：路径对不上时 7z u 会"新增"一条而不是替换，
            # 包看着变大了，实际读到的还是旧成员 —— 这是最隐蔽的失败方式。
            before = list_archive(self.exe, self.path)
            raw, old_size = "", 0
            for it in before:
                if norm_member(it["path"]) == norm_member(m):
                    raw, old_size = it["path"], it["size"]
                    break
            if not raw:
                log("[错误] 包内找不到成员 %r，写回会新增一条而不是替换，已中止" % m)
                log("       包内现有成员：")
                for it in before[:12]:
                    log("         %s" % it["path"])
                if len(before) > 12:
                    log("         …（共 %d 条）" % len(before))
                log("       可能是路径写法不同（\\ 与 /、大小写），先跑 info 看成员名")
                return 1

            log("写回 %s（%s -> %s，重建压缩包，1.2 GB 级别约几分钟）…"
                % (raw, human(old_size), human(want_size)))
            r = subprocess.run([self.exe, "u", self.path, raw], cwd=self.tmp,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if r.returncode != 0:
                log("[错误] 写回失败：%s" % r.stderr.decode("utf-8", "replace")[:300])
                return 1

            # 写回后回读：字节数与 CRC 都必须和本地文件一致，否则等于没写进去
            after = list_archive(self.exe, self.path)
            same = [it for it in after if norm_member(it["path"]) == norm_member(raw)]
            got_size, got_crc, got_sha = stream_member(self.exe, self.path, raw)
            if got_size != want_size or got_crc != want_crc:
                log("[错误] 写回自检失败：包内 %s 仍是旧内容" % raw)
                log("       期望 %s / crc %08x，实际 %s / crc %08x"
                    % (human(want_size), want_crc, human(got_size), got_crc))
                if len(same) > 1:
                    log("       包内有 %d 条同名成员，7z 读的第一条不是刚写的这条" % len(same))
                    for it in same:
                        log("         %s  %s" % (it["path"], human(it["size"])))
                log("       可能原因：包是分卷、只读、或 7z 版本不支持更新；")
                log("       也可能是路径写法不一致（\\ 与 /）。原包未被修改前的备份请自行保留。")
                return 1
            if len(same) > 1:
                log("[警告] 包内有 %d 条同名成员，虽然当前写入的这条已验证一致，"
                    "但解包工具可能取到另一条" % len(same))
                for it in same:
                    log("         %s  %s" % (it["path"], human(it["size"])))
            log("  ✓ 已更新 %s（%s，crc %08x，sha256 %s…）"
                % (raw, human(got_size), got_crc, got_sha[:16]))

        if self.extra:
            if self._write_extra():
                return 1
        self.dirty = []
        self.extra = []
        self.written = True
        log("写回完成：%s（%s）" % (self.path, human(os.path.getsize(self.path))))
        log("建议复验：python3 ropatch.py %s verify --part %s"
            % (os.path.basename(self.path),
               self.part or self.part_of(self.members[0]["path"])))
        return 0

    def _write_extra(self):
        """把覆盖层新文件写进压缩包，并逐个回读核对字节数。

        7z u 对已存在的成员是更新、对不存在的是新增，两种都走这里。
        """
        log("写回覆盖层：%d 个文件（%s）…"
            % (len(self.extra), human(sum(
                os.path.getsize(os.path.join(self.tmp, m)) for m in self.extra))))
        # 一次传太多会撞命令行长度上限，分批
        for k in range(0, len(self.extra), 200):
            batch = self.extra[k:k + 200]
            r = subprocess.run([self.exe, "u", self.path] + batch, cwd=self.tmp,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if r.returncode != 0:
                log("[错误] 覆盖层写回失败：%s"
                    % r.stderr.decode("utf-8", "replace")[:300])
                return 1
        after = list_archive(self.exe, self.path)
        amap = {norm_member(it["path"]): it["size"] for it in after}
        bad = []
        for m in self.extra:
            want = os.path.getsize(os.path.join(self.tmp, m))
            got = amap.get(norm_member(m))
            if got != want:
                bad.append((m, want, got))
        if bad:
            log("[错误] 覆盖层回读不一致，压缩包里没有真正写进去：")
            for m, w, g in bad[:8]:
                log("   %-64s 期望 %s，包内 %s"
                    % (m, human(w), "缺失" if g is None else human(g)))
            return 1
        log("  ✓ %d 个覆盖层文件已写入" % len(self.extra))
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


def mode_str(mode):
    """0o100644 -> -rw-r--r--"""
    head = {"dir": "d", "link": "l", "file": "-"}.get(kind(mode), "?")
    bits = ""
    for shift in (6, 3, 0):
        v = (mode >> shift) & 7
        bits += ("r" if v & 4 else "-") + ("w" if v & 2 else "-") + ("x" if v & 1 else "-")
    return head + bits


def cmd_info(img):
    log("文件      : %s" % img.path)
    log("大小      : %s" % human(img.size))
    log("头长      : %#x" % img.hdr_len)
    log("条目数    : %d" % img.count)
    ch = getattr(img, "count_hdr", img.count)
    if ch != img.count:
        log("[修正]   头部写的是 %d 条，但按数据起点实际有 %d 条。" % (ch, img.count))
        log("         末尾多出的 %d 条工具以前看不见，新增时就正好覆盖它们 ——" % (img.count - ch))
        log("         被覆盖的条目仍被目录项引用，于是新路径读出来是别人的 APK。")
    log("条目大小  : %d" % img.esz)
    log("已用末尾  : %#x (%s)" % (img.used_end, human(img.used_end)))
    log("最后数据  : %#x" % img.last_off)
    log("种子/校验 : %#x" % img.seed)
    log("数据区    : %#x .. %#x" % (img.hdr_len + img.count * img.esz, img.used_end))
    # 表末与数据起点必须严丝合缝（实测这个 ROM 差值为 0）。
    # 差值明显偏大 = count 与真实条目数对不上，新增条目会落进别人的数据区，
    # 表现正是「新增后读出来是另一个应用」。
    tab_end = img.hdr_len + img.count * img.esz
    min_off = min([e["off"] for e in img.entries() if e["off"]] or [0])
    log("条目表末  : %#x（%d + %d×%d）" % (tab_end, img.hdr_len, img.count, img.esz))
    log("数据起点  : %#x（差 %+d）" % (min_off, min_off - tab_end))
    if min_off and min_off < tab_end:
        log("[错误]   数据起点早于条目表末尾：count 偏大，表已经压到数据上。")
        log("         此时新增条目必然覆盖别的文件，请先查清 count 的真实来源。")
    elif min_off and min_off - tab_end > 0x1000:
        log("[警告]   表末到数据起点有 %d B 空隙，count 可能偏小（中间还藏着条目），"
            % (min_off - tab_end))
        log("         新增条目号会撞上已有条目，先别 add。")
    files = dirs = 0
    tot = 0
    for e in img.entries():
        if isdir(e["mode"]):
            dirs += 1
        else:
            files += 1
            tot += e["size"]
    log("内容      : 目录 %d，文件 %d，文件合计 %s" % (dirs, files, human(tot)))
    ref_end = max([img.used_end] + [e["off"] + e["size"] for e in img.entries() if e["size"]])
    log("条目末尾  : %#x (%s)" % (ref_end, human(ref_end)))
    if ref_end > img.used_end:
        log("[警告]   有条目数据落在「已用末尾」之后：旧版 add 复制数据区时会截断它们，")
        log("         造成尾部一批条目 CRC 不符。当前版本已按条目末尾复制，可直接重做。")
    if ref_end < img.size:
        log("尾部空闲  : %s（按条目实际末尾算）" % human(img.size - ref_end))
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


def _strpool(data, off):
    """解析 AXML 的 RES_STRING_POOL_TYPE，返回字符串列表。"""
    _t, hsize, _size = struct.unpack_from("<HHI", data, off)
    count, _scount, flags, sstart, _sstart2 = struct.unpack_from("<IIIII", data, off + 8)
    utf8 = bool(flags & 0x100)
    base = off + sstart
    out = []
    if count == 0:
        return out
    offs = struct.unpack_from("<%dI" % count, data, off + hsize)
    for o in offs:
        p = base + o
        try:
            if utf8:
                n1 = data[p]
                p += 1
                if n1 & 0x80:
                    n1 = ((n1 & 0x7F) << 8) | data[p]
                    p += 1
                n2 = data[p]
                p += 1
                if n2 & 0x80:
                    n2 = ((n2 & 0x7F) << 8) | data[p]
                    p += 1
                out.append(data[p:p + n2].decode("utf-8", "replace"))
            else:
                n = struct.unpack_from("<H", data, p)[0]
                p += 2
                out.append(data[p:p + n * 2].decode("utf-16-le", "replace"))
        except (IndexError, struct.error):
            out.append("")
    return out


def parse_axml(data):
    """精确解析二进制 manifest 的根节点，取 package / versionCode / versionName。

    只走字符串池 + 第一个 <manifest> START_TAG，不建整棵树。
    """
    if not data or len(data) < 16:
        return None
    typ, hsize, total = struct.unpack_from("<HHI", data, 0)
    if typ != 0x0003:
        return None
    end = min(total, len(data))
    strings = []

    off = hsize
    while off + 8 <= end:
        ctype, chsize, csize = struct.unpack_from("<HHI", data, off)
        if csize < 8:
            break
        if ctype == 0x0001:
            strings = _strpool(data, off)
            break
        off += csize

    off = hsize
    while off + 8 <= end:
        ctype, chsize, csize = struct.unpack_from("<HHI", data, off)
        if csize < 8:
            break
        if ctype == 0x0102:                       # START_TAG
            ext = off + chsize
            if ext + 20 > end:
                break
            _ns, name_i, astart, asize, acount = struct.unpack_from("<IIHHH", data, ext)
            tag = strings[name_i] if name_i < len(strings) else ""
            if tag == "manifest":
                attrs = {}
                base = ext + astart
                for k in range(acount):
                    a = base + k * asize
                    if a + 20 > end:
                        break
                    _ans, aname_i, raw, _vsize, _res0, dtype, vdata = \
                        struct.unpack_from("<IIIHBBI", data, a)
                    key = strings[aname_i] if aname_i < len(strings) else ""
                    if not key:
                        continue
                    if dtype in (0x10, 0x11):      # INT_DEC / INT_HEX
                        attrs[key] = vdata
                    elif raw != 0xFFFFFFFF and raw < len(strings):
                        attrs[key] = strings[raw]
                    elif vdata < len(strings):
                        attrs[key] = strings[vdata]
                    else:
                        attrs[key] = vdata
                return {"pkg": str(attrs.get("package", "") or "").strip(),
                        "vc": attrs.get("versionCode", -1),
                        "vn": str(attrs.get("versionName", "") or "")}
        off += csize
    return None


def guess_pkg(man):
    """取 manifest 的 package 属性；解析失败才退回字符串池启发式。

    旧实现直接找「最短的 com.* 字符串」，manifest 里只要出现更短的组件名
    （例如 com.android.settings.icon），就会把包名报错。现在以 AXML 根节点为准。
    """
    meta = parse_axml(man)
    if meta and meta.get("pkg"):
        return [meta["pkg"]]
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


def cmd_extract(img, idx, out, path=""):
    if path:
        try:
            idx, trail = locate_entry(img, path)
        except ValueError as e:
            log("[错误] %s" % e)
            return 2
        log("解析     : %s = 条目 %d" % (trail, idx))
    e = img.entry(idx)
    if isdir(e["mode"]):
        log("[错误] 条目 %d 是目录，extract 只能导出文件" % idx)
        return 2
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


def dex_ver(head):
    """dex 头前 8 字节 -> 版本字符串，如 035 / 039"""
    if len(head) < 8 or head[:4] != b"dex\n" or head[7] != 0:
        return ""
    try:
        return head[4:7].decode("ascii")
    except UnicodeDecodeError:
        return ""


def probe_jar(src):
    """探测一个 zip 容器（jar/apk）：dex 列表、dex 版本、压缩方式、签名文件。

    src 可以是 bytes，也可以是可 seek 的文件对象（Read 时只碰中央目录）。
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(src) if isinstance(src, (bytes, bytearray)) else src)
        with zf:
            infos = zf.infolist()
            dex = []
            for i in infos:
                if not re.fullmatch(r"classes\d*\.dex", i.filename):
                    continue
                ver = ""
                try:
                    with zf.open(i) as fp:
                        ver = dex_ver(fp.read(8))
                except Exception:
                    pass
                dex.append({"name": i.filename, "size": i.file_size,
                            "csize": i.compress_size,
                            "stored": i.compress_type == zipfile.ZIP_STORED,
                            "ver": ver})
            signed = [i.filename for i in infos
                      if i.filename.upper().startswith("META-INF/")
                      and i.filename.upper().endswith((".SF", ".RSA", ".DSA", ".EC"))]
            return {"ok": True, "members": len(infos), "dex": dex,
                    "signed": signed, "names": [i.filename for i in infos]}
    except Exception as exc:
        return {"ok": False, "err": str(exc), "dex": [], "names": [], "signed": []}


def jar_line(label, nbytes, p):
    """一行 jar 摘要"""
    if not p.get("ok"):
        log("%-6s %-11s 不是合法 zip（%s）" % (label, human(nbytes), p.get("err", "?")))
        return
    dex = p["dex"]
    vers = sorted({d["ver"] for d in dex if d["ver"]})
    stored = sum(1 for d in dex if d["stored"])
    mode = ""
    if dex:
        mode = "STORED" if stored == len(dex) else ("DEFLATED" if stored == 0 else "混合")
    log("%-6s %-11s 成员 %-5d dex %d 个%s%s" % (
        label, human(nbytes), p["members"], len(dex),
        ("，版本 " + "/".join(vers)) if vers else "",
        ("，" + mode) if mode else ""))


def repack_store_dex(data):
    """把 classes*.dex 重新打成 STORED（不压缩）的 jar，其余成员原样保留。

    平台 boot jar（framework.jar 等）里的 dex 通常是不压缩的，保持同样形态最稳。
    """
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as zin:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
            zout.comment = zin.comment
            for info in zin.infolist():
                payload = zin.read(info.filename)
                ni = zipfile.ZipInfo(info.filename, info.date_time)
                ni.external_attr = info.external_attr
                ni.create_system = info.create_system
                ni.comment = info.comment
                if re.fullmatch(r"classes\d*\.dex", info.filename):
                    ni.compress_type = zipfile.ZIP_STORED
                else:
                    ni.compress_type = info.compress_type
                zout.writestr(ni, payload)
    return out.getvalue()


AOT_EXT = (".odex", ".vdex", ".oat", ".art")


def find_aot(img, diridx, stem, base=""):
    """找某个 jar 的配套 AOT 产物。

    返回 (hits, boots)：
      hits  [(相对路径, 条目号, entry, 说明)]  形如 /framework/oat/arm64/framework.odex
      boots [(相对路径, 条目号, entry)]         boot.art/boot.oat（不参与 --drop-oat）

    AOT 可能就躺在 jar 旁边，也可能在 oat/<abi>/ 下（Android 8+ 的布局），各往下探两层。
    """
    hits, boots = [], []
    low = stem.lower()
    base = base.rstrip("/")
    abi = ("oat", "arm", "arm64", "x86", "x86_64", "arm64-v8a", "armeabi-v7a")

    def scan(idx, prefix, depth):
        for ino, nm in dir_children(img, img.entry(idx))[0]:
            if not (0 <= ino < img.count):
                continue
            ch = img.entry(ino)
            l = nm.lower()
            if not isdir(ch["mode"]):
                if l.startswith(low + ".") and l.endswith(AOT_EXT):
                    hits.append((base + prefix + "/" + nm, ino, ch,
                                 "同目录" if depth == 0 else "子目录"))
                elif l.startswith("boot.") and l.endswith((".art", ".oat")):
                    boots.append((base + prefix + "/" + nm, ino, ch))
                continue
            if depth < 2 and (l in abi or l.startswith("oat")):
                scan(ino, prefix + "/" + nm, depth + 1)

    scan(diridx, "", 0)
    return hits, boots


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
        return [], 0, 0, False
    data = img.read(e)
    best = None
    for ino_first in (False, True):
        for al in (0, 4):
            got, end = _parse_layout(data, al, ino_first)
            if not got:
                continue
            fit = 1 if end >= len(data) - 3 else 0
            sane = 1 if all(0 <= i < img.count for i, _ in got) else 0
            # 只读层实测布局是 u32 名长(含\0)+名字+u64 inode、无填充，
            # 平局时优先它；sane 只作参考（inode 不连续时它会误判）。
            pref = 1 if (not ino_first and al == 0) else 0
            score = (fit, len(got), pref, sane)
            if best is None or score > best[0]:
                best = (score, got, al, end, ino_first)
    if best is None:
        return [], 0, 0, False
    return best[1], best[2], best[3], best[4]


def detect_layout(img, limit=4096):
    """空目录没有数据可供判断，用分区内其它非空目录的布局作为默认值"""
    for i in range(min(img.count, limit)):
        e = img.entry(i)
        if isdir(e["mode"]) and e["size"]:
            items, al, _used, ino_first = dir_children(img, e)
            if items:
                return ino_first, al
    return False, 0


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


PART_NAMES = ("system", "system_ext", "vendor", "product", "odm", "oem",
              "cust", "data", "root", "metadata", "boot", "vendor_dlkm",
              "odm_dlkm", "system_dlkm", "my_product", "my_heytap", "my_stock")


def strip_part_prefix(img, path):
    """只读层的根就是分区根，manifest 里的 /system/app 在镜像里是 /app。

    只有首段确实是分区名才剥；否则原样返回空串，避免把 /framework/x 剥成 /x 后
    给出误导性的第二条错误信息。
    """
    segs = [s for s in path.replace("\\", "/").split("/") if s]
    if len(segs) < 2 or segs[0].lower() not in PART_NAMES:
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


def locate_entry(img, path):
    """把 /system/framework/framework.jar 解析成条目号（文件或目录都行）

    先按完整路径找，找不到再按"分区内路径"找（只读层的根就是分区根）。
    """
    p = (path or "").strip()
    if not p:
        raise ValueError("路径不能为空")
    if not p.startswith("/"):
        p = "/" + p
    try:
        return resolve_path(img, p, quiet=True)
    except ValueError as e1:
        alt = strip_part_prefix(img, p)
        if not alt:
            raise ValueError(str(e1))
        try:
            idx, trail = resolve_path(img, alt, quiet=True)
        except ValueError as e2:
            raise ValueError("两种解释都没命中：\n"
                             "         完整路径 %s：%s\n"
                             "         分区内   %s：%s\n"
                             "       只读层的根就是分区根，/system/framework 在这里是 /framework"
                             % (p, e1, alt, e2))
        log("  [提示] %s 不在层级里，按分区内路径 %s 解析（只读层的根就是该分区根）"
            % (p, trail))
        return idx, trail


def walk_paths(img, root=0, max_depth=48):
    """遍历整棵树，产出 (路径, 条目号, entry)。用于按扩展名找文件（jar 落在哪）。"""
    seen = {root}
    stack = [("", root, 0)]
    while stack:
        base, idx, depth = stack.pop()
        e = img.entry(idx)
        if not isdir(e["mode"]):
            continue
        for ino, nm in dir_children(img, e)[0]:
            if not (0 <= ino < img.count):
                continue
            ch = img.entry(ino)
            p = base + "/" + nm
            if isdir(ch["mode"]):
                if depth + 1 <= max_depth and ino not in seen:
                    seen.add(ino)
                    stack.append((p, ino, depth + 1))
            else:
                yield p, ino, ch


def cmd_jars(img, name, min_size, max_probe, limit, part=""):
    """列出只读层里所有 .jar（带完整路径、dex 个数与版本）"""
    log("遍历目录树，找 .jar（每个目录都要读一次目录数据，1.2 GB 镜像约十几秒）…")
    rows = []
    n = 0
    for p, i, e in walk_paths(img):
        n += 1
        if not p.lower().endswith(".jar") or e["size"] < min_size:
            continue
        if name and name.lower() not in p.lower():
            continue
        rows.append((p, i, e))
    if not rows:
        log("[结果] 没有符合条件的 .jar（扫描了 %d 个非目录条目）" % n)
        log("       放宽条件：--min-size 0，或去掉 --name")
        return 1
    rows.sort(key=lambda r: -r[2]["size"])
    log("[结果] %d 个 .jar：" % len(rows))
    log("")
    log("%-6s %-11s %-9s %s" % ("条目", "大小", "dex", "路径"))
    for p, i, e in rows[:limit]:
        info = ""
        if e["size"] <= max_probe:
            with open(img.path, "rb") as f:
                pr = probe_jar(SubFile(f, e["off"], e["size"]))
            if pr["ok"]:
                vers = sorted({d["ver"] for d in pr["dex"] if d["ver"]})
                info = "%d 个%s" % (len(pr["dex"]),
                                    ("（" + "/".join(vers) + "）") if vers else "")
            else:
                info = "解析失败"
        else:
            info = "过大未探测"
        log("%-6d %-11s %-9s %s" % (i, human(e["size"]), info, p))
    if len(rows) > limit:
        log("... 另有 %d 个，用 --limit 放宽" % (len(rows) - limit))
    log("")
    log("下一步：python3 ropatch.py <镜像> replace --part %s --path %s 新文件.jar --yes"
        % (part or "<分区>", rows[0][0]))
    return 0


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


def _strict_items(data, ino_first, align=0):
    """独立的目录项解析（不复用探测逻辑），按固定布局把字节读一遍。

    自检如果只用「写的时候那套推断」去读，等于自己证明自己，判错了也发现不了。
    这里用和 titanunpack 一致的读法再验一次，两条路径独立。
    """
    out = []
    q = 0
    n = len(data)
    while q + 12 <= n:
        if ino_first:
            ino = struct.unpack_from("<Q", data, q)[0]
            nl = struct.unpack_from("<I", data, q + 8)[0]
            npos = q + 12
        else:
            nl = struct.unpack_from("<I", data, q)[0]
            npos = q + 4
            if nl < 2 or nl > 255 or npos + nl + 8 > n:
                break
            ino = struct.unpack_from("<Q", data, npos + nl)[0]
        if nl < 2 or nl > 255 or npos + nl > n:
            break
        try:
            s = data[npos:npos + nl].rstrip(b"\0").decode("utf-8")
        except UnicodeDecodeError:
            break
        if not s or any(ord(c) < 32 for c in s):
            break
        out.append((ino, s))
        q = npos + nl + (0 if ino_first else 8)
        if align:
            q = (q + (align - 1)) & ~(align - 1)
    return out, q


class _AddPlan:
    """一次批量新增的事务：先算出新条目表，再一次性重写容器。

    逐条 add 每次都要重写整个容器（1.2 GB 级要几分钟），十几个应用就是十几遍；
    这里把所有改动攒在一起，只落一次盘。新增的目录、文件统一编号，从 count 开始。
    """

    def __init__(self, img, sort=False, xattr_from=-1, append=False):
        self.img = img
        self.sort = sort
        self.append = append
        self.next_ino = img.count
        self.old_dirs = {}     # 原有目录：被改了子项的记在这里
        self.new_dirs = {}     # 本次新建的目录
        self.new_files = {}    # 本次新增的文件
        self.created = []      # [(ino, "dir"/"file")]，按分配顺序
        self.default_layout = detect_layout(img)
        self.tpl = img.entry(xattr_from) if 0 <= xattr_from < img.count else None
        self.blocks = []

    # ---------- 目录状态 ----------
    def _dir(self, idx):
        if idx in self.new_dirs:
            return self.new_dirs[idx]
        d = self.old_dirs.get(idx)
        if d is not None:
            return d
        e = self.img.entry(idx)
        if not isdir(e["mode"]):
            raise ValueError("条目 %d 是 %s，不是目录" % (idx, kind(e["mode"])))
        old = self.img.read(e) if e["size"] else b""
        items, al, used, ino_first = dir_children(self.img, e)
        if not items and not e["size"]:
            ino_first, al = self.default_layout
        # 真实 ROM 的目录项按名字严格升序（引擎多半按名字顺序/二分查找）。
        # 这里把「原有子项是否有序」记下来，决定新条目是插入还是追加。
        srt = all(items[k][1].encode("utf-8") < items[k + 1][1].encode("utf-8")
                  for k in range(len(items) - 1))
        d = {"idx": idx, "e": e, "old": old, "used": used,
             "items": list(items), "extra": [], "new": False, "dirty": False,
             "layout": (ino_first, al), "xattr": self.img.xattr(e),
             "uid": e["uid"], "gid": e["gid"], "sorted": srt, "mode": "append"}
        self.old_dirs[idx] = d
        return d

    def _alloc(self, what):
        ino = self.next_ino
        self.next_ino += 1
        self.created.append((ino, what))
        return ino

    def lookup(self, idx, name):
        for ino, nm in self._dir(idx)["items"]:
            if nm == name:
                return ino
        return None

    def mkdir(self, parent, name):
        d = self._dir(parent)
        if self.lookup(parent, name) is not None:
            raise ValueError("目录里已经有 %r" % name)
        ino = self._alloc("dir")
        d["items"].append((ino, name))
        d["extra"].append((ino, name))
        d["dirty"] = True
        self.new_dirs[ino] = {"idx": ino, "items": [], "extra": [], "new": True,
                              "layout": self.default_layout, "dirty": True,
                              "xattr": d["xattr"], "uid": d["uid"], "gid": d["gid"]}
        return ino

    def add_file(self, parent, name, src, mode=None):
        d = self._dir(parent)
        if self.lookup(parent, name) is not None:
            raise ValueError("目录里已经有 %r（要换内容请用 replace）" % name)
        ino = self._alloc("file")
        size, crc = crc32_file(src)
        if self.tpl is not None:
            xb, uid, gid = self.img.xattr(self.tpl), self.tpl["uid"], self.tpl["gid"]
        else:
            xb, uid, gid = d["xattr"], d["uid"], d["gid"]
        if mode is None:
            mode = 0o100644
            try:
                if os.stat(src).st_mode & 0o111:
                    mode = 0o100755
            except OSError:
                pass
        self.new_files[ino] = {"ino": ino, "src": src, "size": size, "crc": crc,
                               "xattr": xb, "mode": mode, "uid": uid, "gid": gid}
        d["items"].append((ino, name))
        d["extra"].append((ino, name))
        d["dirty"] = True
        return ino

    # ---------- 组装 ----------
    def _render(self, d):
        ino_first, al = d["layout"]
        if d["new"]:
            seq = sorted(d["items"], key=lambda t: t[1].encode("utf-8"))
            return pack_dirents(seq, al, ino_first)
        old, used = d["old"], d["used"]
        base = old[:used] if 0 < used <= len(old) else old
        if not d["extra"]:
            return base
        # 原有目录项是有序的（真实 ROM 都如此），就必须插到名字对应的位置：
        # 追加到末尾会让引擎按名字查不到新条目 —— 现象是「应用装进去了，列表里没有」。
        if not self.append and (d["sorted"] or self.sort) and used >= len(old) - 3:
            d["mode"] = "sorted"
            seq = sorted(d["items"], key=lambda t: t[1].encode("utf-8"))
            return pack_dirents(seq, al, ino_first)
        # 原目录本身无序、或尾部有解析不了的残留：只追加，原有字节一个不动。
        d["mode"] = "append"
        return base + pack_dirents(d["extra"], al, ino_first)

    def layout_blocks(self):
        img = self.img
        esz = img.esz
        n_new = len(self.created)
        delta = n_new * esz
        total = img.count + n_new
        old_start = img.hdr_len + img.count * esz
        # used_end 是头部记的末尾，但真实 ROM 里可能有条目数据落在它之后
        # （目录数据按 4K 块对齐时很常见）。复制数据区必须以「所有条目引用的
        # 最大末尾」为准，否则 add 会把尾部截掉，造成一批条目 CRC 不符。
        ref_end = 0
        for e in img.entries():
            if e["size"] and e["off"] + e["size"] > ref_end:
                ref_end = e["off"] + e["size"]
        end = max(img.used_end or 0, ref_end)
        if end > img.size:
            end = img.size
        self.remain = max(0, end - old_start)
        self.delta = delta
        self.total = total
        self.old_start = old_start
        self.end = end

        cur = end + delta
        self.blocks = []
        for idx in sorted(self.old_dirs):
            d = self.old_dirs[idx]
            if not d["dirty"]:
                continue
            body = self._render(d)
            self.blocks.append((idx, "olddir", body, d["xattr"], cur))
            cur += len(body) + len(d["xattr"])
        for ino, what in self.created:
            if what == "dir":
                d = self.new_dirs[ino]
                body = self._render(d)
                self.blocks.append((ino, "newdir", body, d["xattr"], cur))
                cur += len(body) + len(d["xattr"])
            else:
                f = self.new_files[ino]
                self.blocks.append((ino, "file", b"", f["xattr"], cur))
                cur += f["size"] + len(f["xattr"])
        self.used_end = cur
        return total, cur

    def table_bytes(self):
        img = self.img
        esz = img.esz
        table = bytearray(self.total * esz)
        for i in range(img.count):
            o = img.entry_off(i)
            table[i * esz:(i + 1) * esz] = img.head[o:o + esz]
        if self.delta:
            for i in range(img.count):
                off = struct.unpack_from("<I", table, i * esz + 40)[0]
                if off:
                    struct.pack_into("<I", table, i * esz + 40, off + self.delta)

        now = int(time.time())
        for ino, what, body, xb, off in self.blocks:
            o = ino * esz
            if what == "file":
                f = self.new_files[ino]
                struct.pack_into("<I", table, o, f["mode"])
                struct.pack_into("<II", table, o + 4, f["uid"], f["gid"])
                struct.pack_into("<I", table, o + 12, 1)
                struct.pack_into("<IIII", table, o + 16, now, 0, now, 0)
                struct.pack_into("<IIIII", table, o + 32, 0x50000000,
                                 f["size"], off, f["crc"], len(xb))
            else:
                if what == "newdir":
                    d = self.new_dirs[ino]
                    struct.pack_into("<IIII", table, o, 0o40755, d["uid"], d["gid"], 1)
                    struct.pack_into("<IIII", table, o + 16, now, 0, now, 0)
                    struct.pack_into("<I", table, o + 32, 0x50000000)
                struct.pack_into("<IIII", table, o + 36, len(body), off,
                                 zlib.crc32(body) & 0xFFFFFFFF, len(xb))
        return bytes(table)

    def write(self, tmp):
        img = self.img
        hdr = bytearray(img.head[:img.hdr_len])
        struct.pack_into("<I", hdr, 8, self.total)
        struct.pack_into("<I", hdr, 12, self.used_end)
        # +16 原样保留。早期版本把它当成「最后数据块偏移」写成了 blocks[-1][4]，
        # 但真实 ROM 里该字段与任何数据块偏移都不吻合（如 0x398f74f7，非 4 对齐），
        # 更像是镜像级的随机标识/校验。覆盖它没有任何依据，只会让引擎拿到
        # 一个凭空捏造的值；而早期那个写法恰好与「新增后读出来是别的应用」同时出现。
        table = self.table_bytes()

        written = 0
        with open(img.path, "rb") as src, open(tmp, "wb") as dst:
            dst.write(hdr)
            written += len(hdr)
            dst.write(table)
            written += len(table)
            src.seek(self.old_start)
            left = self.remain
            while left > 0:
                b = src.read(min(CHUNK, left))
                if not b:
                    break
                dst.write(b)
                written += len(b)
                left -= len(b)
            for ino, what, body, xb, off in self.blocks:
                if what == "file":
                    with open(self.new_files[ino]["src"], "rb") as nf:
                        while True:
                            b = nf.read(CHUNK)
                            if not b:
                                break
                            dst.write(b)
                            written += len(b)
                else:
                    dst.write(body)
                    written += len(body)
                dst.write(xb)
                written += len(xb)
        return written

    def verify(self, path):
        """写完先在临时文件上重读一遍：旧条目一个不能变，新条目内容要对得上。"""
        try:
            img2 = Image(path)
        except Exception as e:
            return "重新打开失败：%s" % e
        if img2.count != self.total:
            return "条目数 %d，应为 %d" % (img2.count, self.total)
        if img2.used_end != self.used_end:
            return "已用末尾 %d，应为 %d" % (img2.used_end, self.used_end)

        dirty = {i for i, d in self.old_dirs.items() if d["dirty"]}
        for i in range(self.img.count):
            if i in dirty:
                continue
            a, b = self.img.entry(i), img2.entry(i)
            for k in ("mode", "uid", "gid", "size", "crc"):
                if a[k] != b[k]:
                    return "原有条目 %d 的 %s 被改动（%s -> %s）" % (i, k, a[k], b[k])
        for idx, d in self.old_dirs.items():
            if not d["dirty"]:
                continue
            # 关键：必须用「原目录的布局」重新解析，不能靠 dir_children 自动探测。
            # 布局写反时，探测也会选到同一个反布局并把数据解成自洽的样子，
            # 自检就会误判为通过，而引擎按原布局读是错位的。
            ino_first, al = d["layout"]
            data2 = img2.read(img2.entry(idx))
            got, end2 = _parse_layout(data2, al, ino_first)
            if end2 != len(data2):
                return ("目录 %d 按原布局只解析到 %d/%d 字节，目录项已被写坏"
                        % (idx, end2, len(data2)))
            m1 = {n: i for i, n in d["items"]}
            m2 = {n: i for i, n in got}
            if m1 != m2:
                return ("目录 %d 的子项对不上（写后解析出 %d 项，应为 %d 项）"
                        % (idx, len(m2), len(m1)))
            if d["mode"] == "sorted":
                raw = [n.encode("utf-8") for _i, n in got]
                for k in range(len(raw) - 1):
                    if raw[k] >= raw[k + 1]:
                        return ("目录 %d 写后不是按名字升序（%r 跟在 %r 后面），"
                                "引擎按名字查找会漏掉新条目"
                                % (idx, got[k + 1][1], got[k][1]))
        for ino, d in self.new_dirs.items():
            ino_first, al = d["layout"]
            data2 = img2.read(img2.entry(ino))
            got, end2 = _parse_layout(data2, al, ino_first)
            if end2 != len(data2) or {n: i for i, n in d["items"]} != {n: i for i, n in got}:
                return "新目录 %d 的子项对不上（按原布局解析 %d/%d 字节）" % (
                    ino, end2, len(data2))
        for ino, f in self.new_files.items():
            e = img2.entry(ino)
            if e["size"] != f["size"] or e["crc"] != f["crc"]:
                return ("新条目 %d 内容不符：size %d/%d，crc %08x/%08x"
                        % (ino, e["size"], f["size"], e["crc"], f["crc"]))
        return ""


def _walk_dir(img, plan, path, mkdir, what):
    """按路径逐段下沉，缺失的段在 --mkdir 下现建。"""
    segs = [s for s in path.replace("\\", "/").split("/") if s]
    cur = 0
    trail = ""
    for seg in segs:
        nxt = plan.lookup(cur, seg)
        if nxt is None:
            if not mkdir:
                raise ValueError("%s 不存在（%s）" % (trail + "/" + seg, what))
            nxt = plan.mkdir(cur, seg)
        elif nxt < img.count:
            e = img.entry(nxt)
            if not isdir(e["mode"]):
                raise ValueError("%s 是 %s，不是目录" % (trail + "/" + seg, kind(e["mode"])))
        trail += "/" + seg
        cur = nxt
    return cur, trail or "/"


def _ensure_dir(img, plan, target, mkdir):
    t = (target or "").strip()
    if not t:
        raise ValueError("目标目录不能为空")
    if t.isdigit():
        return locate_dir(img, t)
    p = t if t.startswith("/") else "/" + t
    alt = strip_part_prefix(img, p)
    # 1) 原样路径整体存在：直接用它
    try:
        idx, trail = resolve_path(img, p, quiet=True)
        return idx, "路径 %s" % trail
    except ValueError:
        pass
    if not mkdir:
        # 不走 --mkdir 时才回退到分区内路径（旧用法：/system/app 在镜像里是 /app）
        if alt:
            try:
                idx, trail = resolve_path(img, alt, quiet=True)
                log("  [提示] %s 不在层级里，按分区内路径 %s 解析" % (p, trail))
                return idx, "路径 %s" % trail
            except ValueError:
                pass
        raise ValueError("路径不存在：%s —— 要自动创建目录请加 --mkdir" % p)
    # 2) 要新建：以「父目录能否解析」为准，绝不因为剥离版碰巧存在就跳过去。
    # system-as-root 的镜像根下同时有 /priv-app（空壳）和 /system/priv-app（真应用），
    # 只看「哪个能整体解析」会把新增应用装进空壳，PMS 永远扫不到。
    for c in (p, alt):
        if not c:
            continue
        parent = os.path.dirname(c.rstrip("/")) or "/"
        try:
            resolve_path(img, parent, quiet=True)
        except ValueError:
            continue
        idx, trail = _walk_dir(img, plan, c, True, "目录")
        log("  [新建] %s（条目 %d）" % (trail, idx))
        return idx, "新建 %s" % trail
    raise ValueError("路径不存在，且父目录也找不到：%s" % p)


def cmd_add(img, diridx, name, newfile, yes, backup, xattr_from, sort=False,
            mkdir=False, append=False):
    """兼容旧调用：往单个目录挂一个文件。"""
    return cmd_add_ops(img, [(diridx, name, newfile)], yes, backup,
                       xattr_from, sort, mkdir, append)


def cmd_add_ops(img, ops, yes, backup, xattr_from, sort=False, mkdir=False,
                append=False):
    """ops: [(目标目录（条目号或路径）, 文件名, 源文件)]，一次重写只落一次盘。"""
    plan = _AddPlan(img, sort, xattr_from, append)
    done, skip = [], []
    cache = {}
    for target, name, src in ops:
        name = (name or "").strip()
        if not name or "/" in name or name in (".", ".."):
            skip.append((target, name or "-", "文件名非法"))
            continue
        if not os.path.isfile(src):
            skip.append((target, name, "源文件不存在：%s" % src))
            continue
        if name.lower().endswith(".apk"):
            with open(src, "rb") as f:
                magic = f.read(4)
            if magic[:2] != b"PK":
                skip.append((target, name, "不是 zip/APK（魔数 %r），放进 /system 会让开机扫描卡住"
                             % magic[:4]))
                continue
        try:
            key = str(target)
            if key in cache:
                parent, label = cache[key]
            else:
                parent, label = _ensure_dir(img, plan, target, mkdir)
                cache[key] = (parent, label)
            ino = plan.add_file(parent, name, src)
            done.append((label, name, src, ino))
        except ValueError as e:
            skip.append((target, name, str(e)))

    for target, name, why in skip:
        log("  [跳过] %s / %s：%s" % (target, name, why))
    if not done:
        log("[结束] 没有可新增的文件")
        return 2

    total, used_end = plan.layout_blocks()
    grow = used_end - img.size
    ndir = len(plan.new_dirs)
    log("")
    log("目标目录 : %s" % done[0][0])
    for label, name, src, ino in done:
        log("  + 条目 %-5d %-44s %s" % (ino, name, human(os.path.getsize(src))))
    log("新增     : 文件 %d 个%s" % (len(done), "、目录 %d 个" % ndir if ndir else ""))
    for ino in sorted(plan.new_dirs):
        d = plan.new_dirs[ino]
        log("           %-10s 新建目录，%d 项" % ("条目 %d" % ino, len(d["items"])))
    log("改写     : 目录项 %d 个目录" % sum(1 for d in plan.old_dirs.values() if d["dirty"]))
    for idx in sorted(plan.old_dirs):
        d = plan.old_dirs[idx]
        if not d["dirty"]:
            continue
        n0 = len(d["items"]) - len(d["extra"])
        if d["mode"] == "sorted":
            log("           %-10s %d -> %d 项，按名字插到对应位置"
                % ("条目 %d" % idx, n0, len(d["items"])))
        else:
            log("           %-10s %d -> %d 项，追加到末尾（%s）"
                % ("条目 %d" % idx, n0, len(d["items"]),
                   "--append 强制不重排" if d["sorted"] else "原目录本就无序"))
    log("条目表   : %d -> %d 项（数据区整体后移 %d B）"
        % (img.count, total, plan.delta))
    log("容器     : %s -> %s（%+d B）" % (human(img.size), human(used_end), grow))
    log("xattr    : %s" % ("来自条目 %d" % xattr_from if plan.tpl is not None
                          else "复制各自父目录（security.selinux）"))

    fsc = os.path.join(os.path.dirname(img.path), "fscache.bin")
    if os.path.exists(fsc):
        log("[提示]   同分区有 fscache.bin（%s），它是按**路径名**索引的 dentry 缓存，"
            % human(os.path.getsize(fsc)))
        log("         不是 inode 号表，新路径不在其中也不会被它截获。")
        log("         导入后若仍看不见，先跑 check 看目录项排序（这才是已知主因）。")

    if not yes:
        log("")
        log("[演练] 未写入。加 --yes 真正执行（先写 %s 的临时文件，验过再原子替换）"
            % human(used_end))
        return 0

    if backup:
        bak = img.path + ".bak"
        log("备份 -> %s" % bak)
        shutil.copy2(img.path, bak)

    tmp = img.path + ".newadd"
    try:
        written = plan.write(tmp)
        if written != used_end:
            log("[错误] 写出的长度 %d 与预期 %d 不符，已放弃" % (written, used_end))
            os.unlink(tmp)
            return 2
        bad = plan.verify(tmp)
        if bad:
            log("")
            log("[错误] 自检不通过：%s" % bad)
            log("       原文件未改动，临时文件已丢弃。")
            log("       说明该目录的项布局与工具推断的不一致，别硬写；")
            log("       可用 replace 顶替一个闲置 APK（内容换了，目录项不动，零风险）。")
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

    img.refresh()
    log("")
    log("✓ 已新增 %d 个文件到 %s" % (len(done), done[0][0]))
    log("  容器 %s，条目 %d 个" % (human(os.path.getsize(img.path)), img.count))
    dir_path = done[0][0].replace("新建 ", "").replace("路径 ", "").strip()
    p0 = dir_path.rstrip("/") + "/" + done[0][1]
    log("  体检：ropatch.py <镜像> check --part <分区> --path %s" % p0)
    log("  复验：ropatch.py <镜像> tree --part <分区> / --depth 3")
    return 0


def write_entry(img, e, data, align=0):
    """把 data 写进已存在的条目：能塞下就原位覆写，塞不下就追加到容器末尾。

    返回 (crc, data_off)。mode/uid/gid/mtime/xattr 全部保持不变。
    """
    crc = zlib.crc32(data) & 0xFFFFFFFF
    xb = img.xattr(e)
    if len(data) <= e["size"]:
        # 原位覆写：条目 size 改成新长度，物理位置不动，容器总大小不变
        with open(img.path, "r+b") as f:
            f.seek(e["off"])
            f.write(data)
            f.write(b"\0" * (e["size"] - len(data)))
            f.seek(e["at"] + 36)
            f.write(struct.pack("<II", len(data), e["off"]))
            f.seek(e["at"] + 44)
            f.write(struct.pack("<I", crc))
        log("✓ 原位覆写：off=%#x size=%d crc=%08x（容器大小不变，尾部补零）"
            % (e["off"], len(data), crc))
        return crc, e["off"]

    with open(img.path, "r+b") as f:
        f.seek(0, 2)
        new_off = f.tell()
        if align:
            pad = (-new_off) % align
            if pad:
                f.write(b"\0" * pad)
                new_off += pad
        f.seek(new_off)
        f.write(data)
        if xb:
            f.write(xb)
        # 条目：size(+36) data_off(+40) crc(+44) meta_len(+48)
        f.seek(e["at"] + 36)
        f.write(struct.pack("<IIII", len(data), new_off, crc, len(xb)))
        # 头：used_end(+12) last_data_off(+16)
        f.seek(12)
        f.write(struct.pack("<I", new_off + len(data) + len(xb)))
        f.seek(16)
        f.write(struct.pack("<I", new_off))
    log("✓ 追加写入：data_off=%#x size=%d crc=%08x" % (new_off, len(data), crc))
    log("  原数据留在 %#x（空洞，不再被引用）" % e["off"])
    log("  容器大小：%s -> %s" % (human(img.size), human(os.path.getsize(img.path))))
    return crc, new_off


def cmd_replace(img, idx, newfile, yes, backup, align, label="",
                drop_oat=False, keep_zip=False, force=False):
    e = img.entry(idx)
    if isdir(e["mode"]):
        log("[错误] 条目 %d 是目录，不能替换内容" % idx)
        return 2
    if kind(e["mode"]) == "link":
        log("[错误] 条目 %d 是符号链接，直接把内容写进去会写坏它（也写坏链接目标）" % idx)
        log("       先 show %d 看指向，再用 --path 指向真实文件" % idx)
        return 2
    # fscache.bin 是按路径名索引的 dentry 缓存，不会按 inode 截获这次改动；
    # 这里只做提示，不拦写入（早期按 inode 判定的告警是误报，会挡住正常操作）。
    fsc = os.path.join(os.path.dirname(img.path), "fscache.bin")
    if os.path.exists(fsc):
        log("[提示]   同分区有 fscache.bin（%s），是按路径名索引的缓存；"
            % human(os.path.getsize(fsc)))
        log("         若导入后仍读到旧内容，先跑 digest --expect 确认，再考虑整分区替换。")
        log("")
    if not newfile or not os.path.exists(newfile):
        log("[错误] 找不到新文件：%r" % newfile)
        return 2
    if os.path.getsize(newfile) == 0:
        log("[错误] 新文件是 0 字节，拒绝写入（读不到内容；空 jar 还会直接让系统起不来）")
        return 2
    data = open(newfile, "rb").read()
    name = os.path.basename(label) if label else ""
    # 以目标名字为准；只有条目号模式（不知道名字）才退而看新文件后缀
    is_jar = (name.lower().endswith(".jar") if name
              else os.path.basename(newfile).lower().endswith(".jar"))

    log("目标     : 条目 %d  %s  %s" % (idx, label or "（未给路径）", human(e["size"])))
    log("新文件   : %s  %s" % (newfile, human(len(data))))

    aot, boots = [], []
    if is_jar:
        oldp = {"ok": False, "err": "原条目为空", "dex": [], "names": [], "signed": []}
        if e["size"]:
            with open(img.path, "rb") as f:
                oldp = probe_jar(SubFile(f, e["off"], e["size"]))
        newp = probe_jar(data)
        log("")
        log("[JAR 检查]")
        jar_line("原 jar", e["size"], oldp)
        jar_line("新 jar", len(data), newp)
        if not newp["ok"]:
            log("[错误] 新文件不是合法 zip 容器：%s" % newp.get("err", "?"))
            log("       jar 本质是 zip，unzip -l 能列出内容才算；只改后缀名不算。")
            if not force:
                return 2
            log("[--force] 已指定强制写入，继续")
        else:
            if not newp["dex"]:
                log("[警告] 新 jar 里没有 classes*.dex。framework.jar/services.jar 这类 boot jar")
                log("       少了 dex 必然开不了机；只有纯资源 jar 才可能正常。")
            vo = sorted({d["ver"] for d in oldp.get("dex", []) if d["ver"]})
            vn = sorted({d["ver"] for d in newp["dex"] if d["ver"]})
            if vo and vn:
                if max(vn) > max(vo):
                    log("[警告] 新 dex 版本 %s 高于原 dex %s：设备 ART 不认更高版本，会装不上/开不了机"
                        % ("/".join(vn), "/".join(vo)))
                elif vn != vo:
                    log("[提示] dex 版本 %s -> %s（不高于原版本时可用）"
                        % ("/".join(vo), "/".join(vn)))
            if newp["signed"]:
                log("[警告] 新 jar 带 META-INF 签名：%s" % "、".join(newp["signed"][:4]))
                log("       平台 jar 一般不校验签名，但重打包一定会破坏它。")
            if oldp.get("ok"):
                oldn, newn = set(oldp["names"]), set(newp["names"])
                gone = [x for x in oldp["names"] if x not in newn]
                add = [x for x in newp["names"] if x not in oldn]
                if gone:
                    log("[提示] 比原 jar 少 %d 个成员：%s" % (
                        len(gone), "、".join(gone[:6]) + ("…" if len(gone) > 6 else "")))
                if add:
                    log("[提示] 比原 jar 多 %d 个成员：%s" % (
                        len(add), "、".join(add[:6]) + ("…" if len(add) > 6 else "")))
            if oldp.get("ok") and oldp["dex"] and newp["dex"]:
                old_stored = all(d["stored"] for d in oldp["dex"])
                deflated = [d["name"] for d in newp["dex"] if not d["stored"]]
                if old_stored and deflated and not keep_zip:
                    if newp["signed"]:
                        log("[跳过重打包] 新 jar 有签名，重打包会破坏签名；要强制按 STORED 请去签名后再来")
                    else:
                        before = len(data)
                        data = repack_store_dex(data)
                        newp = probe_jar(data)
                        log("[重打包] 原包 dex 是 STORED，新包是 DEFLATED -> 已按原包形态改成 STORED")
                        log("         %s -> %s（--keep-zip 可保留新包原样）"
                            % (human(before), human(len(data))))
                elif deflated and keep_zip:
                    log("[--keep-zip] 保持新包的 DEFLATED dex 不动（部分 ROM 的 dex2oat 会因此失败）")

        if label:
            dpath = os.path.dirname(label) or "/"
            stem = name[:-4]
            try:
                didx, _ = locate_entry(img, dpath)
                aot, boots = find_aot(img, didx, stem, dpath)
            except ValueError:
                aot, boots = [], []
            log("")
            if aot:
                log("[AOT] 找到 %d 个配套产物（都是按旧 jar 编译的）：" % len(aot))
                for p, i2, ch, where in aot[:10]:
                    log("       %-44s %-9s 条目 %-5d %s"
                        % (p, human(ch["size"]), i2, where))
                if len(aot) > 10:
                    log("       ... 另有 %d 个" % (len(aot) - 10))
                log("       换 jar 后 dex 校验和对不上，ART 会忽略这些产物、首次开机重新编译")
                log("       （慢几分钟，通常能起来）。想直接作废：加 --drop-oat")
            else:
                log("[AOT] 同目录与 oat/<abi>/ 下没有同名 .odex/.vdex/.oat/.art")
            if boots:
                log("[提示] 还发现 boot 镜像：%s" % "、".join(p for p, _i, _e in boots[:4]))
                log("       它内嵌了旧 framework 的类。jar 变了之后首机会退化成无镜像模式并重建，")
                log("       多数 ROM 能自愈；个别锁死 boot 镜像的 ROM 需要连 boot.art/boot.oat 一起换。")
        elif drop_oat:
            log("[提示] --drop-oat 需要配合 --path 使用（要靠目标路径找同目录的 AOT 文件）")
    else:
        log("xattr    : %d 字节（原样保留）" % len(img.xattr(e)))

    if not yes:
        log("")
        log("[演练] 未写入。确认无误后加 --yes")
        return 0
    fsc = os.path.join(os.path.dirname(img.path), "fscache.bin")
    if os.path.exists(fsc):
        log("[注意] 同分区还有 fscache.bin（%s）：它是 inode 元数据的另一份来源，"
            % human(os.path.getsize(fsc)))
        log("       本工具只改 readonly.bin。若导入后读到的还是旧内容，先查这里。")
    if backup:
        bak = img.path + ".bak"
        log("备份 -> %s" % bak)
        shutil.copy2(img.path, bak)

    if is_jar:
        log("")
    write_entry(img, e, data, align)

    if is_jar and drop_oat and aot:
        log("")
        log("[--drop-oat] 置零 %d 个旧 AOT 产物（boot.art/boot.oat 不动）…" % len(aot))
        for p, i2, ch, _where in aot:
            write_entry(img, img.entry(i2), b"")
            log("  ✓ %s  %s -> 0 B" % (p, human(ch["size"])))

    # 回读自检：确认条目表与数据一致（条目表已变，必须重新读头部）
    img.refresh()
    e2 = img.entry(idx)
    back = img.read(e2)
    ok = (len(back) == len(data)
          and (zlib.crc32(back) & 0xFFFFFFFF) == e2["crc"])
    log("")
    if not ok:
        log("[错误] 回读校验失败：写入 %d 字节，条目 now %d 字节 crc %08x"
            % (len(data), e2["size"], e2["crc"]))
        log("       本次改动不写回压缩包，请用 --backup 重新执行。")
        return 3
    log("回读校验通过：%d 字节，crc %08x" % (e2["size"], e2["crc"]))
    if is_jar:
        with open(img.path, "rb") as f:
            chk = probe_jar(SubFile(f, e2["off"], e2["size"]))
        if chk["ok"]:
            vers = sorted({d["ver"] for d in chk["dex"] if d["ver"]})
            log("容器复解析：正常，成员 %d 个，dex %d 个%s"
                % (chk["members"], len(chk["dex"]),
                   ("（" + "/".join(vers) + "）") if vers else ""))
        else:
            log("[错误] 容器复解析失败：%s" % chk.get("err", "?"))
            return 3
    log("")
    return 0


def zip_manifest(data):
    """把一个 zip/jar/apk 的「逻辑内容」抽出来：成员名 + 原始大小 + CRC。

    换 jar 时工具会按原包形态把 deflate 重打成 stored，字节必然变、sha256 必然不同，
    但里面的类一个没变。这个函数用来判断「是不是同一个包」，绕开压缩方式差异。
    """
    out = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for i in z.infolist():
                out.append((i.filename, i.file_size, i.CRC))
    except Exception:
        return None
    return out


def cmd_digest(img, path, expect=""):
    """算某个路径在镜像里的内容指纹，可和本地文件对比。

    回答的正是「我明明换了，怎么解出来还是原包」：这里读的是镜像里的字节，
    不经过任何导出/解包环节。和 --expect 文件一比，就知道改动到底进没进容器。
    """
    try:
        idx, label = locate_entry(img, path)
    except ValueError as e:
        log("[错误] %s" % e)
        return 2
    e = img.entry(idx)
    if isdir(e["mode"]):
        log("[错误] %s 是目录（条目 %d）" % (label, idx))
        return 2

    h = hashlib.sha256()
    crc = 0
    n = 0
    with open(img.path, "rb") as f:
        f.seek(e["off"])
        left = e["size"]
        while left > 0:
            b = f.read(min(1 << 20, left))
            if not b:
                break
            left -= len(b)
            n += len(b)
            crc = zlib.crc32(b, crc)
            h.update(b)
    crc &= 0xFFFFFFFF

    log("镜像     : %s" % getattr(img, "label", img.path))
    log("路径     : %s = 条目 %d" % (label, idx))
    log("内容     : %s  条目记录 crc %08x  size %d  off %#x" % (
        human(n), e["crc"], e["size"], e["off"]))
    log("实测     : %s  crc %08x" % (human(n), crc))
    log("sha256   : %s" % h.hexdigest())
    if crc != e["crc"]:
        log("[警告] 条目表 CRC 与实际内容不符，容器可能已被外部改动过")

    if not expect:
        return 0
    # 允许直接给摘要值（sha256 64 位 / crc 8 位），不必先存成文件
    if re.fullmatch(r"[0-9a-fA-F]{64}", expect):
        log("")
        log("对比摘要 : sha256 %s" % expect.lower())
        if h.hexdigest() == expect.lower():
            log("✓ 完全一致：镜像里就是这个文件")
            return 0
        log("✗ 不一致：镜像里是另一个文件（sha256 %s）" % h.hexdigest())
        return 1
    if re.fullmatch(r"[0-9a-fA-F]{8}", expect):
        log("")
        log("对比摘要 : crc32 %s" % expect.lower())
        if "%08x" % crc == expect.lower():
            log("✓ CRC 一致")
            return 0
        log("✗ CRC 不一致：镜像里是另一个文件（crc %08x）" % crc)
        return 1
    if not os.path.exists(expect):
        log("[错误] --expect 既不是摘要值（64 位 sha256 / 8 位 crc），也不是已存在的文件：%s" % expect)
        return 2
    en, ecrc = crc32_file(expect)
    eh = hashlib.sha256()
    with open(expect, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            eh.update(b)
    esha = eh.hexdigest()
    log("")
    log("对比文件 : %s（%d 字节）" % (expect, en))
    log("           crc %08x  sha256 %s" % (ecrc, esha))
    if n == en and crc == ecrc and h.hexdigest() == esha:
        log("")
        log("✓ 完全一致：镜像里就是这个文件")
        return 0

    log("")
    log("✗ 字节不一致（镜像 %d 字节 / 文件 %d 字节）" % (n, en))

    # jar/apk 在写回时会按原包形态重打 zip，字节必然变；比逻辑内容才有意义
    if path.lower().endswith((".jar", ".apk", ".zip")) and e["size"] <= (256 << 20):
        with open(img.path, "rb") as f:
            f.seek(e["off"])
            a = zip_manifest(f.read(e["size"]))
        with open(expect, "rb") as f:
            b = zip_manifest(f.read())
        if a is not None and b is not None:
            if a == b:
                log("  但逻辑内容完全一致：成员、大小、CRC 全部相同")
                log("  → 改动确实进包了，差异只是压缩方式/时间戳（写回时按原包重打成了 STORED）")
                return 0
            ma = {x[0]: x for x in a}
            mb = {x[0]: x for x in b}
            only_a = [k for k in ma if k not in mb]
            only_b = [k for k in mb if k not in ma]
            diff = [k for k in ma if k in mb and ma[k][2] != mb[k][2]]
            log("  逻辑内容也不同：镜像里独有 %d 个成员，文件独有 %d 个，%d 个成员内容不同"
                % (len(only_a), len(only_b), len(diff)))
            for k in only_a[:5]:
                log("    仅镜像有: %s" % k)
            for k in only_b[:5]:
                log("    仅文件有: %s" % k)
            for k in diff[:5]:
                log("    CRC 不同: %s" % k)
            log("  → 镜像里装的还是另一个版本（很可能是替换没真正写回）")
            return 1

    if n == en:
        log("  字节数相同但内容不同 —— 常见于「替换后没写回」或「解出来的还是旧包」")
    else:
        log("  字节数也不同 —— 镜像里是另一个版本")
    return 1


def cmd_check(img, path, cache=True):
    """导入前体检：这个文件在镜像里是不是「结构上找得到」。"""
    from_file = getattr(img, "label", img.path)
    log("镜像     : %s" % from_file)
    try:
        idx, label = locate_entry(img, path)
    except ValueError as exc:
        log("[✗] 路径解析失败：%s" % exc)
        log("    名字不在任何目录项里，等于这个文件不存在。")
        return 2

    e = img.entry(idx)
    log("路径     : %s = 条目 %d" % (label, idx))
    log("属性     : %s  uid=%d gid=%d  size=%s"
        % (mode_str(e["mode"]), e["uid"], e["gid"], human(e["size"])))

    ok = True
    warn_cache = False
    if not isdir(e["mode"]):
        with open(img.path, "rb") as f:
            f.seek(e["off"])
            left, crc = e["size"], 0
            while left > 0:
                b = f.read(min(1 << 20, left))
                if not b:
                    break
                left -= len(b)
                crc = zlib.crc32(b, crc)
            crc &= 0xFFFFFFFF
        if crc == e["crc"]:
            log("内容     : ✓ CRC %08x 与条目表一致" % crc)
        else:
            log("内容     : ✗ CRC %08x，条目表记的是 %08x —— 数据被截断或改过"
                % (crc, e["crc"]))
            ok = False

    # 父目录里这一项的位置：决定引擎按名字查找能不能命中
    segs = [s for s in label.split("/") if s]
    name = segs[-1] if segs else ""
    parent = "/" + "/".join(segs[:-1])
    try:
        pidx, plabel = locate_entry(img, parent or "/")
    except ValueError as exc:
        log("[✗] 父目录 %s 解析失败：%s" % (parent, exc))
        return 2
    pe = img.entry(pidx)
    items, _al, used, _f = dir_children(img, pe)
    names = [n for _i, n in items]
    raw = [n.encode("utf-8") for n in names]
    ordered = all(raw[k] < raw[k + 1] for k in range(len(raw) - 1))
    hits = [i for i, n in items if n == name]
    log("父目录   : %s（条目 %d） 子项 %d 个，数据 %s/%s"
        % (plabel, pidx, len(items), human(used), human(pe["size"])))
    if not hits:
        log("           ✗ 目录项里没有 %r —— 引擎扫目录时看不到它" % name)
        return 2
    if hits[0] != idx:
        log("           ✗ 目录项里的 %r 指向条目 %d，不是 %d" % (name, hits[0], idx))
        ok = False
    else:
        log("           ✓ 目录项 %r -> 条目 %d" % (name, idx))
    if ordered:
        pos = names.index(name)
        want = sorted(names, key=lambda n: n.encode("utf-8")).index(name)
        if pos == want:
            log(" 排序     : ✓ 目录按名字升序，这一项在第 %d 位（位置正确）" % (pos + 1))
        else:
            log(" 排序     : ✗ 目录按名字升序，但这一项在第 %d 位，应在第 %d 位"
                % (pos + 1, want + 1))
            log("             引擎按名字二分/顺序查找时会跳过它 —— 典型症状是「应用不见了」")
            ok = False
    else:
        # 整个目录无序：看看是不是只有这一项破坏了顺序（旧版 append 的典型后果）
        pos0 = names.index(name)
        rest = [n.encode("utf-8") for k, n in enumerate(names) if k != pos0]
        rest_ok = all(rest[k] < rest[k + 1] for k in range(len(rest) - 1))
        if rest_ok and len(raw) > 1:
            log(" 排序     : ✗ 其余 %d 项本来是有序的，只有这一项破坏了顺序" % len(rest))
            log("             引擎按名字查找会跳过它 —— 典型症状是「应用不见了」")
            log("             重做：把这一项删掉重新 add（新版会自动按名字插入）")
            ok = False
        else:
            log(" 排序     : ! 该目录本身无序，无法判断位置（引擎一般会全量遍历）")

    if name.lower().endswith(".apk") and not isdir(e["mode"]):
        with open(img.path, "rb") as f:
            f.seek(e["off"])
            magic = f.read(2)
            if magic == b"PK":
                try:
                    z = zipfile.ZipFile(SubFile(f, e["off"], e["size"]))
                    man = z.read("AndroidManifest.xml")
                    pkgs = guess_pkg(man)
                    log("APK      : ✓ 合法 zip，manifest %s，包名 %s"
                        % (human(len(man)), pkgs[0] if pkgs else "（读不出，可能是空壳）"))
                except KeyError:
                    log("APK      : ! 没有 AndroidManifest.xml，PMS 不会把它当应用（占位文件属正常）")
                except Exception as exc:
                    log("APK      : ✗ 打不开：%s" % exc)
                    ok = False
            else:
                log("APK      : ✗ 开头不是 PK，不是合法 APK（开机扫描可能卡住）")
                ok = False

    if cache:
        fsc = os.path.join(os.path.dirname(img.path), "fscache.bin")
        if os.path.exists(fsc):
            with open(fsc, "rb") as f:
                data = f.read()
            cands = [label]
            if not path.lstrip("/").startswith(("system", "vendor", "product")):
                cands.append("/" + segs[0] if segs else path)
            hit = [c for c in cands if c.encode("utf-8") in data]
            log("缓存     : fscache.bin %s，%s"
                % (human(len(data)),
                   "含 %s" % hit[0] if hit else "不含该路径"))
            if not hit:
                warn_cache = True
                base = label.rsplit("/", 1)[0]
                sib = [n for n in names if n != name][:8]
                got2 = [n for n in sib if (base + "/" + n).encode("utf-8") in data]
                log("           路径不在缓存里：这只是按路径索引的 dentry 缓存，")
                log("           新路径缺失本身不拦目录遍历；仍报错时优先看上面的『排序』行。")
                if sib:
                    log("           对照：同目录 %d 个老条目，缓存里命中 %d 个%s"
                        % (len(sib), len(got2),
                           "  ← 缓存较完整，但不足以判定新路径会被拦截" if len(got2) == len(sib)
                           else "  ← 缓存不全，更说明它只是部分缓存"))

    log("")
    if ok:
        log("✓ 结构自洽：路径、目录项、内容都对得上")
        if warn_cache:
            log("ℹ 提示：该路径不在 fscache.bin 里（dentry 缓存按路径索引，不拦新路径）")
        return 0
    log("✗ 有问题，逐条见上面 ✗ 行")
    return 1


def walk_paths(img, cap=300000):
    """整棵树遍历，返回 [(绝对路径, 条目号)]，用 visited 防环。"""
    out = []
    seen = set()

    def walk(i, prefix):
        if i in seen or len(out) >= cap:
            return
        seen.add(i)
        e = img.entry(i)
        if not isdir(e["mode"]):
            return
        for ino, nm in dir_children(img, e)[0]:
            p = prefix + "/" + nm
            out.append((p, ino))
            if 0 <= ino < img.count and isdir(img.entry(ino)["mode"]):
                walk(ino, p)

    walk(0, "")
    return out


def cmd_fscache(img, path, limit):
    """判定 fscache.bin 是「部分缓存」还是「完整清单」。

    引擎在分区初始化时对 fscache.bin 做 openat + mmap(O_RDWR)，目录查找优先走它。
    若它记的是完整目录树，新增条目不在其中就永远查不到 —— 现象是应用凭空消失；
    若只是热点缓存，缺失会回落遍历目录，不影响新增。
    这个命令用「镜像里全部路径 vs 缓存命中率」直接给结论。
    """
    fsc = os.path.join(os.path.dirname(img.path), "fscache.bin")
    where = "分区目录"
    if not os.path.exists(fsc):
        # 多数 ROM 把 fscache.bin 放在 fs 根（androidfs_10.0.0/fscache.bin），
        # 跨分区共用一份，而不是每个分区目录一份。
        up = os.path.join(os.path.dirname(os.path.dirname(img.path)), "fscache.bin")
        if os.path.exists(up):
            fsc, where = up, "fs 根"
    if not os.path.exists(fsc):
        log("[错误] 没找到 fscache.bin（分区目录和 fs 根都没有）")
        log("       该 ROM 不带路径缓存，新增不受缓存影响")
        return 2
    with open(fsc, "rb") as f:
        data = f.read()
    part = os.path.basename(os.path.dirname(img.path))
    log("文件      : %s  %s（%s）" % (fsc, human(len(data)), where))

    if path:
        try:
            idx, label = locate_entry(img, path)
        except ValueError as exc:
            log("[错误] %s" % exc)
            return 2
        cands = ["/" + part + label, label]
        hit = [c for c in cands if c.encode("utf-8") in data]
        log("查询      : %s（条目 %d）" % (label, idx))
        log("命中      : %s" % ("是，缓存里是 %s" % hit[0] if hit else "否，缓存里没有这条路径"))
        if not hit:
            d = os.path.dirname(label) or "/"
            try:
                didx, dlabel = locate_entry(img, d)
                sib = [n for n in dir_children(img, img.entry(didx))[0]]
                got = [n for _, n in sib
                       if ("/" + part + dlabel + "/" + n).encode("utf-8") in data
                       or (dlabel + "/" + n).encode("utf-8") in data]
                log("同目录    : %d 项，缓存里命中 %d 项%s"
                    % (len(sib), len(got),
                       "  ← 全命中，缓存是完整清单" if sib and len(got) == len(sib) else ""))
            except ValueError:
                pass
        log("")

    paths = walk_paths(img)
    if not paths:
        log("[错误] 没解析出任何路径，先跑 tree 看看")
        return 2
    filed = [(p, i) for p, i in paths if not isdir(img.entry(i)["mode"])]
    dird = [(p, i) for p, i in paths if isdir(img.entry(i)["mode"])]

    def rate(items):
        n = 0
        for p, _ in items:
            if p.encode("utf-8") in data or ("/" + part + p).encode("utf-8") in data:
                n += 1
        return n, (100.0 * n / len(items) if items else 0.0)

    fn, fr = rate(filed)
    dn, dr = rate(dird)
    log("镜像      : 文件 %d 个，目录 %d 个" % (len(filed), len(dird)))
    log("命中      : 文件 %d/%d (%.1f%%)，目录 %d/%d (%.1f%%)"
        % (fn, len(filed), fr, dn, len(dird), dr))
    miss = [p for p, _ in filed if p.encode("utf-8") not in data][:limit]
    if miss:
        log("未命中示例:")
        for p in miss:
            log("           %s" % p)
    log("")
    if fr >= 80:
        log("结论      : ✗ 缓存里几乎全部路径都在 —— 这是**完整清单**，不是热点缓存。")
        log("           新增条目不在里面，引擎按名字查不到，应用就会「凭空消失」。")
        log("           替换同路径能生效，是因为那条路径本来就在清单里。")
    elif fr <= 20:
        log("结论      : ✓ 缓存只覆盖少数路径 —— 是部分缓存，缺失会回落目录遍历。")
        log("           新增条目不受它影响，问题在别处（PMS 扫描 / data 分区旧清单）。")
    else:
        log("结论      : ? 命中 %.1f%%，介于两者之间，需结合 check 的排序行一起判断。" % fr)
    return 0


def cmd_sortdir(img, path, yes):
    """把一个目录的子项按名字升序重排（只动目录数据，inode 与内容都不变）。

    用来补救旧版 append 造成的「应用凭空消失」：目录项本身没坏，只是顺序不对，
    引擎按名字查找时跳过了它。重排后 size 通常不变，走原位覆写。
    """
    try:
        idx, label = locate_entry(img, path)
    except ValueError as exc:
        log("[错误] %s" % exc)
        log("       先 tree 看一眼目录名")
        return 2
    e = img.entry(idx)
    if not isdir(e["mode"]):
        log("[错误] %s 是文件，不是目录" % label)
        return 2

    items, al, used, ino_first = dir_children(img, e)
    if not items:
        log("[提示] %s 是空目录，无需处理" % label)
        return 0
    names = [n for _i, n in items]
    raw = [n.encode("utf-8") for n in names]
    ordered = all(raw[k] < raw[k + 1] for k in range(len(raw) - 1))
    log("目录     : %s = 条目 %d" % (label, idx))
    log("子项     : %d 个，数据 %s/%s，%s"
        % (len(items), human(used), human(e["size"]),
           "已按名字升序" if ordered else "顺序被破坏"))
    if ordered:
        log("")
        log("✓ 已经是升序，不用处理")
        return 0

    want = sorted(items, key=lambda t: t[1].encode("utf-8"))
    body = pack_dirents(want, al, ino_first)
    pos_old = {n: k + 1 for k, n in enumerate(names)}
    pos_new = {n: k + 1 for k, n in enumerate([w[1] for w in want])}
    moved = [(n, pos_old[n], pos_new[n]) for n in names if pos_old[n] != pos_new[n]]
    log("重排     : 需要移动 %d 项，例如" % len(moved))
    for nm, a, b in moved[:5]:
        log("           %-32s 第 %d 位 -> 第 %d 位" % (nm, a, b))
    log("目录数据 : %s -> %s" % (human(e["size"]), human(len(body))))
    if not yes:
        log("")
        log("[演练] 未写入。加 --yes 执行（原数据做成空洞，条目指向新位置）")
        return 0

    xb = img.xattr(e)
    write_entry(img, e, body, 0)
    img.refresh()
    e2 = img.entry(idx)
    items2, _a2, _u2, _f2 = dir_children(img, e2)
    got = [n for _i, n in items2]
    if {n: i for i, n in items2} != {n: i for i, n in items}:
        log("[错误] 重排后子项对不上（%d -> %d 项），请从原始包重做" % (len(items), len(items2)))
        return 2
    g = [n.encode("utf-8") for n in got]
    if any(g[k] >= g[k + 1] for k in range(len(g) - 1)):
        log("[错误] 重排后仍不是升序，请从原始包重做")
        return 2
    log("✓ 已重排：%d 项全部升序，inode 与内容未变" % len(got))
    return 0


# ---------------------------------------------------------------------------
# 覆盖层：androidfs_<ver>/<分区>/<桶>/<inode>
#
# 引擎打开文件时先看覆盖层，命中就用覆盖层，没命中才回 readonly.bin。
# 目录在覆盖层里存的是「纯文本目录表」，一行一项：
#     名字=<十六进制 inode>\r\n
# 同名 <inode>.metadata 是 99~140 字节的 ATEM 头（mode/uid/gid/时间/xattr）。
# 覆盖层里一旦出现某个目录的目录表，引擎就只认它，只读层那份被整个忽略 ——
# 所以往 system 加应用不必碰 1.2 GB 的 readonly.bin，写几个小文件就行。
# ---------------------------------------------------------------------------

ATEM_MAGIC = b"ATEM"
ATEM_VER = 0x30
DEF_SELINUX = b"u:object_r:system_file:s0"


def build_meta(mode, uid=0, gid=0, nlink=1, mtime=0,
               selinux=DEF_SELINUX):
    """拼一个 ATEM 元数据块（52 字节头 + xattr 区）"""
    if mtime <= 0:
        mtime = int(time.time())
    if isinstance(selinux, str):
        selinux = selinux.encode("utf-8")
    if not selinux.endswith(b"\0"):
        selinux += b"\0"
    name = b"security.selinux\0"
    x = name + struct.pack("<I", len(selinux)) + selinux
    head = ATEM_MAGIC + struct.pack("<I", ATEM_VER)
    head += struct.pack("<IIII", mode, uid, gid, nlink)
    head += struct.pack("<IIII", mtime, mtime, mtime, mtime)
    head += struct.pack("<I", 0)              # +40 保留
    head += struct.pack("<I", 4 + len(x))     # +44 xattr 区总长（含条目数字段）
    head += struct.pack("<I", 1)              # +48 xattr 个数
    return head + x


def parse_meta(data):
    """解 ATEM 头，返回 mode/uid/gid/xattr 字典；不合法返回 None"""
    if len(data) < 52 or data[:4] != ATEM_MAGIC:
        return None
    mode, uid, gid, nlink = struct.unpack_from("<IIII", data, 8)
    mtime, ctime, atime, ctime2 = struct.unpack_from("<IIII", data, 24)
    _z, _alen, xn = struct.unpack_from("<III", data, 40)
    out = {"mode": mode, "uid": uid, "gid": gid, "nlink": nlink,
           "mtime": mtime, "ctime": ctime, "atime": atime, "xattr": {}}
    p = 52
    try:
        for _ in range(xn):
            e = data.index(b"\0", p)
            name = data[p:e].decode("utf-8", "replace")
            p = e + 1
            ln = struct.unpack_from("<I", data, p)[0]
            p += 4
            out["xattr"][name] = data[p:p + ln]
            p += ln
    except (ValueError, struct.error):
        pass
    return out


def locate_androidfs(p):
    """把用户给的路径解析成 androidfs_<ver> 目录本身。

    接受：androidfs_10.0.0 / 它的上级（含 raw/）/ 某个分区目录 / readonly.bin 路径。
    """
    p = os.path.abspath(p)
    if os.path.isfile(p):
        p = os.path.dirname(p)
    if not os.path.isdir(p):
        return ""
    if os.path.basename(p).startswith("androidfs_"):
        return p
    par = os.path.dirname(p)
    if os.path.basename(par).startswith("androidfs_"):
        return par
    for base in (p, os.path.join(p, "raw")):
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            full = os.path.join(base, name)
            if name.startswith("androidfs_") and os.path.isdir(full):
                return full
    return ""


class OvlFS:
    """androidfs_<ver>/<分区>/ 下的覆盖层，按 inode 分桶存放"""

    def __init__(self, fs_root, part):
        self.root = fs_root
        self.part = (part or "system").strip("/")
        self.base = os.path.join(fs_root, self.part)
        self.touched = []

    def path(self, ino, meta=False):
        p = os.path.join(self.base, "%08x" % (ino >> 12),
                         "%08x%s" % (ino, ".metadata" if meta else ""))
        return p

    def has(self, ino):
        return os.path.exists(self.path(ino))

    def all_inodes(self):
        s = set()
        if not os.path.isdir(self.base):
            return s
        for b in os.listdir(self.base):
            d = os.path.join(self.base, b)
            if not os.path.isdir(d):
                continue
            try:
                int(b, 16)
            except ValueError:
                continue
            for f in os.listdir(d):
                if f.endswith(".metadata"):
                    continue
                try:
                    s.add(int(f, 16))
                except ValueError:
                    pass
        return s

    def meta_of(self, ino):
        p = self.path(ino, True)
        if not os.path.exists(p):
            return None
        with open(p, "rb") as f:
            return parse_meta(f.read())

    def read_dir(self, ino):
        """读覆盖层里的目录表，返回 [(ino, 名字)]；不存在返回 None"""
        p = self.path(ino)
        if not os.path.exists(p):
            return None
        with open(p, "rb") as f:
            data = f.read()
        if not data:
            return []
        out = []
        for line in data.split(b"\r\n"):
            if not line:
                continue
            k = line.rfind(b"=")
            if k <= 0:
                raise ValueError("%s 不是合法的目录表（行 %r）" % (p, line[:40]))
            try:
                out.append((int(line[k + 1:], 16), line[:k].decode("utf-8")))
            except (ValueError, UnicodeDecodeError):
                raise ValueError("%s 第 %d 行解析不了：%r"
                                 % (p, len(out) + 1, line[:40]))
        return out

    def write_dir(self, ino, items, mode=0o40755, mtime=0):
        p = self.path(ino)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        body = b"".join(("%s=%x\r\n" % (n, i)).encode("utf-8") for i, n in items)
        with open(p, "wb") as f:
            f.write(body)
        with open(self.path(ino, True), "wb") as f:
            f.write(build_meta(mode, mtime=mtime))
        self.touched.append(p)
        self.touched.append(self.path(ino, True))
        return len(body)

    def write_file(self, ino, src, mode=0o100644, mtime=0, size=0):
        p = self.path(ino)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(src, "rb") as fi, open(p, "wb") as fo:
            shutil.copyfileobj(fi, fo, 4 << 20)
        ost = os.path.getmtime(src)
        with open(self.path(ino, True), "wb") as f:
            f.write(build_meta(mode, mtime=mtime or int(ost)))
        self.touched.append(p)
        self.touched.append(self.path(ino, True))
        return size or os.path.getsize(p)

    def remove(self, ino):
        for p in (self.path(ino), self.path(ino, True)):
            if os.path.exists(p):
                os.remove(p)
                self.touched.append(p)

    # --- superblock：16 字节，「BPUS」+ 头长 0x10 + inode 水位 + 0 ---
    # 实测：原包只读层最大 inode 4517 时水位 0x11a6(4518)；
    # 改过的包覆盖层最大 inode 0x12a5(4773) 时水位 0x12a6(4774)。
    # 所以它是「下一个可分配的 inode」，不是条目数。加了新 inode 不同步它，
    # 引擎可能认为该 inode 不存在 —— 这正是「进包了却打不开」的一类原因。
    def super_path(self):
        return os.path.join(self.root, self.part, "superblock.bin")

    def read_super(self):
        p = self.super_path()
        if not os.path.exists(p):
            return None
        with open(p, "rb") as f:
            b = f.read()
        if len(b) < 12 or b[:4] != b"BPUS":
            return None
        return struct.unpack_from("<I", b, 8)[0]

    def write_super(self, nxt):
        p = self.super_path()
        if not os.path.exists(p):
            return None
        with open(p, "rb") as f:
            b = bytearray(f.read())
        if len(b) < 12 or b[:4] != b"BPUS":
            return None
        old = struct.unpack_from("<I", b, 8)[0]
        if nxt <= old:
            return old
        struct.pack_into("<I", b, 8, nxt)
        with open(p, "wb") as f:
            f.write(bytes(b))
        self.touched.append(p)
        return nxt


def ovl_dir_view(img, ovl, path):
    """对比某个目录在只读层与覆盖层里的差异，返回 (idx, 只读层项, 覆盖层项)"""
    idx, label = locate_dir(img, path)
    ent = img.entry(idx)
    base_items = list(dir_children(img, ent)[0])
    ov_items = None
    try:
        ov_items = ovl.read_dir(idx)
    except ValueError as e:
        log("[警告] %s" % e)
    return idx, label, base_items, ov_items


def cmd_ovl(img, ovl, path):
    """看覆盖层现状：哪些 inode 被覆盖、某个目录有没有被接管"""
    log("覆盖层根 : %s" % ovl.base)
    if not os.path.isdir(ovl.base):
        log("           目录不存在（该分区还没有任何覆盖写入）")
    sb = ovl.read_super()
    log("superblock: %s" % ("没有" if sb is None else "inode 水位 0x%x (%d)" % (sb, sb)))
    inodes = ovl.all_inodes()
    log("已覆盖   : %d 个 inode" % len(inodes))
    if inodes:
        sized = []
        for i in sorted(inodes):
            p = ovl.path(i)
            if os.path.exists(p):
                sized.append((os.path.getsize(p), i))
        sized.sort(reverse=True)
        for sz, i in sized[:12]:
            m = ovl.meta_of(i) or {}
            log("  %08x  %-9s %o" % (i, human(sz), m.get("mode", 0) & 0o7777))
        if len(sized) > 12:
            log("  …（共 %d 个）" % len(sized))

    if not path:
        return 0
    try:
        idx, label, base_items, ov_items = ovl_dir_view(img, ovl, path)
    except ValueError as e:
        log("[错误] %s" % e)
        return 2
    log("")
    log("目录     : %s = 条目 %d (0x%x)" % (label, idx, idx))
    log("只读层   : %d 项" % len(base_items))
    if ov_items is None:
        log("覆盖层   : 未接管（引擎读只读层那份）")
        return 0
    log("覆盖层   : %d 项（已接管，只读层那份被忽略）" % len(ov_items))
    bset = {n: i for i, n in base_items}
    oset = {n: i for i, n in ov_items}
    gone = [n for n in bset if n not in oset]
    new = [n for n in oset if n not in bset]
    moved = [(n, bset[n], oset[n]) for n in bset if n in oset and bset[n] != oset[n]]
    for n in gone[:12]:
        log("   - 少了 %s（只读层是 %x）" % (n, bset[n]))
    for n in new[:12]:
        log("   + 多了 %s（%x）" % (n, oset[n]))
    for n, a, b in moved[:12]:
        log("   ~ %s inode %x -> %x" % (n, a, b))
    if not (gone or new or moved):
        log("   = 与只读层完全一致")
    return 0


def cmd_install(img, ovl, target_dir, apk_path, name, mode_bits, yes, force,
                dry_only=False):
    """把 APK 装进只读分区的某个应用目录 —— 全程只写覆盖层，不动 readonly.bin"""
    if not os.path.isfile(apk_path):
        log("[错误] 找不到文件：%s" % apk_path)
        return 2
    try:
        idx, label, base_items, ov_items = ovl_dir_view(img, ovl, target_dir)
    except ValueError as e:
        log("[错误] 目标目录解析失败：%s" % e)
        log("       先用 tree --part %s / 看一眼目录名" % ovl.part)
        return 2

    ent = img.entry(idx)
    src = "覆盖层（已接管）" if ov_items is not None else "只读层"
    items = list(ov_items) if ov_items is not None else list(base_items)
    log("目标目录 : %s = 条目 %d (0x%x)，现有 %d 项，来自 %s"
        % (label, idx, idx, len(items), src))

    base = (name or os.path.splitext(os.path.basename(apk_path))[0]).strip()
    if not base or any(c in base for c in "=\r\n/\\"):
        log("[错误] 目录名不合法：%r（不能含 = / \\ 换行）" % base)
        return 2
    apk_name = base + ".apk"

    hit = [(i, n) for i, n in items if n == base]
    if hit and not force:
        log("[错误] %s 下已有 %r（条目 0x%x）" % (label, base, hit[0][0]))
        log("       换个名字：--name 别的名字")
        log("       确实要覆盖：加 --force（复用原目录 inode，只换里面的 apk）")
        return 2

    size, crc = crc32_file(apk_path)
    if size <= 0:
        log("[错误] 文件是空的：%s" % apk_path)
        return 2
    if size > 0xFFFFFFFF:
        log("[错误] 超过 4 GB，只读层的 size 字段放不下")
        return 2
    with open(apk_path, "rb") as f:
        magic = f.read(4)
    if magic[:2] != b"PK":
        log("[警告] 这不是 zip/APK（前 4 字节 %r），引擎大概率装不上" % magic)

    pkg = ""
    try:
        with zipfile.ZipFile(apk_path) as z:
            cand = guess_pkg(z.read("AndroidManifest.xml"))
        pkg = cand[0] if cand else ""
    except Exception:
        pass

    # 分配 inode：只读层占 0..count-1，覆盖层里已有的也要避开
    used = set(range(img.count)) | ovl.all_inodes()
    nxt = max(used) + 1 if used else 1

    if hit:
        ino_dir = hit[0][0]
        old = ovl.read_dir(ino_dir) or []
        apk_hit = [i for i, n in old if n.lower().endswith(".apk")]
        ino_apk = apk_hit[0] if apk_hit else nxt
        log("复用目录 : %s = 0x%x，apk 用 0x%x" % (base, ino_dir, ino_apk))
    else:
        ino_dir = nxt
        ino_apk = nxt + 1

    log("")
    log("新增条目 :")
    log("  %-28s inode 0x%x  目录 0755" % (base + "/", ino_dir))
    log("  %-28s inode 0x%x  %s  %s" % (base + "/" + apk_name, ino_apk, human(size), pkg or "包名未知"))
    log("  覆盖层目录表 : %s" % ovl.path(idx))
    log("                %s" % ovl.path(ino_dir))
    log("  新 inode 起点 : 0x%x（只读层 %d 条已占 0..0x%x）"
        % (nxt, img.count, img.count - 1))
    cur_super = ovl.read_super()
    new_super = max(ino_dir, ino_apk) + 1
    if cur_super is None:
        log("  superblock   : 没有，跳过水位同步")
    elif new_super > cur_super:
        log("  superblock   : 水位 0x%x -> 0x%x" % (cur_super, new_super))
    else:
        log("  superblock   : 水位 0x%x，已够（新 inode 都低于它）" % cur_super)
    log("")

    if not yes or dry_only:
        log("[演练] 未写入。确认无误后加 --yes 执行")
        log("       只写覆盖层，readonly.bin 一个字节都不动")
        return 0

    mb = int(mode_bits, 8) if isinstance(mode_bits, str) else mode_bits
    if hit:
        # 覆盖时保持原来的位置，不把这一行挪到末尾
        items2 = [((ino_dir if n == base else i), n) for i, n in items]
    else:
        items2 = [(i, n) for i, n in items] + [(ino_dir, base)]
    n1 = ovl.write_dir(idx, items2, mode=ent["mode"])
    n2 = ovl.write_dir(ino_dir, [(ino_apk, apk_name)], mode=0o40755)
    got = ovl.write_file(ino_apk, apk_path, mode=0o100000 | mb)
    sup = ovl.write_super(new_super)

    # 落盘后回读：目录表行数与内容、apk 字节数都必须对得上
    back = ovl.read_dir(idx) or []
    if (ino_dir, base) not in back or len(back) != len(items2):
        log("[错误] 目录表回读不一致（写 %d 项，读 %d 项）" % (len(items2), len(back)))
        return 2
    back2 = ovl.read_dir(ino_dir) or []
    if (ino_apk, apk_name) not in back2:
        log("[错误] 应用目录回读不一致")
        return 2
    if os.path.getsize(ovl.path(ino_apk)) != size:
        log("[错误] apk 落盘大小不符")
        return 2

    log("✓ 已写入覆盖层")
    log("  %s  %d 字节（%d 行）" % (ovl.path(idx), n1, len(items2)))
    log("  %s  %d 字节（%d 行）" % (ovl.path(ino_dir), n2, len(back2)))
    log("  %s  %s" % (ovl.path(ino_apk), human(got)))
    if sup and sup != cur_super:
        log("  %s  inode 水位 -> 0x%x" % (ovl.super_path(), sup))
    log("")
    log("readonly.bin 未改动，CRC 与 used_end 都保持原样。")
    log("接下来：把整个 androidfs 目录重新打包回 7z，装进虚拟机验证。")
    return 0


def cmd_uninstall(img, ovl, target_dir, name, yes):
    """从覆盖层里摘掉一个应用（把目录表那一行删掉，并清理它的 inode 文件）"""
    try:
        idx, label, base_items, ov_items = ovl_dir_view(img, ovl, target_dir)
    except ValueError as e:
        log("[错误] %s" % e)
        return 2
    if ov_items is None:
        log("[提示] %s 没有被覆盖层接管，没有可摘的东西" % label)
        return 0
    hit = [(i, n) for i, n in ov_items if n == name]
    if not hit:
        log("[错误] %s 的覆盖层里没有 %r" % (label, name))
        log("       现有：%s" % "、".join(n for _i, n in ov_items[:20]))
        return 2
    ino_dir = hit[0][0]
    inner = ovl.read_dir(ino_dir) or []
    log("将删除   : %s = inode 0x%x" % (name, ino_dir))
    if inner:
        log("           其中 %d 项：%s" % (len(inner), "、".join(n for _i, n in inner[:8])))
    log("覆盖层目录表会少这一行，只读层那份仍然保留（摘掉后恢复到只读层的原始状态）")
    if not yes:
        log("")
        log("[演练] 未写入。加 --yes 执行")
        return 0

    items2 = [(i, n) for i, n in ov_items if n != name]
    ovl.write_dir(idx, items2, mode=img.entry(idx)["mode"])
    for i, _n in inner:
        ovl.remove(i)
    ovl.remove(ino_dir)

    # 剩下这些和只读层一字不差时，把目录表也撤掉，彻底回到出厂状态
    base = {n: i for i, n in dir_children(img, img.entry(idx))[0]}
    if {n: i for i, n in items2} == base:
        ovl.remove(idx)
        log("✓ 已从覆盖层摘除 %s，剩余项与只读层一致，目录表也撤了" % name)
        log("  该目录回到「引擎读只读层」的状态")
    else:
        log("✓ 已从覆盖层摘除 %s（剩余 %d 项，仍接管该目录）" % (name, len(items2)))
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


def normalize_argv(argv, cmds, opts_with_value):
    """把「镜像路径 / 写在子命令前的全局选项」挪到子命令之后。

    ropatch.py fs.7z info
    ropatch.py fs.7z --member X replace --path /framework/framework.jar a.jar
    ropatch.py --member X fs.7z replace ...
    都规范成 <子命令> <镜像> <全局选项…> <其余参数>
    """
    cmd_i = None
    for i, t in enumerate(argv):
        if t in cmds:
            cmd_i = i
            break
    if cmd_i is None or cmd_i == 0:
        return argv
    head = argv[:cmd_i]
    image = ""
    opts = []
    i = 0
    while i < len(head):
        t = head[i]
        if t.startswith("-"):
            opts.append(t)
            if t in opts_with_value and i + 1 < len(head):
                opts.append(head[i + 1])
                i += 2
                continue
            i += 1
            continue
        if not image:
            image = t
        i += 1
    return [argv[cmd_i]] + ([image] if image else []) + opts + argv[cmd_i + 1:]


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--part", default="",
                        help="ROM 模式：指定分区，如 system / vendor / data")
    common.add_argument("--keep", action="store_true", help="保留临时解出的 readonly.bin")
    common.add_argument("--no-writeback", action="store_true",
                        help="改完不写回 7z，只改临时副本")
    common.add_argument("--fresh", action="store_true",
                        help="丢弃解压缓存，重新从 7z 解出")
    common.add_argument("--member", default="",
                        help="直接指定包内成员全路径，如 androidfs_10.0.0/system/readonly.bin"
                             "（包里有多个同名 readonly.bin 时用它锁定）")

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
            "  python3 ropatch.py fs.7z add --part system /system/app --from ./新应用 --mkdir --yes",
            "  python3 ropatch.py fs.7z jars --part system --name framework",
            "  python3 ropatch.py fs.7z extract --part system --path /framework/framework.jar -o old.jar",
            "  python3 ropatch.py fs.7z replace --part system --path /framework/framework.jar new.jar --yes",
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

    p = sub.add_parser("jars", parents=[common],
                       help="列出所有 .jar 的完整路径（换 framework.jar 前先用它定位）")
    p.add_argument("image")
    p.add_argument("--name", default="", help="只列路径含该串的，如 framework")
    p.add_argument("--min-size", default="0", help="只看大于该大小的，默认 0")
    p.add_argument("--max-probe", default="64M",
                   help="超过该大小的不做 dex 解析，默认 64M")
    p.add_argument("--limit", type=int, default=60)

    p = sub.add_parser("extract", parents=[common],
                       help="导出某个条目（条目号或 --path）")
    p.add_argument("image")
    p.add_argument("idx", type=int, nargs="?", default=None)
    p.add_argument("-o", "--out", default="")
    p.add_argument("--path", default="", help="按路径导出，如 /framework/framework.jar")

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
                       help="在目录下新增文件条目（支持一次加一个文件夹，只重写一次）")
    p.add_argument("image")
    p.add_argument("dir", help="父目录：条目号（如 12）或路径（如 /system/app）")
    p.add_argument("name", nargs="?", default="", help="新文件名，如 NewApp.apk")
    p.add_argument("file", nargs="?", default="", help="要写入的内容文件")
    p.add_argument("--from", dest="from_dir", default="",
                   help="把该目录下的 .apk/.jar 全部加进去（用原文件名）")
    p.add_argument("--mkdir", action="store_true",
                   help="目标目录不存在时自动创建（支持多级，如 /system/app/NewApp）")
    p.add_argument("--yes", action="store_true", help="真的写入")
    p.add_argument("--backup", action="store_true", help="写前整文件备份为 .bak（ROM 模式不适用）")
    p.add_argument("--xattr-from", type=int, default=-1,
                   help="从该条目复制 xattr 块，默认用父目录的")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--sort", action="store_true",
                   help="原目录无序时也按名字重排（默认只在原目录本来有序时插入）")
    g.add_argument("--append", action="store_true",
                   help="只追加到末尾、不重排（调试用；引擎按名字查时会导致新应用「凭空消失」）")

    p = sub.add_parser("replace", parents=[common],
                       help="替换某个条目的内容（条目号或 --path；jar 会做专项检查）")
    p.add_argument("image")
    p.add_argument("idx", type=int, nargs="?", default=None)
    p.add_argument("newfile")
    p.add_argument("--path", default="", help="按路径定位，如 /framework/framework.jar")
    p.add_argument("--yes", action="store_true", help="真的写入")
    p.add_argument("--backup", action="store_true", help="写前整文件备份为 .bak（ROM 模式不适用）")
    p.add_argument("--align", type=int, default=0, help="追加前按该字节数对齐，如 4096")
    p.add_argument("--drop-oat", action="store_true",
                   help="同时把同目录/oat/<abi>/ 下同名 .odex/.vdex/.oat/.art 置零"
                        "（不动 boot.art/boot.oat）")
    p.add_argument("--keep-zip", action="store_true",
                   help="不把 dex 重打成 STORED（默认按原 jar 的形态对齐）")
    p.add_argument("--force", action="store_true", help="新文件不是合法 zip 也照写")

    p = sub.add_parser("verify", parents=[common], help="全量 CRC 校验")
    p.add_argument("image")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("digest", parents=[common],
                       help="算镜像里某个文件的内容指纹，可和本地文件对比")
    p.add_argument("image")
    p.add_argument("--path", required=True, help="镜像内路径，如 /framework/framework.jar")
    p.add_argument("--expect", default="", help="要和它对比的本地文件")

    p = sub.add_parser("check", parents=[common],
                       help="导入前体检：路径、目录项、排序、内容、缓存逐项核对")
    p.add_argument("image")
    p.add_argument("--path", required=True, help="镜像内路径，如 /system/app/YourApp/YourApp.apk")
    p.add_argument("--no-cache", action="store_true", help="跳过 fscache.bin 检查")

    p = sub.add_parser("sortdir", parents=[common],
                       help="把某个目录的子项按名字重排（补救旧版 append 造成的顺序错乱）")
    p.add_argument("image")
    p.add_argument("--path", required=True, help="目录路径，如 /system/app")
    p.add_argument("--yes", action="store_true", help="真的写入")

    p = sub.add_parser("fscache", parents=[common],
                       help="判定 fscache.bin 是完整清单还是部分缓存（决定新增会不会被缓存挡住）")
    p.add_argument("image")
    p.add_argument("--path", default="", help="额外查一条路径在不在缓存里")
    p.add_argument("--limit", type=int, default=10, help="未命中示例最多列几条")

    p = sub.add_parser("ovl", parents=[common],
                       help="查看覆盖层：哪些 inode 被接管、某个目录有没有被接管")
    p.add_argument("image")
    p.add_argument("--dir", default="", help="顺带对比这个目录，如 /system/priv-app")

    p = sub.add_parser("install", parents=[common],
                       help="把 APK 装进只读分区（只写覆盖层，readonly.bin 一个字节都不动）")
    p.add_argument("image")
    p.add_argument("apk", help="要装的 .apk")
    p.add_argument("--dir", default="/system/priv-app",
                   help="装在哪个目录下，默认 /system/priv-app")
    p.add_argument("--name", default="", help="应用目录名，默认取 apk 文件名（去扩展名）")
    p.add_argument("--mode", default="644", help="apk 的权限（八进制），默认 644")
    p.add_argument("--yes", action="store_true", help="真的写入")
    p.add_argument("--force", action="store_true", help="已有同名应用时覆盖")

    p = sub.add_parser("uninstall", parents=[common],
                       help="从覆盖层摘掉一个应用（摘掉后回到只读层的原状）")
    p.add_argument("image")
    p.add_argument("name", help="应用目录名，如 SFM")
    p.add_argument("--dir", default="/system/priv-app")
    p.add_argument("--yes", action="store_true", help="真的写入")

    ap.add_argument("--version", action="version", version="ropatch " + VERSION)
    # 允许 "ropatch.py fs.7z info" 和 "ropatch.py fs.7z --member X replace ..." 两种写法：
    # 把镜像路径和写在子命令前的全局选项，一并挪到子命令之后（它们只在子 parser 里定义）
    CMDS = ("info", "list", "find", "apks", "jars", "path", "tree", "show",
            "extract", "add", "replace", "verify", "digest", "check", "sortdir",
            "fscache", "ovl", "install", "uninstall")
    argv = normalize_argv(sys.argv[1:], CMDS, ("--part", "--member"))
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
    ovl = None
    OVL_CMDS = ("ovl", "install", "uninstall")
    try:
        if is_archive(args.image):
            rom = Rom(args.image, args.part, args.keep, args.no_writeback, args.fresh,
                      getattr(args, "member", ""))
            if args.cmd == "info" and not args.part:
                return rom.show_members()
            member = rom.pick()
            img = rom.image(member)
            img.label = "%s :: %s" % (os.path.basename(args.image), member)
            if args.cmd in OVL_CMDS:
                fsr = rom.overlay_root(member)
                ovl = OvlFS(fsr, rom.part_of(member) or args.part)
        else:
            if args.cmd in OVL_CMDS:
                fsr = locate_androidfs(args.image)
                if not fsr:
                    log("[错误] 这个路径里没有 androidfs_<版本> 目录：%s" % args.image)
                    log("       先解包：python3 titanunpack.py fs.7z <目录>")
                    return 2
                part = (args.part or "system").strip("/")
                rp = os.path.join(fsr, part, "readonly.bin")
                if not os.path.exists(rp):
                    log("[错误] %s 里没有 %s/readonly.bin" % (fsr, part))
                    log("       现有分区：%s" % "、".join(
                        d for d in sorted(os.listdir(fsr))
                        if os.path.isdir(os.path.join(fsr, d))))
                    return 2
                img = Image(rp)
                img.label = fsr
                ovl = OvlFS(fsr, part)
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
        elif args.cmd == "jars":
            rc = cmd_jars(img, args.name.strip(), parse_size(args.min_size, 0),
                          parse_size(args.max_probe, 64 << 20), args.limit,
                          args.part or (rom.part_of(member) if rom else ""))
        elif args.cmd == "show":
            rc = cmd_show(img, args.idx, args.limit, args.xattr_bytes)
        elif args.cmd == "path":
            rc = cmd_path(img, args.path)
        elif args.cmd == "tree":
            rc = cmd_tree(img, args.path, args.depth, args.limit)
        elif args.cmd == "extract":
            if args.idx is None and not args.path:
                log("[错误] 要给条目号，或用 --path 指定路径")
                rc = 2
            else:
                rc = cmd_extract(img, args.idx, args.out, args.path)
        elif args.cmd == "add":
            ops = []
            if args.from_dir:
                base = args.from_dir
                if not os.path.isdir(base):
                    log("[错误] --from 指向的不是目录：%s" % base)
                    rc = 2
                    ops = None
                else:
                    names = sorted(os.listdir(base))
                    for fn in names:
                        full = os.path.join(base, fn)
                        if os.path.isfile(full) and fn.lower().endswith((".apk", ".jar")):
                            ops.append((args.dir, fn, full))
                    if not ops:
                        log("[错误] %s 里没有 .apk/.jar 文件" % base)
                        rc = 2
                        ops = None
                    else:
                        log("来源目录 : %s，%d 个文件" % (base, len(ops)))
            elif args.name and args.file:
                ops.append((args.dir, args.name, args.file))
            if ops is not None:
                if not ops:
                    log("[错误] 用法：add <镜像> <目录> <文件名> <源文件>，或用 --from <目录>")
                    rc = 2
                else:
                    rc = cmd_add_ops(img, ops, args.yes, args.backup and not rom,
                                     args.xattr_from, args.sort, args.mkdir,
                                     args.append)
                    if rom and rc == 0 and args.yes:
                        rom.mark(member)
        elif args.cmd == "replace":
            target, label = args.idx, ""
            if args.path:
                try:
                    target, label = locate_entry(img, args.path)
                except ValueError as e:
                    log("[错误] 找不到目标文件：%s" % e)
                    log("       先用 jars 或 tree 定位：ropatch.py <镜像> jars --part %s --name <名字>"
                        % (args.part or "<分区>"))
                    target = None
                else:
                    log("解析目标 : %s = 条目 %d" % (label, target))
            if target is None:
                rc = 2
            else:
                rc = cmd_replace(img, target, args.newfile, args.yes,
                                 args.backup and not rom, args.align, label,
                                 args.drop_oat, args.keep_zip, args.force)
                if rom and rc == 0 and args.yes:
                    rom.mark(member)
        elif args.cmd == "verify":
            rc = cmd_verify(img, args.limit)
        elif args.cmd == "digest":
            rc = cmd_digest(img, args.path, args.expect)
        elif args.cmd == "check":
            rc = cmd_check(img, args.path, not args.no_cache)
        elif args.cmd == "fscache":
            rc = cmd_fscache(img, args.path, args.limit)
        elif args.cmd == "sortdir":
            rc = cmd_sortdir(img, args.path, args.yes)
            if rom and rc == 0 and args.yes:
                rom.mark(member)
        elif args.cmd == "ovl":
            rc = cmd_ovl(img, ovl, args.dir)
        elif args.cmd == "install":
            rc = cmd_install(img, ovl, args.dir, args.apk, args.name, args.mode,
                             args.yes, args.force)
            if rom and rc == 0 and args.yes:
                for p in ovl.touched:
                    if os.path.exists(p):
                        rom.add_extra(p)
        elif args.cmd == "uninstall":
            if rom:
                log("[错误] uninstall 只能对解包出来的目录用：")
                log("       7z 里删成员要重建整个包，先 titanunpack 解包再改")
                rc = 2
            else:
                rc = cmd_uninstall(img, ovl, args.dir, args.name, args.yes)
    finally:
        if rom:
            if rom.dirty or rom.extra:
                if rom.writeback():
                    log("[失败] 改动没有写进压缩包，只留在缓存：%s" % rom.tmp)
                    log("       解包/导入看到的仍是旧内容；按上面的错误修好后重跑。")
                    rc = 1
            rom.cleanup()
    return rc


if __name__ == "__main__":
    sys.exit(main())

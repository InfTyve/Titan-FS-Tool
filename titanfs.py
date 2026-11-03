#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
titanfs.py —— VPhoneGaga(虚拟大师) "Titan FS" 虚拟机文件系统解析/导出工具

适用对象：App 私有目录下 fs/ 这样的目录（host 侧）：
  fs/
    androidfs.bin          # SFDA 描述符
    config.gz  locales.bin
    fscache.bin            # 全局路径缓存（可选，仅用于加速定位）
    root/  data/  cache/   # 各挂载分区
       superblock.bin      # BPUS 超级块(16B)
       fscache.bin         # ATIT 记录池 = inode 元数据/inode 表
       <bucket>/<inode>            # 文件内容
       <bucket>/<inode>.metadata   # ATEM inode 元数据（旧式）
    root/readonly.bin      # ATIT 索引镜像 = 只读层（/）

用法:
  python3 titanfs.py info   <fs_dir>
  python3 titanfs.py tree   <fs_dir> [--layer all|data|cache|root]
  python3 titanfs.py ls     <fs_dir> <路径>
  python3 titanfs.py extract <fs_dir> <输出目录> [--layer all|...]
"""

import os
import sys
import stat
import struct
import zlib
import argparse
from dataclasses import dataclass, field

# ---------------------------------------------------------------- 基础结构

MAGIC_SFDA = b"SFDA"   # androidfs.bin
MAGIC_BPUS = b"BPUS"   # superblock.bin
MAGIC_ATIT = b"ATIT"   # 镜像/记录池
MAGIC_ATEM = b"ATEM"   # 单 inode 元数据


class TitanError(Exception):
    pass


def u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def u64(b, o):
    return struct.unpack_from("<Q", b, o)[0]


def pad4(n):
    return (n + 3) & ~3


@dataclass
class Node:
    ino: int
    mode: int
    uid: int = 0
    gid: int = 0
    nlink: int = 1
    size: int = 0
    mtime: int = 0
    ctime: int = 0
    atime: int = 0
    xattrs: list = field(default_factory=list)
    children: dict = field(default_factory=dict)   # name -> ino
    data: bytes = None                            # 惰性内容
    origin: str = ""

    @property
    def isdir(self):
        return stat.S_ISDIR(self.mode)

    @property
    def islink(self):
        return stat.S_ISLNK(self.mode)


# ---------------------------------------------------------------- ATEM 元数据

def parse_atem(blob):
    """ATEM inode 元数据（.metadata 文件）"""
    if blob[:4] != MAGIC_ATEM:
        raise TitanError("not ATEM")
    mode = u32(blob, 8)
    n = Node(ino=-1, mode=mode, uid=u32(blob, 12), gid=u32(blob, 16),
             nlink=u32(blob, 20), mtime=u32(blob, 24), ctime=u32(blob, 32),
             size=u32(blob, 40), origin="ATEM")
    xlen, xcnt = u32(blob, 44), u32(blob, 48)
    p = 52
    for _ in range(xcnt):
        e = blob.index(b"\0", p)
        name = blob[p:e].decode("utf-8", "replace")
        p = e + 1
        vl = u32(blob, p)
        p += 4
        val = blob[p:p + vl - 1]
        p += vl
        n.xattrs.append((name, val))
    return n


# ---------------------------------------------------------------- ATIT 记录池

def _walk_entries(blob, q, elen):
    """遍历 ATIT 记录尾部的目录项数组：u64 inode + u32 名字长(含\\0) + 名字

    返回 (条目列表, 结束偏移)；解析不合法时返回 (None, 0)
    """
    ents = []
    used = 0
    end = q + elen
    while used + 12 <= elen:
        if q + 12 > len(blob):
            return None, 0
        cino = u64(blob, q)
        nl = u32(blob, q + 8)
        if nl < 2 or nl > 256 or used + 12 + nl > elen + 3:
            return None, 0
        nm = blob[q + 12:q + 12 + nl - 1]
        if b"\0" in nm or any(c < 0x20 for c in nm):
            return None, 0
        ents.append((nm.decode("utf-8", "replace"), cino))
        q += 12 + nl
        used += 12 + nl
    if used + 3 < elen:          # 剩余不是少量补齐
        return None, 0
    return ents, end


def parse_atit_pool(blob, base=0, limit=None):
    """fscache.bin：变长 ATIT 记录池（inode 表缓存）

    记录头 84 字节：
      0  magic 'ATIT'
      4  u32 记录总长
      8  u32 状态(1)          12 u64 上一条记录偏移
      20 u64 inode            28 u64 文件大小
      36 u32 mode  40 uid  44 gid  48 nlink
      52 u32 mtime 56 mtime2  60 ctime
      64 u32 xattr 字节数      68 u32 目录项字节数  72 u32 保留
      76 u32 atime             80 u32 xattr 个数
    之后：xattr 数组（name\\0 + u32 长度 + 值），再之后：目录项
      （u64 子 inode + u32 名字长度(含\\0) + 名字），整体按 4 字节补齐
    """
    nodes = {}
    pos = 0
    end = len(blob) if limit is None else min(len(blob), limit)
    while pos + 84 <= end:
        if blob[pos:pos + 4] != MAGIC_ATIT:
            pos += 4
            continue
        rlen = u32(blob, pos + 4)
        if rlen < 84 or pos + rlen > end:
            pos += 4
            continue
        ino = u64(blob, pos + 20)
        size = u64(blob, pos + 28)
        mode = u32(blob, pos + 36)
        if mode == 0:            # 池头/池尾超级块，不是 inode 记录
            pos += rlen
            continue
        n = Node(ino=ino, mode=mode, uid=u32(blob, pos + 40),
                 gid=u32(blob, pos + 44), nlink=u32(blob, pos + 48),
                 mtime=u32(blob, pos + 52), ctime=u32(blob, pos + 60),
                 atime=u32(blob, pos + 76), size=size, origin="ATIT")
        xcnt = u32(blob, pos + 80)
        elen = u32(blob, pos + 68)
        xlen = u32(blob, pos + 64)
        p = pos + 84
        for _ in range(xcnt):
            e = blob.index(b"\0", p)
            name = blob[p:e].decode("utf-8", "replace")
            p = e + 1
            vl = u32(blob, p)
            p += 4
            n.xattrs.append((name, blob[p:p + vl - 1]))
            p += vl
        # 目录项：xattr 区从 +80 起、总长 xlen（含 4 字节计数），其后紧接目录项
        if elen:
            cands = [pos + 80 + xlen]
            base = pos + rlen - elen
            cands += [base - k for k in range(4)]
            for q in cands:
                ents, endpos = _walk_entries(blob, q, elen)
                if ents is not None and endpos <= pos + rlen:
                    for nm, cino in ents:
                        n.children[nm] = cino
                    break
        nodes[ino] = n
        pos += rlen
    return nodes


# ---------------------------------------------------------------- ATIT 索引镜像

def parse_atit_image(path):
    """root/readonly.bin：64 字节头 + N*52 字节索引 + 数据/元数据区

    头：0 'ATIT' 4 hdr=0x40 8 条目数 12 已用末尾 16 最后数据偏移
        20 校验/种子 24 条目大小(0x34)
    条目(52B)：mode uid gid nlink mtime mtime2 ctime ctime2
              flag(0x50000000) size off crc32 metalen
    """
    blob = open(path, "rb").read()
    if blob[:4] != MAGIC_ATIT:
        raise TitanError("%s 不是 ATIT 镜像" % path)
    count = u32(blob, 8)
    esz = u32(blob, 24) or 52
    nodes = {}
    for i in range(count):
        o = 0x40 + i * esz
        mode, uid, gid, nlink = struct.unpack_from("<IIII", blob, o)
        mtime, _m2, ctime = struct.unpack_from("<III", blob, o + 16)
        size, off, crc, mlen = struct.unpack_from("<IIII", blob, o + 36)
        n = Node(ino=i, mode=mode, uid=uid, gid=gid, nlink=nlink,
                 mtime=mtime, ctime=ctime, size=size, origin="readonly")
        data = blob[off:off + size]
        n.data = data
        if stat.S_ISDIR(mode):
            q = 0
            while q + 12 <= len(data):
                nl = u32(data, q)
                nm = data[q + 4:q + 4 + nl - 1].decode("utf-8", "replace")
                cino = u64(data, q + 4 + nl)
                n.children[nm] = cino
                q += 4 + nl + 8
        # 尾随 xattr 块
        xb = blob[off + size:off + size + mlen]
        if len(xb) >= 4:
            xc = u32(xb, 0)
            p = 4
            for _ in range(xc):
                e = xb.index(b"\0", p)
                name = xb[p:e].decode("utf-8", "replace")
                p = e + 1
                vl = u32(xb, p)
                p += 4
                n.xattrs.append((name, xb[p:p + vl - 1]))
                p += vl
        nodes[i] = n
    return nodes


# ---------------------------------------------------------------- 分区

class Partition:
    """data/ cache/ root/ 这类由 <bucket>/<inode> 组成的分区"""

    def __init__(self, path, name):
        self.path = path
        self.name = name
        self.nodes = {}
        self._load()

    def _load(self):
        # 1) fscache.bin 里的 ATIT 记录（元数据缓存，最新）
        fc = os.path.join(self.path, "fscache.bin")
        if os.path.exists(fc):
            try:
                self.nodes.update(parse_atit_pool(open(fc, "rb").read()))
            except Exception:
                pass
        # 2) 磁盘上的 <inode>.metadata（ATEM，旧式/落盘）
        for root, dirs, files in os.walk(self.path):
            dirs[:] = [d for d in dirs if d != "lost+found"]
            for f in files:
                if f.endswith(".metadata"):
                    try:
                        ino = int(f.split(".")[0], 16)
                        n = parse_atem(open(os.path.join(root, f), "rb").read())
                        n.ino = ino
                        self.nodes.setdefault(ino, n)
                    except Exception:
                        pass
        # 3) 内容文件 <inode>（十六进制）；目录内容为 "name=inode\\r\\n" 文本
        for root, dirs, files in os.walk(self.path):
            for f in files:
                if "." in f or f in ("fscache.bin", "superblock.bin"):
                    continue
                try:
                    ino = int(f, 16)
                except ValueError:
                    continue
                n = self.nodes.get(ino)
                if n is None:
                    n = Node(ino=ino, mode=stat.S_IFREG | 0o644, origin="file")
                    self.nodes[ino] = n
                try:
                    n.data = open(os.path.join(root, f), "rb").read()
                except OSError:
                    continue
                if n.isdir and b"=" in n.data:
                    for line in n.data.splitlines():
                        if b"=" in line:
                            nm, _, iv = line.partition(b"=")
                            try:
                                n.children[nm.decode("utf-8", "replace")] = int(iv)
                            except ValueError:
                                pass

    def root(self):
        return self.nodes.get(0)


# ---------------------------------------------------------------- 容器

class TitanFS:
    def __init__(self, fsdir):
        self.dir = fsdir
        self.parts = {}
        self.readonly = {}
        for name in ("root", "data", "cache"):
            p = os.path.join(fsdir, name)
            if os.path.isdir(p):
                self.parts[name] = Partition(p, name)
        ro = os.path.join(fsdir, "root", "readonly.bin")
        if os.path.exists(ro):
            try:
                self.readonly = parse_atit_image(ro)
            except Exception:
                self.readonly = {}

    # 统一视图：layer -> nodes
    def layers(self):
        out = {}
        if self.readonly:
            out["root(readonly)"] = self.readonly
        for k, p in self.parts.items():
            out[k] = p.nodes
        return out

    def lookup_all(self, ino):
        """在所有层里找 inode（同号不同层）"""
        res = []
        for k, nodes in self.layers().items():
            if ino in nodes:
                res.append((k, nodes[ino]))
        return res


def fmt_mode(m):
    return stat.filemode(m)


def dump_tree(nodes, ino=0, prefix="", out=None, seen=None):
    n = nodes.get(ino)
    if n is None:
        return
    if seen is None:
        seen = set()
    for name, cino in sorted(n.children.items()):
        c = nodes.get(cino)
        if c is None:
            out.append(prefix + name + "  [缺失 inode %d]" % cino)
            continue
        if c.isdir:
            out.append("%s%s/  ino=%d mode=%s uid=%d gid=%d" %
                       (prefix, name, cino, fmt_mode(c.mode), c.uid, c.gid))
            if cino not in seen:
                seen.add(cino)
                dump_tree(nodes, cino, prefix + "    ", out, seen)
        else:
            out.append("%s%-40s ino=%-6d %8d  %s" %
                       (prefix, name, cino, c.size, fmt_mode(c.mode)))


def export(nodes, ino, dest, seen=None):
    n = nodes.get(ino)
    if n is None or not n.isdir:
        return
    if seen is None:
        seen = set()
    os.makedirs(dest, exist_ok=True)
    for name, cino in sorted(n.children.items()):
        c = nodes.get(cino)
        if c is None:
            continue
        safe = name.replace("/", "_")
        target = os.path.join(dest, safe)
        if c.isdir:
            if cino in seen:
                continue
            seen.add(cino)
            export(nodes, cino, target, seen)
            os.chmod(target, c.mode & 0o7777)
        else:
            data = c.data if c.data is not None else b""
            with open(target, "wb") as fh:
                fh.write(data[:c.size] if c.size else data)
            os.chmod(target, c.mode & 0o7777)


def cmd_info(fsdir):
    print("== 容器目录: %s" % fsdir)
    afs = os.path.join(fsdir, "androidfs.bin")
    if os.path.exists(afs):
        b = open(afs, "rb").read()
        print("androidfs.bin : magic=%s ver=%d field=%d" %
              (b[:4].decode(), u32(b, 4), u32(b, 8)))
    for name in ("root", "data", "cache"):
        sb = os.path.join(fsdir, name, "superblock.bin")
        if os.path.exists(sb):
            b = open(sb, "rb").read()
            print("%-5s superblock: magic=%s hdr=%d gen=%d" %
                  (name, b[:4].decode(), u32(b, 4), u32(b, 8)))
    for k, nodes in TitanFS(fsdir).layers().items():
        nd = sum(1 for n in nodes.values() if n.isdir)
        nf = len(nodes) - nd
        print("层 %-16s inode=%-7d 目录=%-5d 文件=%-6d" % (k, len(nodes), nd, nf))


def cmd_tree(fsdir, layer):
    fs = TitanFS(fsdir)
    for k, nodes in fs.layers().items():
        if layer != "all" and layer not in k:
            continue
        root = nodes.get(0)
        print("\n=== 层: %s  (根 inode=0, mode=%s, 子项 %d)" %
              (k, fmt_mode(root.mode) if root else "?", len(root.children) if root else 0))
        out = []
        dump_tree(nodes, 0, "", out)
        print("\n".join(out) if out else "  (空)")


def cmd_ls(fsdir, path):
    path = path.strip("/")
    fs = TitanFS(fsdir)
    for k, nodes in fs.layers().items():
        cur = 0
        ok = True
        for comp in [c for c in path.split("/") if c]:
            n = nodes.get(cur)
            if n is None or comp not in n.children:
                ok = False
                break
            cur = n.children[comp]
        if not ok:
            continue
        n = nodes[cur]
        print("=== %s : /%s (ino=%d %s)" % (k, path, cur, fmt_mode(n.mode)))
        if n.isdir:
            for name, cino in sorted(n.children.items()):
                c = nodes.get(cino)
                print("  %-38s ino=%-6d %s %8s" %
                      (name, cino, fmt_mode(c.mode) if c else "??????????",
                       c.size if c else "?"))
        else:
            print("  内容: %r" % ((n.data or b"")[:200]))


def cmd_extract(fsdir, dest, layer):
    fs = TitanFS(fsdir)
    for k, nodes in fs.layers().items():
        if layer != "all" and layer not in k:
            continue
        d = os.path.join(dest, k.replace("/", "_").replace("(", "_").replace(")", ""))
        export(nodes, 0, d)
        print("已导出层 %-16s -> %s" % (k, d))


def main():
    ap = argparse.ArgumentParser(description="VPhoneGaga Titan FS 解析/导出")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("info");  p.add_argument("fs")
    p = sub.add_parser("tree");  p.add_argument("fs"); p.add_argument("--layer", default="all")
    p = sub.add_parser("ls");    p.add_argument("fs"); p.add_argument("path")
    p = sub.add_parser("extract"); p.add_argument("fs"); p.add_argument("dest")
    p.add_argument("--layer", default="all")
    a = ap.parse_args()
    if a.cmd == "info":
        cmd_info(a.fs)
    elif a.cmd == "tree":
        cmd_tree(a.fs, a.layer)
    elif a.cmd == "ls":
        cmd_ls(a.fs, a.path)
    else:
        cmd_extract(a.fs, a.dest, a.layer)


if __name__ == "__main__":
    main()

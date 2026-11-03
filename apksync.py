#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apksync.py —— 批量把一文件夹的 APK 同步进 ROM 只读分区（按包名自动匹配）

ropatch.py 一次只换一个条目，十几个 APK 就要查十几次条目号。本工具把这件事
压成一条命令：给一个目录，自动读每个 APK 的包名，自动在分区里找到同包名条目，
全部替换，最后只写回一次压缩包。

用法
----
  python3 apksync.py fs.7z ./apks                  # 演练：只匹配，不写盘
  python3 apksync.py fs.7z ./apks --yes            # 真替换
  python3 apksync.py fs.7z ./apks --part system --yes
  python3 apksync.py system/readonly.bin ./apks --yes   # 直接对解出的镜像操作

默认扫描包内**所有**只读层（system / vendor / product…），按包名全局匹配；
`--part system` 可只扫一个分区，省掉其它分区的解压时间。

匹配规则
--------
  * 包名完全相同才算命中（区分大小写以外全部按 AXML 里的 package 属性）
  * 同一包名在多个位置（/app 与 /priv-app、system 与 product）→ 默认全部替换
    加 --first 只换第一个
  * 目录里有 base.apk 的 split 包 → 只替换 base.apk，其余成员跳过
  * 同一个包名在输入目录里出现多次 → 只保留 versionCode 最大的那个
  * 新 APK 与镜像内条目 CRC 相同 → 跳过，不重复写

索引缓存
--------
第一次要遍历目录树 + 读上千个 AndroidManifest.xml（1.2 GB 的层约几分钟），
结果缓存在 /tmp/apksync_index/<hash>.json，同一条命令再跑就是秒级。
镜像或分区变了（size/mtime 变）自动重建；--fresh 强制重建。
"""

VERSION = "2026.11.08"

import os
import io
import re
import sys
import json
import time
import zlib
import struct
import zipfile
import tempfile
import argparse
import hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import ropatch as R


# --------------------------------------------------------------------------
# 二进制 AndroidManifest.xml（AXML）解析
# --------------------------------------------------------------------------

def _strpool(data, off):
    """解析 RES_STRING_POOL_TYPE，返回字符串列表"""
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
    """从二进制 manifest 里取 package / versionCode / versionName。

    只关心根节点 <manifest> 的属性，不建整棵树，几十 KB 的 manifest 毫秒级。
    """
    if not data or len(data) < 16:
        return None
    typ, hsize, total = struct.unpack_from("<HHI", data, 0)
    if typ != 0x0003:
        return None
    end = min(total, len(data))
    strings = []

    # 第一遍：找字符串池（通常紧跟文件头）
    off = hsize
    while off + 8 <= end:
        ctype, chsize, csize = struct.unpack_from("<HHI", data, off)
        if csize < 8:
            break
        if ctype == 0x0001:
            strings = _strpool(data, off)
            break
        off += csize

    # 第二遍：找 <manifest> 起始标签
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


def apk_meta_from_bytes(manifest):
    return parse_axml(manifest) if manifest else None


def read_apk_file(path):
    """读本地 APK：包名、版本、大小、是否 sanity"""
    size = os.path.getsize(path)
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if "AndroidManifest.xml" not in names:
                return {"ok": False, "err": "没有 AndroidManifest.xml（不是 APK）", "size": size}
            meta = parse_axml(z.read("AndroidManifest.xml"))
    except Exception as exc:
        return {"ok": False, "err": "打不开：%s" % exc, "size": size}
    if not meta or not meta.get("pkg"):
        return {"ok": False, "err": "manifest 里读不出包名", "size": size}
    return {"ok": True, "pkg": meta["pkg"], "vc": meta.get("vc", -1),
            "vn": meta.get("vn", ""), "size": size, "path": path,
            "name": os.path.basename(path)}


def read_manifest_from_image(img, entry):
    """从镜像条目里读 AndroidManifest.xml（只解这一个成员）"""
    try:
        with open(img.path, "rb") as f:
            with zipfile.ZipFile(R.SubFile(f, entry["off"], entry["size"])) as z:
                names = z.namelist()
                if "AndroidManifest.xml" not in names:
                    return None
                return z.read("AndroidManifest.xml")
    except Exception:
        return None


# --------------------------------------------------------------------------
# 分区索引
# --------------------------------------------------------------------------

CACHE_DIR = os.path.join(tempfile.gettempdir(), "apksync_index")


def cache_path(img, part, min_size=0):
    try:
        key = "%s|%d|%d|%d|%s" % (os.path.abspath(img.path), os.path.getsize(img.path),
                                  int(os.path.getmtime(img.path)), min_size, VERSION)
    except OSError:
        return ""
    h = hashlib.md5(key.encode("utf-8")).hexdigest()[:16]
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, "%s-%s.json" % (part or "all", h))


def index_partition(img, part, jobs=8, fresh=False, min_size=4096):
    """遍历分区，返回 [{path, idx, pkg, vc, vn, size, split}]"""
    cp = cache_path(img, part, min_size)
    if cp and os.path.exists(cp) and not fresh:
        try:
            with open(cp, "r", encoding="utf-8") as f:
                d = json.load(f)
            if d.get("count") == img.count and d.get("version") == VERSION:
                R.log("  命中索引缓存：%s（%d 个 APK）" % (cp, len(d["items"])))
                return d["items"]
        except Exception:
            pass

    t0 = time.time()
    cand = []
    n = 0
    tiny = 0
    for p, i, e in R.walk_paths(img):
        n += 1
        if n % 20000 == 0:
            R.log("    已遍历 %d 个条目…" % n)
        if not p.lower().endswith(".apk"):
            continue
        if e["size"] < min_size:
            tiny += 1
            continue
        cand.append((p, i, e))
    R.log("  非目录条目 %d 个，其中 .apk %d 个%s（遍历 %.1fs），开始读 manifest…"
          % (n, len(cand),
             "（另有 %d 个小于 %s 已跳过）" % (tiny, R.human(min_size)) if tiny else "",
             time.time() - t0))

    items = []
    done = 0
    t1 = time.time()

    def work(t):
        p, i, e = t
        man = read_manifest_from_image(img, e)
        meta = apk_meta_from_bytes(man)
        return p, i, e, meta

    if cand:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
            for p, i, e, meta in ex.map(work, cand):
                done += 1
                if done % 200 == 0:
                    R.log("    manifest %d/%d…" % (done, len(cand)))
                if not meta or not meta.get("pkg"):
                    continue
                items.append({"path": p, "idx": i, "pkg": meta["pkg"],
                              "vc": meta.get("vc", -1), "vn": meta.get("vn", ""),
                              "size": e["size"], "split": False})
    R.log("  解析完成：可用 %d 个（%.1fs）" % (len(items), time.time() - t1))

    # 标记 split 成员：同目录有 base.apk 时，非 base 的都是分包
    bydir = {}
    for it in items:
        bydir.setdefault(os.path.dirname(it["path"]), []).append(it)
    for _d, lst in bydir.items():
        has_base = any(os.path.basename(x["path"]).lower() == "base.apk" for x in lst)
        if has_base:
            for x in lst:
                if os.path.basename(x["path"]).lower() != "base.apk":
                    x["split"] = True

    if cp:
        try:
            with open(cp, "w", encoding="utf-8") as f:
                json.dump({"version": VERSION, "count": img.count,
                           "part": part, "items": items}, f)
            R.log("  索引已缓存：%s" % cp)
        except OSError:
            pass
    return items


# --------------------------------------------------------------------------
# 输入目录扫描
# --------------------------------------------------------------------------

def scan_input_dir(root, recursive=True, only="", exclude=""):
    found = []
    if recursive:
        walker = os.walk(root)
    else:
        walker = [(root, [], sorted(os.listdir(root)))]
    for cur, _dirs, files in walker:
        for fn in sorted(files):
            if not fn.lower().endswith(".apk"):
                continue
            p = os.path.join(cur, fn)
            if only and only.lower() not in p.lower():
                continue
            if exclude and exclude.lower() in p.lower():
                continue
            found.append(p)
    return sorted(found)


def build_apk_table(paths):
    """读所有输入 APK，返回 (有效列表, 失败列表)"""
    ok, bad = [], []
    for p in paths:
        m = read_apk_file(p)
        if not m.get("ok"):
            bad.append({"path": p, "name": os.path.basename(p), "err": m.get("err", "?")})
        else:
            ok.append(m)
    # 同包名多份只留 versionCode 最大的
    best = {}
    dup = []
    for m in ok:
        cur = best.get(m["pkg"])
        if cur is None:
            best[m["pkg"]] = m
        else:
            keep, drop = (m, cur) if (m["vc"] or 0) > (cur["vc"] or 0) else (cur, m)
            best[m["pkg"]] = keep
            dup.append({"keep": keep, "drop": drop})
    return list(best.values()), bad, dup


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def human(n):
    return R.human(n)


def dw(s):
    """显示宽度：中日韩字符算 2 格，用于对齐表格"""
    w = 0
    for ch in str(s):
        w += 2 if ord(ch) > 0x2E7F and not (0xFF61 <= ord(ch) <= 0xFF9F) else 1
    return w


def pad(s, n):
    s = str(s)
    w = dw(s)
    if w >= n:
        return s
    return s + " " * (n - w)


def cut(s, n):
    """按显示宽度截断，避免表格错位"""
    s = str(s)
    out, w = "", 0
    for ch in s:
        cw = 2 if (ord(ch) > 0x2E7F and not (0xFF61 <= ord(ch) <= 0xFF9F)) else 1
        if w + cw > n:
            return out
        out += ch
        w += cw
    return out


def fmt_ver(m):
    vc = m.get("vc", -1)
    vn = m.get("vn", "")
    if vc and vc > 0:
        return "%s(%d)" % (vn, vc) if vn else "vc%d" % vc
    return vn or "?"


def main():
    ap = argparse.ArgumentParser(
        description="批量把一文件夹的 APK 按包名自动替换进 ROM 只读分区",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join([
            "示例:",
            "  python3 apksync.py fs.7z ./apks                    # 演练，只匹配",
            "  python3 apksync.py fs.7z ./apks --yes              # 全部替换",
            "  python3 apksync.py fs.7z ./apks --part system --yes",
            "  python3 apksync.py fs.7z ./apks --first --yes      # 同包名多处只换第一个",
            "  python3 apksync.py system/readonly.bin ./apks --yes",
            "",
            "匹配靠 APK 内 AndroidManifest.xml 的包名，不靠文件名，",
            "所以把 Settings.apk 改名成 a.apk 也能正确命中。"]))
    ap.add_argument("image", help="fs.7z / fs.7z.001 / 解出来的 readonly.bin")
    ap.add_argument("apks", help="APK 所在目录")
    ap.add_argument("--part", default="", help="只处理该分区，如 system（默认全部只读层）")
    ap.add_argument("--yes", action="store_true", help="真的写入（默认只演练）")
    ap.add_argument("--first", action="store_true", help="同包名多处时只替换第一个")
    ap.add_argument("--jobs", type=int, default=8, help="读 manifest 的线程数，默认 8")
    ap.add_argument("--fresh", action="store_true", help="丢弃索引缓存，重新扫描")
    ap.add_argument("--keep", action="store_true", help="保留临时解出的 readonly.bin")
    ap.add_argument("--no-writeback", action="store_true", help="只改临时副本，不写回 7z")
    ap.add_argument("--no-recursive", action="store_true", help="只扫目录第一层")
    ap.add_argument("--only", default="", help="只处理路径含该串的 APK")
    ap.add_argument("--exclude", default="", help="跳过路径含该串的 APK")
    ap.add_argument("--min-entry", default="1K", help="镜像里小于该体积的 .apk 忽略，默认 1K")
    ap.add_argument("--report", default="", help="报告写到该 TSV，默认 apksync-report-<时间>.tsv")
    ap.add_argument("--verify", action="store_true", help="写回后做全量 CRC 校验")
    ap.add_argument("--version", action="version", version="apksync " + VERSION)
    args = ap.parse_args()

    if not os.path.isdir(args.apks):
        R.log("[错误] APK 目录不存在：%s" % args.apks)
        return 2

    # ---------- 1. 读输入 APK ----------
    R.log("[1/4] 扫描输入目录 %s" % os.path.abspath(args.apks))
    paths = scan_input_dir(args.apks, not args.no_recursive, args.only.strip(),
                           args.exclude.strip())
    if not paths:
        R.log("[错误] 目录里没有 .apk（--only/--exclude 过滤后为空？）")
        return 2
    apks, bad, dup = build_apk_table(paths)
    R.log("  发现 %d 个 APK，可识别 %d 个" % (len(paths), len(apks)))
    for m in apks:
        R.log("    %s%s%s%s" % (pad(cut(m["name"], 27), 28), pad(cut(m["pkg"], 33), 34),
                                pad(fmt_ver(m), 16), human(m["size"])))
    if bad:
        R.log("  [跳过] %d 个读不出包名：" % len(bad))
        for b in bad[:10]:
            R.log("    %s%s" % (pad(cut(b["name"], 27), 28), b["err"]))
    if dup:
        R.log("  [去重] %d 组同包名，保留 versionCode 大的：" % len(dup))
        for d in dup[:10]:
            R.log("    %s 丢弃 %s（%s）" % (d["keep"]["pkg"], d["drop"]["name"], fmt_ver(d["drop"])))
    if not apks:
        R.log("[错误] 没有可用的 APK")
        return 2

    # ---------- 2. 打开镜像 ----------
    rom = None
    targets = []
    try:
        if R.is_archive(args.image):
            rom = R.Rom(args.image, args.part, args.keep, args.no_writeback, args.fresh)
            R.log("")
            R.log("[2/4] 打开包 %s" % os.path.basename(args.image))
            R.log("  只读层 %d 个：%s" % (
                len(rom.members),
                "、".join(rom.part_of(m["path"]) for m in rom.members)))
            for m in rom.members:
                targets.append({"member": m["path"],
                                "part": rom.part_of(m["path"]),
                                "img": None, "items": []})
        else:
            R.log("")
            R.log("[2/4] 打开镜像 %s" % args.image)
            targets.append({"member": None, "part": args.part or "",
                            "img": R.Image(args.image), "items": []})
    except (OSError, ValueError) as exc:
        R.log("[错误] %s" % exc)
        return 2

    # ---------- 3. 建索引 ----------
    R.log("")
    R.log("[3/4] 索引分区里的 APK（首次较慢，之后走缓存）")
    for t in targets:
        if t["img"] is None:
            try:
                t["img"] = rom.image(t["member"])
            except ValueError as exc:
                R.log("  [跳过] %s：%s" % (t["part"], exc))
                t["img"] = False
                continue
        if not t["img"]:
            continue
        R.log("  分区 %s（%s，条目 %d）" % (
            t["part"] or "-", human(t["img"].size), t["img"].count))
        t["items"] = index_partition(t["img"], t["part"], args.jobs, args.fresh,
                                     R.parse_size(args.min_entry, 1024))

    # ---------- 4. 匹配 ----------
    R.log("")
    R.log("[4/4] 按包名匹配")
    by_pkg = {}
    for t in targets:
        for it in t["items"]:
            it["part"] = t["part"]
            it["_t"] = t
            it["img"] = t["img"]
            by_pkg.setdefault(it["pkg"], []).append(it)

    rows = []
    plan = []
    for m in apks:
        hits = by_pkg.get(m["pkg"], [])
        if not hits:
            rows.append(("未命中", m["name"], m["pkg"], "-", "-", "镜像里没有这个包"))
            continue
        bases = [h for h in hits if not h["split"]]
        use = bases or hits
        if args.first:
            use = use[:1]
        for h in use:
            same = False
            try:
                e0 = h["img"].entry(h["idx"])
                if e0["size"] == m["size"]:
                    crc = 0
                    with open(m["path"], "rb") as f:
                        while True:
                            b = f.read(8 << 20)
                            if not b:
                                break
                            crc = zlib.crc32(b, crc)
                    same = (crc & 0xFFFFFFFF) == e0["crc"]
            except OSError:
                pass
            where = (h["part"] + ":" + h["path"]) if h["part"] else h["path"]
            if same:
                rows.append(("相同", m["name"], m["pkg"], where, fmt_ver(m),
                             "内容与镜像一致，跳过"))
                continue
            old_vc = h.get("vc", -1)
            vc = m.get("vc", -1)
            tag = ""
            if isinstance(old_vc, int) and isinstance(vc, int) and old_vc > 0 and vc > 0:
                tag = "↑" if vc > old_vc else ("↓" if vc < old_vc else "=")
            rows.append(("待替换", m["name"], m["pkg"], where,
                         "%s %s %s" % (fmt_ver(h), tag, fmt_ver(m)), ""))
            plan.append({"apk": m, "hit": h})
        if not bases and hits:
            rows.append(("提示", m["name"], m["pkg"], "-", fmt_ver(m),
                         "只有 split 分包，没有 base.apk，未替换"))
        elif bases and len(hits) > len(bases):
            bpart = bases[0]["part"]
            bdir = os.path.dirname(bases[0]["path"])
            rows.append(("提示", m["name"], m["pkg"],
                         (bpart + ":" + bdir) if bpart else bdir,
                         fmt_ver(m),
                         "该包有 %d 个 split 分包，只换了 base.apk，分包仍是旧的"
                         % (len(hits) - len(bases))))

    # ---------- 打印匹配表 ----------
    R.log("")
    order = {"待替换": 0, "相同": 1, "提示": 2, "未命中": 3}
    rows.sort(key=lambda r: order.get(r[0], 9))
    R.log("%s%s%s%s%s" % (pad("状态", 8), pad("输入 APK", 26),
                          pad("包名", 34), pad("ROM 位置", 42), "版本"))
    for st, name, pkg, where, ver, note in rows:
        R.log("%s%s%s%s%s%s" % (pad(st, 8), pad(cut(name, 25), 26),
                                pad(cut(pkg, 33), 34), pad(cut(where, 41), 42), ver,
                                ("  " + note) if note else ""))
    want = len(plan)
    miss = sum(1 for r in rows if r[0] == "未命中")
    same_n = sum(1 for r in rows if r[0] == "相同")
    R.log("")
    R.log("汇总：待替换 %d 处，内容相同 %d 处，未命中 %d 个包%s"
          % (want, same_n, miss, "，读不出包名 %d 个" % len(bad) if bad else ""))

    if not want:
        R.log("[结束] 没有可执行的替换")
        return 0 if same_n else 1

    if not args.yes:
        R.log("")
        R.log("[演练] 未写入。确认无误后加 --yes 执行；写入后只需一次 7z 重建。")
        return 0

    # ---------- 5. 写入 ----------
    R.log("")
    R.log("[执行] 替换 %d 个条目…" % want)
    done = 0
    fail = []
    for item in plan:
        m, h = item["apk"], item["hit"]
        img, e = h["img"], h["img"].entry(h["idx"])
        try:
            if R.isdir(e["mode"]):
                raise ValueError("条目是目录")
            if R.kind(e["mode"]) == "link":
                raise ValueError("条目是符号链接，跳过")
            with open(m["path"], "rb") as f:
                data = f.read()
            R.log("  → %s  %s  %s → %s" % (m["name"], m["pkg"], h["path"], human(len(data))))
            R.write_entry(img, e, data, 0)
            done += 1
            h["_t"]["dirty"] = True
            m["result"] = "ok"
        except Exception as exc:
            fail.append((m["name"], h["path"], str(exc)))
            R.log("  [失败] %s -> %s：%s" % (m["name"], h["path"], exc))
            m["result"] = "fail"

    for t in targets:
        if t.get("dirty") and t["img"]:
            t["img"].refresh()
            if rom and t["member"]:
                rom.mark(t["member"])

    if args.verify and done:
        R.log("")
        R.log("[全量校验] 改动后的只读层…")
        for t in targets:
            if t.get("dirty") and t["img"]:
                R.cmd_verify(t["img"], 20)

    # ---------- 6. 写回 & 自检 ----------
    rc = 0
    if rom:
        R.log("")
        R.log("[写回] 更新压缩包（1.2 GB 级别约几分钟）…")
        rc = rom.writeback()
        if rc:
            R.log("[错误] 写回失败，改动留在临时目录：%s" % rom.tmp)

    # 抽样回读：验证 3 个条目（第一个、最后一个、最大的一个）
    if done:
        picks = []
        valid = [p for p in plan if p["apk"].get("result") == "ok"]
        seen = set()
        for p in valid:                        # 每个分区至少抽一个
            key = (id(p["hit"]["img"]), p["hit"]["idx"])
            if key in seen:
                continue
            if not any(id(x["hit"]["img"]) == id(p["hit"]["img"]) for x in picks):
                seen.add(key)
                picks.append(p)
        if valid:
            for p in (valid[0], max(valid, key=lambda x: x["apk"]["size"])):
                key = (id(p["hit"]["img"]), p["hit"]["idx"])
                if key in seen:
                    continue
                seen.add(key)
                picks.append(p)
        R.log("")
        R.log("[自检] 抽样回读 %d 个条目…" % len(picks))
        for p in picks:
            img, h = p["hit"]["img"], p["hit"]
            try:
                img.refresh()
                e = img.entry(h["idx"])
                back = img.read(e)
                okc = (zlib.crc32(back) & 0xFFFFFFFF) == e["crc"] and len(back) == e["size"]
                R.log("  %s  %s  %s" % ("✓" if okc else "✗", h["path"],
                                        "%s crc %08x" % (human(e["size"]), e["crc"])))
                if not okc:
                    rc = 3
            except Exception as exc:
                R.log("  ✗ %s：%s" % (h["path"], exc))
                rc = 3

    # ---------- 7. 报告 ----------
    rep = args.report or ("apksync-report-%s.tsv" % time.strftime("%Y%m%d-%H%M%S"))
    try:
        with open(rep, "w", encoding="utf-8") as f:
            f.write("状态\t输入APK\t包名\tROM位置\t版本\t备注\n")
            for st, name, pkg, where, ver, note in rows:
                f.write("%s\t%s\t%s\t%s\t%s\t%s\n" % (st, name, pkg, where, ver, note))
            for name, where, err in fail:
                f.write("失败\t%s\t\t%s\t\t%s\n" % (name, where, err))
        R.log("")
        R.log("报告：%s" % os.path.abspath(rep))
    except OSError as exc:
        R.log("[提示] 报告写不了：%s" % exc)

    R.log("")
    if rc == 0:
        R.log("✓ 完成：替换 %d 个，失败 %d 个" % (done, len(fail)))
        if rom:
            R.log("  导入前可复验：python3 ropatch.py %s verify --part %s"
                  % (os.path.basename(args.image), args.part or rom.part_of(rom.members[0]["path"])))
        else:
            R.log("  导入前可复验：python3 ropatch.py %s verify" % os.path.basename(args.image))
    else:
        R.log("✗ 有 %d 个失败，见上面的 [失败] 行和报告" % len(fail))

    if rom:
        rom.cleanup()
    return rc


if __name__ == "__main__":
    sys.exit(main())

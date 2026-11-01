#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apkcheck.py —— 核对打好的 fs 包里某个 APK 到底是不是你改的那一份

用途
----
改完系统 APK、打好包、导入虚拟机却发现“界面一点没变”时，先用它判定是哪一类问题：

  A) 包内那份 APK 的 sha256 == 你本地改好的文件   -> 文件进包了，问题在系统侧
     （预编译 odex、PackageManager 缓存、ROM 覆盖等）
  B) 包内仍是旧哈希                                -> 改动根本没进包
     （最常见：改的是 out/by-partition 里的导出文件，而打包用的是 out/raw）

用法
----
  python3 apkcheck.py fs.7z --apk Settings.apk
  python3 apkcheck.py fs.7z --apk system/priv-app/Settings/Settings.apk --local 改好的.apk
  python3 apkcheck.py out/raw --apk Settings.apk --local 改好的.apk     # 还没打包时直接查镜像

输出里会给出该 APK 在镜像中的真实文件路径（<分区>/<桶>/<inode>），
以及同一目录下是否存在 oat/odex 预编译产物。
"""

import argparse
import hashlib
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import titanunpack as U

VERSION = "2026.11.01"
ZERO = "0" * 64


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def locate_fsroot(src, workdir):
    """返回 (fsroot, 是否需要清理)"""
    if os.path.isdir(src):
        roots = U.find_fs_roots(src)
        if not roots:
            return None
        return roots[0]
    raw = os.path.join(workdir, "raw")
    if not U.unpack(src, raw):
        return None
    roots = U.find_fs_roots(raw)
    return roots[0] if roots else None


def real_file_of(part_path, node):
    """把 Node 反查到镜像里的实体文件路径（用于手工替换/回灌）"""
    src = node.src
    if isinstance(src, tuple) and isinstance(src[0], str):
        return src[0]
    # 退而求其次：按 inode 编号猜命名
    for root, _dirs, files in os.walk(part_path):
        name = "%08x" % node.ino
        if name in files:
            return os.path.join(root, name)
    return ""


def main():
    ap = argparse.ArgumentParser(
        description="核对 fs 包里的 APK 是否为你修改的那份",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python3 apkcheck.py fs.7z --apk Settings.apk\n"
               "  python3 apkcheck.py fs.7z --apk system/priv-app/Settings/Settings.apk --local new.apk")
    ap.add_argument("src", help="打好的包（7z/7z.001/zip/tar）或已解包的 fs 目录")
    ap.add_argument("--apk", required=True,
                    help="镜像内路径；只写文件名则全盘搜索所有同名 APK")
    ap.add_argument("--local", default="", help="你改好的 APK 本地路径，用于哈希比对")
    ap.add_argument("--keep", action="store_true", help="保留下载/解压出来的临时目录")
    ap.add_argument("--version", action="version", version="apkcheck " + VERSION)
    args = ap.parse_args()

    src = os.path.abspath(args.src)
    if not os.path.exists(src):
        print("[错误] 输入不存在: %s" % src)
        return 2

    local_hash = ""
    local_size = 0
    if args.local:
        lp = os.path.abspath(args.local)
        if not os.path.isfile(lp):
            print("[错误] --local 指向的文件不存在: %s" % lp)
            return 2
        print("计算本地文件哈希…")
        local_hash = sha256_file(lp)
        local_size = os.path.getsize(lp)

    work = tempfile.mkdtemp(prefix="apkcheck_")
    fsroot = None
    try:
        print("=== APK 核对  v%s ===" % VERSION)
        print("输入: %s" % src)
        fsroot = locate_fsroot(src, work)
        if not fsroot:
            print("[错误] 没找到 Titan FS 根（androidfs.bin / superblock.bin）")
            return 1
        print("镜像根: %s" % fsroot)

        parts = U.discover_partitions(fsroot)
        if not parts:
            print("[错误] 没发现任何分区")
            return 1
        print("分区: %s\n" % ", ".join(n for n, _ in parts))

        want = args.apk.strip("/").replace("\\", "/")
        by_name_only = "/" not in want
        target_base = want.split("/")[-1].lower()

        hits = []
        for name, ppath in parts:
            ro = os.path.exists(os.path.join(ppath, "readonly.bin"))
            part = U.Partition(name, ppath).load()
            paths = U.resolve(part.nodes, 0)
            for vpath, node in sorted(paths.items()):
                if node.isdir or node.islink:
                    continue
                if by_name_only:
                    if os.path.basename(vpath).lower() != target_base:
                        continue
                else:
                    if vpath.strip("/").lower() != want.lower():
                        continue
                hits.append((name, vpath, node, ro, ppath, paths))

        if not hits:
            print("[结果] 镜像里没有这个路径：%s" % args.apk)
            print("       用 --apk 只给文件名（如 Settings.apk）可以全盘搜索同名文件。")
            return 1

        print("[命中] 共 %d 处\n" % len(hits))
        for i, (name, vpath, node, ro, ppath, paths) in enumerate(hits, 1):
            data = node.read()
            h = sha256_bytes(data)
            rel = real_file_of(ppath, node)
            shown = vpath if name in ("root", "system_root") else "/" + name + vpath
            origin = {"ATEM": "元数据(.metadata)", "ATIT": "记录池",
                      "readonly": "只读层(readonly.bin)",
                      "content": "普通桶文件"}.get(node.origin, node.origin or "未知")
            print("  [%d] 分区 %s  %s" % (i, name, shown))
            print("      inode=%#x  大小 %s  来源 %s%s" % (
                node.ino, U.human(len(data)), origin,
                "  (只读层 readonly.bin)" if ro else ""))
            if rel:
                print("      镜像文件: %s" % rel)
            print("      sha256 = %s" % h)

            if local_hash:
                if h == local_hash:
                    print("      比对: ✓ 与本地文件一致")
                else:
                    print("      比对: ✗ 与本地文件不一致（这里还是旧版本）")

            # 预编译产物：同目录下的 oat/odex/vdex
            parent = os.path.dirname(vpath) or "/"
            odes = [p for p in paths
                    if p.startswith(parent + "/") and os.path.basename(p) in
                    ("oat",) or (p.startswith(parent + "/") and p.endswith((".odex", ".vdex", ".oat")))]
            if odes:
                print("      伴随预编译: %s" % ", ".join(sorted(set(odes))[:4]))
            print()

        # ---- 结论
        if local_hash:
            same = [i for i, (_, _, n, _, _, _) in enumerate(hits, 1)
                    if sha256_bytes(n.read()) == local_hash]
            print("[结论]")
            if len(same) == len(hits):
                print("  包里 %d 处全部是你改的那份 —— 文件进包没问题。" % len(hits))
                print("  那问题在系统侧，按这个顺序查：")
                print("    1) 同目录若有 oat/ 或 *.odex，删掉它们（ART 会优先用预编译代码）")
                print("    2) 清 /data/system/packages.xml 之外的缓存不可靠，直接换新实例验证")
                print("    3) 确认 App 导入的是这个包对应的实例，不是另一个 vm 目录")
            elif same:
                print("  只有部分位置是新的：%s" % ", ".join(map(str, same)))
                print("  其余位置仍是旧 APK，系统很可能加载的是旧的那份。")
            else:
                print("  包里全部还是旧版本 —— 你的改动没有进包。")
                print("  最常见原因：改的是 out/by-partition/ 下导出出来的文件，")
                print("  而 titanpack.py 打的是 out/raw/ 里的镜像数据。")
                print("  正确做法：")
                print("    mkdir -p patch/system/priv-app/Settings")
                print("    cp 改好的.apk patch/system/priv-app/Settings/Settings.apk")
                print("    python3 titanpack.py out -o fs.7z --overlay patch --strict")
        return 0
    finally:
        if args.keep:
            print("\n临时目录保留: %s" % work)
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

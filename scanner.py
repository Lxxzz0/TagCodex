# -*- coding: utf-8 -*-
"""
扫图库：递归登记图库根下所有图片（不限层级，记下所在目录 dir），
生成缩略图，把 txt/xlsx 导入的占位记录回填成真实图片。

只读图片文件，不移动、不改名、不删除；只写数据库和缩略图目录。
扫描和反推不能同时跑（都写库、都吃 CPU）。
"""
import hashlib
import os
import threading
import time

from PIL import Image, ImageOps

import db
import importer
import tagger

THUMB_SIZE = 256             # 缩略图长边
THUMB_QUALITY = 80
HASH_CHUNK = 1 << 20
EXIF_ORIENTATION = 0x0112    # 5–8 表示旋转了 90°，宽高互换


class ScanBusy(tagger.TaggerBusy):
    """已有扫描或反推任务在跑（继承 TaggerBusy，接口统一回 409）。"""


# ---------------------------------------------------------------------
#  文件工具
# ---------------------------------------------------------------------
def thumbs_dir(cfg):
    return cfg.get("thumbs_dir") or os.path.join(os.path.dirname(cfg["db_path"]), "thumbs")


def thumb_path(cfg, image_id):
    return os.path.join(thumbs_dir(cfg), "%d.webp" % image_id)


def sha1_of(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(HASH_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def make_thumb(src, dst):
    """生成长边 THUMB_SIZE 的 webp，先写临时文件再替换。返回原图（exif 校正后）的 (宽, 高)。"""
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        size = im.size
        im = im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB")
        im.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.LANCZOS)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = dst + ".tmp"
        im.save(tmp, "WEBP", quality=THUMB_QUALITY)
    os.replace(tmp, dst)
    return size


def ensure_thumb(cfg, image_id, path):
    """缩略图缺了就补一张；返回缩略图路径，原图读不了时返回 None。"""
    dst = thumb_path(cfg, image_id)
    if os.path.exists(dst):
        return dst
    try:
        make_thumb(path, dst)
    except (OSError, ValueError):
        return None
    return dst


def place(root, path):
    """
    (dir, set_name)：dir 是图片所在目录相对图库根的路径（/ 分隔，根目录下为 ''），不限层级；
    set_name 是所在目录的名字（根目录下的图取图库根自己的名字），导入记录回填按它和文件名对。
    """
    d = os.path.relpath(os.path.dirname(path), root)
    d = "" if d == "." else d.replace(os.sep, "/")
    return d, os.path.basename(os.path.dirname(path))


def list_files(root):
    out = []
    for d, dirs, names in os.walk(root):
        dirs.sort()
        out.extend(os.path.join(d, n) for n in sorted(names) if n.lower().endswith(tagger.IMAGE_EXTS))
    return out


# ---------------------------------------------------------------------
#  后台任务：同一时间只跑一个
# ---------------------------------------------------------------------
_lock = threading.Lock()
JOB = {"running": False}


def _snapshot():
    with _lock:
        return dict(JOB)


def status(cfg):
    return {"root": cfg.get("gallery_root", ""), "job": _snapshot()}


def _new_report():
    return {"files": 0, "new": 0, "adopted": 0, "unchanged": 0, "returned": 0, "missing": 0,
            "changed": [], "moved": [], "backfilled": [], "backfill_ambiguous": [],
            "duplicates": [], "failed": []}


def start(cfg, opts):
    root = (opts.get("root") or cfg.get("gallery_root") or "").strip().strip('"')
    if not root:
        raise importer.ImportFailed("没有填图库根目录")
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise importer.ImportFailed("目录不存在：%s" % root)
    sub = _sub_dir(root, opts.get("dir"))
    if tagger.JOB.get("running"):
        raise ScanBusy("反推任务正在跑，等它结束再扫描")
    with _lock:
        if JOB.get("running"):
            raise ScanBusy("已经有扫描任务在跑：%s" % JOB.get("root"))
        JOB.clear()
        JOB.update(running=True, stop=False, phase="列出文件", root=root, dir=sub, total=0, done=0,
                   current="", eta=None, started=db.now(), finished=None, error=None, report=None)
    t = threading.Thread(target=_run, args=(cfg, root, sub), daemon=True)
    t.start()
    return _snapshot()


def _sub_dir(root, sub):
    """
    只扫图库里的某个子目录：完整路径或相对图库根的路径（如 参考图/sfw/角色A）。
    dir 仍按图库根算，所以子目录必须在图库根里面。留空或等于根返回 None（扫整个图库）。
    """
    sub = (sub or "").strip().strip('"').strip("/\\")
    if not sub:
        return None
    sub = os.path.abspath(sub if os.path.isabs(sub) else os.path.join(root, sub))
    if os.path.normcase(sub) == os.path.normcase(root):
        return None
    if not os.path.normcase(sub).startswith(os.path.normcase(os.path.join(root, ""))):
        raise importer.ImportFailed("子目录必须在图库根目录里面：%s" % sub)
    if not os.path.isdir(sub):
        raise importer.ImportFailed("子目录不存在：%s" % sub)
    return sub


def stop():
    with _lock:
        if not JOB.get("running"):
            return False
        JOB["stop"] = True
        return True


def _set(**kw):
    with _lock:
        JOB.update(kw)


def _run(cfg, root, sub=None):
    """dir 相对 root 算；sub 不为空时只遍历这个子目录，「文件已不在」也只在它里面判断。"""
    root = os.path.abspath(root)         # 库里的路径一律是规范写法，否则同一个文件会认不出来
    sub = os.path.abspath(sub) if sub else None
    conn = db.connect(cfg["db_path"])
    report = _new_report()
    base = sub or root
    try:
        files = list_files(base)
        if JOB.get("stop"):              # 列文件时就点了停止（文件为 0 时循环里查不到）
            _set(phase="已停止")
            return
        _set(phase="扫描中", total=len(files))
        seen = set()
        t0 = time.time()
        for i, path in enumerate(files):
            if JOB.get("stop"):
                _set(phase="已停止")
                break
            _set(current=os.path.relpath(path, root))
            seen.add(os.path.normcase(path))
            try:
                _scan_one(conn, cfg, root, path, report)
            except Exception as e:       # 单张失败不影响其他
                report["failed"].append({"file": path, "error": str(e)})
            done = i + 1
            report["files"] = done
            _set(done=done, eta=round((time.time() - t0) / done * (len(files) - done)))
        else:
            _mark_missing(conn, base, seen, report)
            _set(phase="完成")
    except Exception as e:
        _set(phase="出错", error="扫描失败：%s" % e)
    finally:
        conn.close()
        _set(running=False, current="", eta=None, finished=db.now(), report=report)


def _scan_one(conn, cfg, root, path, report):
    where = place(root, path)
    dir_, set_name = where
    stem = os.path.splitext(os.path.basename(path))[0]
    st = os.stat(path)
    row = conn.execute("SELECT id, size, mtime, missing, dir, set_name FROM images WHERE path = ?", (path,)).fetchone()

    # 1) 登记过且没动过：只补缩略图、同步所在目录（schema 8 升级时没换算出来的在这里补上）
    if row is not None and row["size"] == st.st_size and row["mtime"] == st.st_mtime:
        with conn:
            if row["missing"]:
                report["returned"] += 1
            if row["missing"] or (row["dir"], row["set_name"]) != where:
                conn.execute("UPDATE images SET missing = 0, dir = ?, set_name = ? WHERE id = ?",
                             (dir_, set_name, row["id"]))
        ensure_thumb(cfg, row["id"], path)
        report["unchanged"] += 1
        return

    fields = dict(file_info(path, st), missing=0, dir=dir_, set_name=set_name, stem=stem)

    # 2) 登记过但内容变了：标签不动，重生缩略图，报告里列出（要不要重新反推由用户决定）
    #    size 为空的是在「反推目录」页反推过、第一次被扫到的，只补文件信息
    if row is not None:
        with conn:
            update_image(conn, row["id"], fields)
        make_thumb(path, thumb_path(cfg, row["id"]))
        if row["size"] is None:
            report["adopted"] += 1
        else:
            report["changed"].append(path)
        return

    image_id = _match(conn, path, set_name, stem, fields["sha1"], report)
    with conn:
        if image_id is None:
            image_id = conn.execute(
                "INSERT INTO images (path, origin, added) VALUES (?, 'scan', ?)", (path, db.now())).lastrowid
            report["new"] += 1
        elif image_id is False:          # 同内容重复文件，不入库
            return
        else:
            fields["path"] = path
        update_image(conn, image_id, fields)
    make_thumb(path, thumb_path(cfg, image_id))


def _match(conn, path, set_name, stem, sha, report):
    """
    新路径找对应的旧记录：返回 id（移动 / 回填占位）、None（新建）、False（重复文件，不登记）。
    """
    same = [r for r in conn.execute("SELECT id, path FROM images WHERE sha1 = ? AND path NOT LIKE 'import:%'",
                                    (sha,))]
    alive = [r for r in same if os.path.exists(r["path"])]
    if alive:
        report["duplicates"].append({"file": path, "same_as": alive[0]["path"]})
        return False
    if same:
        report["moved"].append({"from": same[0]["path"], "to": path})
        return same[0]["id"]

    r = conn.execute("SELECT id FROM images WHERE path = ?", ("import:%s/%s" % (set_name, stem),)).fetchone()
    if r is None:
        # 一期约定的退路：同套图找不到，全库只有一条同名占位也算
        cands = conn.execute("SELECT id, path FROM images WHERE path LIKE 'import:%' AND stem = ?",
                             (stem,)).fetchall()
        if len(cands) > 1:
            report["backfill_ambiguous"].append({"file": path, "candidates": [c["path"] for c in cands]})
            return None
        r = cands[0] if cands else None
    if r is None:
        return None
    report["backfilled"].append(path)
    return r["id"]


def file_info(path, st=None):
    """images 表里的文件信息：sha1、宽高、大小、修改时间。"""
    st = st or os.stat(path)
    w, h = _probe(path)
    return {"sha1": sha1_of(path), "w": w, "h": h, "size": st.st_size, "mtime": st.st_mtime}


def _probe(path):
    """读宽高（按 exif 方向校正），只读文件头不解码，顺便确认文件是能打开的图。"""
    with Image.open(path) as im:
        w, h = im.size
        if im.getexif().get(EXIF_ORIENTATION) in (5, 6, 7, 8):
            w, h = h, w
    return w, h


def update_image(conn, image_id, fields):
    cols = sorted(fields)
    conn.execute("UPDATE images SET %s WHERE id = ?" % ", ".join("%s = ?" % c for c in cols),
                 [fields[c] for c in cols] + [image_id])


def _mark_missing(conn, base, seen, report):
    """这次扫描的目录（图库根或子目录）下原来登记过、这次没扫到的，标为文件已不在。只在完整扫完时做。"""
    prefix = os.path.normcase(os.path.join(base, ""))
    gone = [r["id"] for r in conn.execute("SELECT id, path FROM images WHERE missing = 0 AND path NOT LIKE 'import:%'")
            if os.path.normcase(r["path"]).startswith(prefix) and os.path.normcase(r["path"]) not in seen]
    with conn:
        conn.executemany("UPDATE images SET missing = 1 WHERE id = ?", [(i,) for i in gone])
    report["missing"] = len(gone)

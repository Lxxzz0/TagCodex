# -*- coding: utf-8 -*-
"""
导出 tag_dict.json（词典快照，给 ComfyUI 节点等外部程序读，格式见 docs/标签词典-使用说明.md）和完整词典（给人看、做备份）。
tag_dict.json 先写临时文件再 os.replace，读的一方不会读到写一半的文件。
"""
import csv
import io
import json
import os
import threading
import time

import db

VERSION = 2
DEBOUNCE_SECONDS = 1.5

_timer = None
_lock = threading.Lock()
LAST = {"ts": None, "path": None, "error": None, "counts": None}


def build(conn):
    buckets = [{"key": r["key"], "name": r["name"], "joins": bool(r["joins"])}
               for r in conn.execute("SELECT key, name, joins FROM buckets ORDER BY ord, id")]
    # 规则表兜底落到未归类的词不写，交给读 tag_dict.json 的程序自己处理；手工/文件定的未归类照写
    tags = {r["word"]: r["key"] for r in conn.execute(
        "SELECT t.word, b.key FROM tags t JOIN buckets b ON b.id = t.bucket_id "
        "WHERE t.blocked = 0 AND (b.key <> ? OR t.source IN ('manual', 'file')) ORDER BY t.word",
        (db.UNKNOWN,))}
    aliases = {r["alias"]: r["word"] for r in conn.execute(
        "SELECT a.alias, t.word FROM tag_aliases a JOIN tags t ON t.id = a.tag_id ORDER BY a.alias")}
    blocked = [r[0] for r in conn.execute("SELECT word FROM tags WHERE blocked = 1 ORDER BY word")]
    return {"version": VERSION, "generated": db.now(), "buckets": buckets,
            "tags": tags, "aliases": aliases, "blocked": blocked}


def _atomic_write(path, text):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    # ComfyUI 正在读时 Windows 上 replace 可能短暂失败，重试几次
    for i in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == 9:
                raise
            time.sleep(0.1)


def export(db_path, out_path):
    conn = db.connect(db_path)
    try:
        data = build(conn)
    finally:
        conn.close()
    try:
        _atomic_write(out_path, json.dumps(data, ensure_ascii=False, indent=1))
        counts = {"buckets": len(data["buckets"]), "tags": len(data["tags"]),
                  "aliases": len(data["aliases"]), "blocked": len(data["blocked"])}
        LAST.update(ts=data["generated"], path=out_path, error=None, counts=counts)
    except OSError as e:
        LAST.update(ts=db.now(), path=out_path, error="写 tag_dict.json 失败：%s" % e)
        raise
    return dict(LAST)


def schedule(db_path, out_path, delay=DEBOUNCE_SECONDS):
    """词典改动后调用；连续改动只在最后一次之后 delay 秒写一次。"""
    global _timer

    def run():
        try:
            export(db_path, out_path)
        except Exception:
            pass                         # 错误已记在 LAST，界面会显示

    with _lock:
        if _timer is not None:
            _timer.cancel()
        _timer = threading.Timer(delay, run)
        _timer.daemon = True
        _timer.start()


# ---------------------------------------------------------------------
#  完整导出：全部字段，csv（Excel 直接打开）或 json
# ---------------------------------------------------------------------
FULL_FIELDS = ["word", "zh", "bucket", "bucket_name", "source", "blocked",
               "note", "freq", "aliases", "created"]


def full_rows(conn):
    return [dict(r) for r in conn.execute(
        "SELECT t.word, t.zh, b.key AS bucket, b.name AS bucket_name, t.source, t.blocked, t.note, "
        "(SELECT COUNT(*) FROM image_tags it WHERE it.tag_id = t.id) AS freq, "
        "(SELECT group_concat(a.alias, ', ') FROM tag_aliases a WHERE a.tag_id = t.id) AS aliases, "
        "t.created FROM tags t JOIN buckets b ON b.id = t.bucket_id ORDER BY b.ord, t.word")]


def export_full(conn, fmt="csv"):
    """返回 (文件名, mimetype, 内容bytes)"""
    rows = full_rows(conn)
    stamp = db.now().replace(":", "").replace("-", "").replace("T", "_")
    if fmt == "json":
        buckets = [dict(r) for r in conn.execute(
            "SELECT key, name, ord, joins, builtin FROM buckets ORDER BY ord, id")]
        body = json.dumps({"generated": db.now(), "buckets": buckets, "tags": rows},
                          ensure_ascii=False, indent=1)
        return "tagcodex_dict_%s.json" % stamp, "application/json", body.encode("utf-8")
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FULL_FIELDS)
    w.writeheader()
    w.writerows(rows)
    # utf-8-sig：Excel 双击打开中文不乱码
    return "tagcodex_dict_%s.csv" % stamp, "text/csv", buf.getvalue().encode("utf-8-sig")

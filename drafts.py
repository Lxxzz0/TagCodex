# -*- coding: utf-8 -*-
"""
空白套图：组合器里从 0 创建的套图。

每一条存成 images 里的一行，沿用「仅导入记录」的占位路径 import:<套图名>/<条名>，origin = 'draft'，
stem = <套图名>_<三位序号>；词照常存 image_tags，分段和拼提示词都走图库那套。手动改词直接用图库的单图修改。
新建、加条、删条、改名、删除、写入组合结果都写 op_log（kind = draft），撤销走 tagops.undo_last。
"""
import re

import db
import importer
import tagops

KIND = "draft"
ORIGIN = "draft"
MAX_COUNT = 200          # 一次最多建 / 加这么多条
BAD_NAME = re.compile(r"[\\/:*?\"<>|]")


def _path(name, stem):
    return "import:%s/%s" % (name, stem)


def _stem(name, no):
    return "%s_%03d" % (name, no)


def _ids(conn, name):
    return [r[0] for r in conn.execute("SELECT id FROM images WHERE origin = ? AND set_name = ? ORDER BY stem",
                                       (ORIGIN, name))]


def _need_draft(conn, name):
    ids = _ids(conn, name)
    if not ids:
        raise tagops.OpError("没有叫「%s」的空白套图" % name)
    return ids


def _clean_name(conn, name, current=None):
    name = " ".join(str(name or "").split())
    if not name:
        raise tagops.OpError("套图名不能为空")
    if BAD_NAME.search(name):
        raise tagops.OpError("套图名里不能有 \\ / : * ? \" < > |（导出时要当文件名）")
    if name != current and conn.execute("SELECT 1 FROM images WHERE set_name = ? LIMIT 1", (name,)).fetchone():
        raise tagops.OpError("已经有叫「%s」的套图" % name)
    return name


def _count(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise tagops.OpError("条数要是 1–%d 的整数" % MAX_COUNT)
    if not 1 <= n <= MAX_COUNT:
        raise tagops.OpError("条数要是 1–%d 的整数" % MAX_COUNT)
    return n


def _next_no(conn, name):
    """接着最大序号往后编（删掉的号不回收）。"""
    nos = [0]
    for (stem,) in conn.execute("SELECT stem FROM images WHERE origin = ? AND set_name = ?", (ORIGIN, name)):
        tail = (stem or "")[len(name) + 1:]
        if tail.isdigit():
            nos.append(int(tail))
    return max(nos) + 1


def _insert(conn, name, count):
    start, ts = _next_no(conn, name), db.now()
    ids = []
    for no in range(start, start + count):
        stem = _stem(name, no)
        ids.append(conn.execute("INSERT INTO images (path, stem, set_name, origin, added) VALUES (?, ?, ?, ?, ?)",
                                (_path(name, stem), stem, name, ORIGIN, ts)).lastrowid)
    return ids


# ---------------------------------------------------------------------
#  快照（删条撤销用）：图片行 + 词 + 关联 + 主题成员，原样插回
# ---------------------------------------------------------------------
SNAP_TABLES = (("images", "id"), ("image_tags", "image_id"), ("image_links", "src_id"),
               ("image_links", "gen_id"), ("image_collections", "image_id"))


def _snapshot(conn, ids):
    marks = ",".join("?" * len(ids))
    snap = {}
    for table, col in SNAP_TABLES:
        rows = [dict(r) for r in conn.execute("SELECT * FROM %s WHERE %s IN (%s)" % (table, col, marks), ids)]
        snap.setdefault(table, [])
        snap[table] += [r for r in rows if r not in snap[table]]       # 两张都在要删的里面的关联只记一次
    return snap


def _restore(conn, snap):
    if snap["images"] and conn.execute("SELECT 1 FROM images WHERE path IN (%s)" % ",".join("?" * len(snap["images"])),
                                       [r["path"] for r in snap["images"]]).fetchone():
        raise tagops.OpError("同名的条已经又建出来了，无法撤销")
    for table, _ in SNAP_TABLES:
        for r in snap.get(table, []):
            if table == "image_tags" and not conn.execute("SELECT 1 FROM tags WHERE id = ?", (r["tag_id"],)).fetchone():
                continue                                               # 词后来被删了
            if table == "image_links" and not all(conn.execute("SELECT 1 FROM images WHERE id = ?", (r[c],)).fetchone()
                                                  for c in ("src_id", "gen_id")):
                continue                                               # 关联的另一张图已经不在了
            if table == "image_collections" and not conn.execute("SELECT 1 FROM collections WHERE id = ?",
                                                                 (r["collection_id"],)).fetchone():
                continue
            cols = list(r)
            conn.execute("INSERT OR IGNORE INTO %s (%s) VALUES (%s)" % (table, ",".join(cols), ",".join("?" * len(cols))),
                         [r[c] for c in cols])


# ---------------------------------------------------------------------
#  操作
# ---------------------------------------------------------------------
def list_drafts(conn):
    """[{name, count, filled}]：filled = 有词的条数。按名字排。"""
    return [dict(r) for r in conn.execute(
        "SELECT set_name AS name, COUNT(*) AS count, "
        "SUM(EXISTS (SELECT 1 FROM image_tags y WHERE y.image_id = i.id)) AS filled "
        "FROM images i WHERE origin = ? GROUP BY set_name ORDER BY set_name", (ORIGIN,))]


def create(conn, body):
    """{name, count} → {name, ids}"""
    with conn:
        name = _clean_name(conn, body.get("name"))
        ids = _insert(conn, name, _count(body.get("count")))
        db.log_op(conn, KIND, "新建空白套图「%s」（%d 条）" % (name, len(ids)), {"op": "create", "ids": ids})
    return {"name": name, "ids": ids}


def add_items(conn, body):
    """{name, count} → {name, ids}：接着最大序号加条。"""
    with conn:
        name = body.get("name")
        _need_draft(conn, name)
        ids = _insert(conn, name, _count(body.get("count")))
        db.log_op(conn, KIND, "空白套图「%s」加 %d 条" % (name, len(ids)), {"op": "create", "ids": ids})
    return {"name": name, "ids": ids}


def _delete(conn, ids, summary):
    snap = _snapshot(conn, ids)
    conn.execute("DELETE FROM images WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)
    db.log_op(conn, KIND, summary, {"op": "delete", "snap": snap})


def delete_items(conn, body):
    """{ids} → {count}：只能删空白套图的条。"""
    ids = [int(x) for x in body.get("ids") or []]
    if not ids:
        raise tagops.OpError("没有选要删的条")
    with conn:
        rows = conn.execute("SELECT id, set_name, origin FROM images WHERE id IN (%s)" % ",".join("?" * len(ids)),
                            ids).fetchall()
        if len(rows) != len(set(ids)) or any(r["origin"] != ORIGIN for r in rows):
            raise tagops.OpError("只能删空白套图里的条")
        names = sorted({r["set_name"] for r in rows})
        _delete(conn, sorted(set(ids)), "从空白套图「%s」删掉 %d 条" % ("、".join(names), len(rows)))
    return {"count": len(rows)}


def delete(conn, body):
    """{name}：删除整个空白套图。"""
    name = body.get("name")
    with conn:
        ids = _need_draft(conn, name)
        _delete(conn, ids, "删除空白套图「%s」（%d 条）" % (name, len(ids)))
    return {"ok": True}


def _rename(conn, old, new):
    for r in conn.execute("SELECT id, stem FROM images WHERE origin = ? AND set_name = ?", (ORIGIN, old)).fetchall():
        stem = new + r["stem"][len(old):] if (r["stem"] or "").startswith(old) else r["stem"]
        conn.execute("UPDATE images SET set_name = ?, stem = ?, path = ? WHERE id = ?",
                     (new, stem, _path(new, stem), r["id"]))


def rename(conn, body):
    """{name, new_name} → {name}：各条的 stem、path 跟着改。"""
    old = body.get("name")
    with conn:
        _need_draft(conn, old)
        new = _clean_name(conn, body.get("new_name"), current=old)
        if new == old:
            return {"name": old}
        _rename(conn, old, new)
        db.log_op(conn, KIND, "空白套图「%s」改名为「%s」" % (old, new), {"op": "rename", "old": old, "new": new})
    return {"name": new}


def apply(conn, body):
    """
    {source, slots} → {count, lora}：把组合结果整份写进来源里的各条（来源必须全是空白套图的条）。
    词典里没有的词加进它所在的段（来源记手工）；LoRA 标记不写（它跟着预设走，导出时由槽带上），lora 返回跳过的个数。
    整批一条 op_log，撤销时各条还原成写入前的词，这次新建的词没有别的图在用就删掉。
    """
    import composer                  # composer 依赖 gallery / presets，放这里避免循环导入
    ids = composer.source_ids(conn, body.get("source"))
    with conn:
        bad = conn.execute("SELECT COUNT(*) FROM images WHERE origin <> ? AND id IN (%s)" % ",".join("?" * len(ids)),
                           [ORIGIN] + ids).fetchone()[0]
        if bad:
            raise tagops.OpError("只能写入空白套图：来源里有 %d 张是图库里的图" % bad)
        rows, _, _ = composer.compute(conn, body.get("source"), body.get("slots"))
        bucket_ids, ts = db.bucket_ids(conn), db.now()
        before, created, lora = {}, [], 0
        for row in rows:
            iid = row["image_id"]
            before[iid] = {"tags": [[r["tag_id"], r["ord"], r["score"]] for r in conn.execute(
                "SELECT tag_id, ord, score FROM image_tags WHERE image_id = ? ORDER BY ord", (iid,))],
                "meta": list(conn.execute("SELECT edited, touched FROM images WHERE id = ?", (iid,)).fetchone())}
            tids = []
            for seg in row["segments"]:
                for w in seg["words"]:
                    if importer.LORA_RE.match(w):
                        lora += 1
                        continue
                    word = importer._resolve_alias(conn, importer.normalize(w))
                    r = conn.execute("SELECT id FROM tags WHERE word = ?", (word,)).fetchone()
                    if r is None:
                        tid = conn.execute("INSERT INTO tags (word, bucket_id, source, created) VALUES (?, ?, 'manual', ?)",
                                           (word, bucket_ids[seg["key"]], ts)).lastrowid
                        created.append(tid)
                    else:
                        tid = r[0]
                    if tid not in tids:
                        tids.append(tid)
            conn.execute("DELETE FROM image_tags WHERE image_id = ?", (iid,))
            conn.executemany("INSERT INTO image_tags (image_id, tag_id, ord) VALUES (?, ?, ?)",
                             [(iid, t, n) for n, t in enumerate(tids)])
            conn.execute("UPDATE images SET edited = ?, touched = ? WHERE id = ?", (ts, ts, iid))
        names = sorted({r["set_name"] for r in rows})
        db.log_op(conn, KIND, "组合结果写入空白套图「%s」（%d 条）" % ("、".join(names), len(rows)),
                  {"op": "apply", "before": {str(k): v for k, v in before.items()}, "created": created})
    return {"count": len(rows), "lora": lora}


def undo(conn, p):
    op = p["op"]
    if op == "create":
        conn.execute("DELETE FROM images WHERE origin = ? AND id IN (%s)" % ",".join("?" * len(p["ids"])),
                     [ORIGIN] + p["ids"])
    elif op == "delete":
        _restore(conn, p["snap"])
    elif op == "rename":
        if not _ids(conn, p["new"]):
            raise tagops.OpError("空白套图「%s」已经不在了，无法撤销改名" % p["new"])
        if conn.execute("SELECT 1 FROM images WHERE set_name = ? LIMIT 1", (p["old"],)).fetchone():
            raise tagops.OpError("又有叫「%s」的套图了，无法撤销改名" % p["old"])
        _rename(conn, p["new"], p["old"])
    elif op == "apply":
        for iid, b in p["before"].items():
            if not conn.execute("SELECT 1 FROM images WHERE id = ?", (int(iid),)).fetchone():
                continue
            conn.execute("DELETE FROM image_tags WHERE image_id = ?", (int(iid),))
            for tid, ord_, score in b["tags"]:
                if conn.execute("SELECT 1 FROM tags WHERE id = ?", (tid,)).fetchone():
                    conn.execute("INSERT INTO image_tags (image_id, tag_id, ord, score) VALUES (?, ?, ?, ?)",
                                 (int(iid), tid, ord_, score))
            conn.execute("UPDATE images SET edited = ?, touched = ? WHERE id = ?", b["meta"] + [int(iid)])
        for tid in p.get("created", []):
            if not conn.execute("SELECT 1 FROM image_tags WHERE tag_id = ?", (tid,)).fetchone():
                conn.execute("DELETE FROM tags WHERE id = ?", (tid,))
    else:
        raise tagops.OpError("不认识的空白套图操作：%s" % op)

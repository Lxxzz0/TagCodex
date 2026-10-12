# -*- coding: utf-8 -*-
"""
词典操作：段管理、词查询、批量归位/拉黑/添加、别名合并、统计、撤销。
会改归属的批量操作都写 op_log，撤销按后进先出只撤最近一条。
"""
import json
import re

import db
import importer

BUCKET_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
UNDO_LIST = 10

# 分歧：手工归过的词不算
FILE_CONFLICT = ("t.source <> 'manual' AND (SELECT COUNT(DISTINCT o.bucket_key) FROM tag_opinions o "
                 "WHERE o.tag_id = t.id AND o.source = 'file') > 1")
MACHINE_CONFLICT = ("t.source NOT IN ('manual', 'file') AND EXISTS (SELECT 1 FROM tag_opinions d "
                    "JOIN tag_opinions r ON r.tag_id = d.tag_id AND r.source = 'rule' "
                    "WHERE d.tag_id = t.id AND d.source = 'danbooru' AND d.bucket_key <> r.bucket_key "
                    "AND (r.bucket_key, d.bucket_key) NOT IN (VALUES %s))"
                    % ", ".join("('%s', '%s')" % (old, new)
                                for old, news in importer.RULE_COVERS.items() for new in news))


class OpError(ValueError):
    """给界面看的人话错误。"""


def _ids(values):
    try:
        out = list(dict.fromkeys(int(v) for v in values or []))
    except (TypeError, ValueError):
        raise OpError("ids 必须是整数列表")
    if not out:
        raise OpError("没有选中任何词")
    return out


def _bucket(conn, key):
    r = conn.execute("SELECT * FROM buckets WHERE key = ?", (key,)).fetchone()
    if r is None:
        raise OpError("段不存在：%s" % key)
    return r


def _states(conn, tag_ids):
    q = ("SELECT t.id, b.key AS bucket, t.source, t.blocked FROM tags t "
         "JOIN buckets b ON b.id = t.bucket_id WHERE t.id IN (%s)" % ",".join("?" * len(tag_ids)))
    found = {r["id"]: {"bucket": r["bucket"], "source": r["source"], "blocked": r["blocked"]}
             for r in conn.execute(q, tag_ids)}
    missing = [i for i in tag_ids if i not in found]
    if missing:
        raise OpError("词不存在：%s" % missing[:10])
    return found


def _set_state(conn, tag_id, st, bucket_ids):
    """按状态字典改一个词；段已被删就落到 unknown。"""
    if "bucket" in st:
        conn.execute("UPDATE tags SET bucket_id = ? WHERE id = ?",
                     (bucket_ids.get(st["bucket"], bucket_ids[db.UNKNOWN]), tag_id))
    if "source" in st:
        conn.execute("UPDATE tags SET source = ? WHERE id = ?", (st["source"], tag_id))
    if "blocked" in st:
        conn.execute("UPDATE tags SET blocked = ? WHERE id = ?", (int(st["blocked"]), tag_id))


# ---------------------------------------------------------------------
#  段
# ---------------------------------------------------------------------
def list_buckets(conn):
    rows = conn.execute(
        "SELECT b.id, b.key, b.name, b.ord, b.joins, b.builtin, "
        "(SELECT COUNT(*) FROM tags t WHERE t.bucket_id = b.id AND t.blocked = 0) AS count "
        "FROM buckets b ORDER BY b.ord, b.id").fetchall()
    return [dict(r) for r in rows]


def _check_name(conn, name, exclude_key=None):
    name = (name or "").strip()
    if not name:
        raise OpError("段名不能为空")
    if re.search(r"[\[\],]", name):
        raise OpError("段名不能含 [ ] 或逗号（txt 分节会解析错）")
    for r in conn.execute("SELECT key, name FROM buckets"):
        if r["key"] == exclude_key:
            continue
        if name.lower() in (r["name"].lower(), r["key"]):
            raise OpError("段名和已有的段重复：%s" % name)
    return name


def patch_buckets(conn, body):
    """body 只含一个操作：add / rename / order / joins / delete"""
    body = body or {}
    with conn:
        if "add" in body:
            key = (body["add"].get("key") or "").strip()
            if not BUCKET_KEY_RE.match(key):
                raise OpError("段 key 只能用小写字母开头的 a-z0-9_，最长 32")
            if conn.execute("SELECT 1 FROM buckets WHERE key = ?", (key,)).fetchone():
                raise OpError("段 key 已存在：%s" % key)
            name = _check_name(conn, body["add"].get("name"))
            # 新段插在 unknown 前面
            unk = _bucket(conn, db.UNKNOWN)["ord"]
            conn.execute("UPDATE buckets SET ord = ord + 1 WHERE ord >= ?", (unk,))
            conn.execute("INSERT INTO buckets (key, name, ord, joins, builtin) VALUES (?, ?, ?, 1, 0)",
                         (key, name, unk))
        elif "rename" in body:
            b = _bucket(conn, body["rename"].get("key"))
            name = _check_name(conn, body["rename"].get("name"), exclude_key=b["key"])
            conn.execute("UPDATE buckets SET name = ? WHERE id = ?", (name, b["id"]))
        elif "order" in body:
            keys = list(body["order"] or [])
            have = [r["key"] for r in conn.execute("SELECT key FROM buckets")]
            if sorted(keys) != sorted(have):
                raise OpError("排序必须包含全部段，且不能重复")
            for i, k in enumerate(keys):
                conn.execute("UPDATE buckets SET ord = ? WHERE key = ?", (i, k))
        elif "joins" in body:
            b = _bucket(conn, body["joins"].get("key"))
            conn.execute("UPDATE buckets SET joins = ? WHERE id = ?",
                         (1 if body["joins"].get("joins") else 0, b["id"]))
        elif "delete" in body:
            _delete_bucket(conn, body["delete"])
        else:
            raise OpError("不支持的段操作")
    return list_buckets(conn)


def _delete_bucket(conn, key):
    if key == db.UNKNOWN:
        raise OpError("未归类段不能删除")
    b = _bucket(conn, key)
    if conn.execute("SELECT 1 FROM presets WHERE bucket_id = ?", (b["id"],)).fetchone():
        raise OpError("这个段下还有预设，先删除或移走预设")
    order = [r["key"] for r in conn.execute("SELECT key FROM buckets ORDER BY ord, id")]
    moved = [r["id"] for r in conn.execute("SELECT id FROM tags WHERE bucket_id = ?", (b["id"],))]
    unk = _bucket(conn, db.UNKNOWN)["id"]
    conn.execute("UPDATE tags SET bucket_id = ? WHERE bucket_id = ?", (unk, b["id"]))
    conn.execute("DELETE FROM buckets WHERE id = ?", (b["id"],))
    rest = [k for k in order if k != key]
    for i, k in enumerate(rest):
        conn.execute("UPDATE buckets SET ord = ? WHERE key = ?", (i, k))
    db.log_op(conn, "bucket_delete", "删除段「%s」，%d 个词移到未归类" % (b["name"], len(moved)),
              {"bucket": dict(b), "order": order, "moved": moved})


# ---------------------------------------------------------------------
#  词查询
# ---------------------------------------------------------------------
SORTS = {
    "freq": "freq DESC, word",
    "word": "word",
    "created": "created DESC, id DESC",
}


def list_tags(conn, args):
    where, params = [], []
    bucket = args.get("bucket")
    if args.get("unclassified") in ("1", "true"):
        bucket = db.UNKNOWN
    if bucket:
        where.append("b.key = ?")
        params.append(bucket)
    blocked = args.get("blocked", "0")
    if blocked in ("0", "1"):
        where.append("t.blocked = ?")
        params.append(int(blocked))
    if args.get("source"):
        where.append("t.source = ?")
        params.append(args["source"])
    conflict = args.get("conflict")
    if conflict == "file":
        where.append(FILE_CONFLICT)
    elif conflict == "machine":
        where.append(MACHINE_CONFLICT)
    elif conflict in ("1", "any"):
        where.append("((%s) OR (%s))" % (FILE_CONFLICT, MACHINE_CONFLICT))
    q = (args.get("q") or "").strip()
    if q:
        like = "%" + q.lower().replace("_", " ").replace("\\", "\\\\").replace("%", "\\%") + "%"
        where.append("(t.word LIKE ? ESCAPE '\\' OR t.zh LIKE ? ESCAPE '\\' OR t.note LIKE ? ESCAPE '\\' "
                     "OR EXISTS (SELECT 1 FROM tag_aliases a WHERE a.tag_id = t.id AND a.alias LIKE ? ESCAPE '\\'))")
        params += [like] * 4
    sql = ("SELECT t.id, t.word, t.zh, t.note, t.blocked, t.source, t.created, "
           "b.key AS bucket, b.name AS bucket_name, "
           "(SELECT COUNT(*) FROM image_tags it WHERE it.tag_id = t.id) AS freq, "
           "(SELECT group_concat(a.alias, ', ') FROM tag_aliases a WHERE a.tag_id = t.id) AS aliases, "
           "(%s) AS file_conflict, (%s) AS machine_conflict "
           "FROM tags t JOIN buckets b ON b.id = t.bucket_id" % (FILE_CONFLICT, MACHINE_CONFLICT))
    if where:
        sql += " WHERE " + " AND ".join(where)
    order = SORTS.get(args.get("sort") or "freq", SORTS["freq"])
    page = db.paginate(conn, sql, params, args.get("page", 1), args.get("size", 100), order)
    if page["items"]:
        _attach_opinions(conn, page["items"])
    return page


def _attach_opinions(conn, items):
    ids = [it["id"] for it in items]
    ops = {}
    for r in conn.execute("SELECT tag_id, source, origin, bucket_key FROM tag_opinions "
                          "WHERE tag_id IN (%s)" % ",".join("?" * len(ids)), ids):
        ops.setdefault(r["tag_id"], []).append(
            {"source": r["source"], "origin": r["origin"], "bucket_key": r["bucket_key"]})
    for it in items:
        it["opinions"] = ops.get(it["id"], [])


def tag_images(conn, tag_id, page=1, size=60):
    if not conn.execute("SELECT 1 FROM tags WHERE id = ?", (tag_id,)).fetchone():
        raise OpError("词不存在：%s" % tag_id)
    return db.paginate(conn,
                       "SELECT i.id, i.path, i.stem, i.set_name, i.origin, i.sha1, i.missing FROM image_tags it "
                       "JOIN images i ON i.id = it.image_id WHERE it.tag_id = ?",
                       (tag_id,), page, size, "i.set_name, i.stem")


def update_tag(conn, tag_id, body):
    sets, params = [], []
    for f in ("zh", "note"):
        if f in body:
            sets.append("%s = ?" % f)
            params.append((body[f] or "").strip() or None)
    if not sets:
        raise OpError("没有要改的字段")
    with conn:
        n = conn.execute("UPDATE tags SET %s WHERE id = ?" % ", ".join(sets), params + [tag_id]).rowcount
    if not n:
        raise OpError("词不存在：%s" % tag_id)


# ---------------------------------------------------------------------
#  批量操作（都可撤销）
# ---------------------------------------------------------------------
def assign(conn, tag_ids, bucket_key):
    tag_ids = _ids(tag_ids)
    with conn:
        b = _bucket(conn, bucket_key)
        before = _states(conn, tag_ids)
        changes = []
        for tid in tag_ids:
            old = {"bucket": before[tid]["bucket"], "source": before[tid]["source"]}
            new = {"bucket": b["key"], "source": "manual"}
            if old != new:
                changes.append({"id": tid, "before": old, "after": new})
        conn.executemany("UPDATE tags SET bucket_id = ?, source = 'manual' WHERE id = ?",
                         [(b["id"], c["id"]) for c in changes])
        if changes:
            db.log_op(conn, "assign", "%d 个词归到「%s」" % (len(changes), b["name"]), {"tags": changes})
    return {"changed": len(changes)}


def block(conn, tag_ids, blocked=True):
    tag_ids = _ids(tag_ids)
    flag = 1 if blocked else 0
    with conn:
        before = _states(conn, tag_ids)
        changes = [{"id": t, "before": {"blocked": before[t]["blocked"]}, "after": {"blocked": flag}}
                   for t in tag_ids if before[t]["blocked"] != flag]
        conn.executemany("UPDATE tags SET blocked = ? WHERE id = ?", [(flag, c["id"]) for c in changes])
        if changes:
            db.log_op(conn, "block" if flag else "unblock",
                      "%d 个词%s黑名单" % (len(changes), "加入" if flag else "移出"), {"tags": changes})
    return {"changed": len(changes)}


def add_words(conn, bucket_key, text):
    """粘贴一批词（逗号或换行分隔）指定到某段。已有的词改段，新词入库并从参考表补中文。"""
    words = []
    for raw in re.split(r"[,\n]", text or ""):
        w = importer.normalize(raw)
        if w:
            words.append(w)
    if not words:
        raise OpError("没有可添加的词")
    with conn:
        b = _bucket(conn, bucket_key)
        ts = db.now()
        changes, created, unchanged = [], [], 0
        for w in dict.fromkeys(_alias_target(conn, w) for w in words):
            row = conn.execute("SELECT t.id, b.key AS bucket, t.source FROM tags t "
                               "JOIN buckets b ON b.id = t.bucket_id WHERE t.word = ?", (w,)).fetchone()
            if row is None:
                ref = importer.ref_lookup(conn, w)
                tid = conn.execute("INSERT INTO tags (word, bucket_id, zh, source, created) "
                                   "VALUES (?, ?, ?, 'manual', ?)",
                                   (w, b["id"], ref["zh"] if ref else None, ts)).lastrowid
                created.append(tid)
            elif (row["bucket"], row["source"]) != (b["key"], "manual"):
                conn.execute("UPDATE tags SET bucket_id = ?, source = 'manual' WHERE id = ?", (b["id"], row["id"]))
                changes.append({"id": row["id"], "before": {"bucket": row["bucket"], "source": row["source"]},
                                "after": {"bucket": b["key"], "source": "manual"}})
            else:
                unchanged += 1
        if changes or created:
            db.log_op(conn, "add", "添加到「%s」：新增 %d 个词，改段 %d 个词" % (b["name"], len(created), len(changes)),
                      {"tags": changes, "created": created})
    return {"created": len(created), "moved": len(changes), "unchanged": unchanged}


def _alias_target(conn, word):
    r = conn.execute("SELECT t.word FROM tag_aliases a JOIN tags t ON t.id = a.tag_id WHERE a.alias = ?",
                     (word,)).fetchone()
    return r["word"] if r else word


def merge_alias(conn, main_id, tag_ids):
    """把 tag_ids 并入 main_id：原词变成主词的别名，图关系和已有别名一起转过去，原词删除。"""
    tag_ids = _ids(tag_ids)
    main_id = int(main_id)
    if main_id in tag_ids:
        raise OpError("主词不能同时作为被合并的词")
    with conn:
        main = conn.execute("SELECT * FROM tags WHERE id = ?", (main_id,)).fetchone()
        if main is None:
            raise OpError("主词不存在：%s" % main_id)
        _states(conn, tag_ids)
        merged = []
        for tid in tag_ids:
            t = dict(conn.execute("SELECT * FROM tags WHERE id = ?", (tid,)).fetchone())
            snap = {
                "tag": t,
                "aliases": [r[0] for r in conn.execute("SELECT alias FROM tag_aliases WHERE tag_id = ?", (tid,))],
                "image_tags": [list(r) for r in conn.execute(
                    "SELECT image_id, ord FROM image_tags WHERE tag_id = ?", (tid,))],
                "opinions": [dict(r) for r in conn.execute(
                    "SELECT source, origin, bucket_key, seen FROM tag_opinions WHERE tag_id = ?", (tid,))],
                "moved_images": [],
            }
            for image_id, ord_ in snap["image_tags"]:
                cur = conn.execute("INSERT OR IGNORE INTO image_tags (image_id, tag_id, ord) VALUES (?, ?, ?)",
                                   (image_id, main_id, ord_))
                if cur.rowcount:
                    snap["moved_images"].append(image_id)
            conn.execute("UPDATE tag_aliases SET tag_id = ? WHERE tag_id = ?", (main_id, tid))
            conn.execute("DELETE FROM tags WHERE id = ?", (tid,))
            conn.execute("INSERT OR REPLACE INTO tag_aliases (alias, tag_id) VALUES (?, ?)", (t["word"], main_id))
            merged.append(snap)
        db.log_op(conn, "alias", "%d 个词合并为「%s」的别名" % (len(merged), main["word"]),
                  {"main_id": main_id, "merged": merged})
    return {"merged": len(merged)}


def remove_alias(conn, alias):
    with conn:
        n = conn.execute("DELETE FROM tag_aliases WHERE alias = ?", (importer.normalize(alias or ""),)).rowcount
    if not n:
        raise OpError("别名不存在：%s" % alias)


# ---------------------------------------------------------------------
#  撤销
# ---------------------------------------------------------------------
def list_ops(conn, limit=UNDO_LIST):
    rows = conn.execute("SELECT id, ts, kind, summary, undone FROM op_log ORDER BY id DESC LIMIT ?",
                        (limit,)).fetchall()
    return [dict(r) for r in rows]


def undo_last(conn):
    """撤销最近一条未撤销的操作。只能按顺序往回撤，避免和后面的操作打架。"""
    with conn:
        op = conn.execute("SELECT * FROM op_log WHERE undone = 0 ORDER BY id DESC LIMIT 1").fetchone()
        if op is None:
            raise OpError("没有可撤销的操作")
        p = json.loads(op["payload"])
        ids = db.bucket_ids(conn)
        kind = op["kind"]
        if kind == "alias":
            _undo_alias(conn, p)
        elif kind == "bucket_delete":
            _undo_bucket_delete(conn, p)
        elif kind == "image_edit":
            import gallery               # gallery 依赖本模块，放这里避免循环导入
            gallery.undo_image_edit(conn, p)
        elif kind == "image_link":
            import gallery
            gallery.undo_link(conn, p)
        elif kind == "theme":
            import themes
            themes.undo(conn, p)
        elif kind == "preset":
            import presets
            presets.undo(conn, p)
        elif kind == "draft":
            import drafts
            drafts.undo(conn, p)
        else:
            for c in reversed(p.get("tags", [])):
                if conn.execute("SELECT 1 FROM tags WHERE id = ?", (c["id"],)).fetchone():
                    _set_state(conn, c["id"], c["before"], ids)
            for tid in p.get("created", []):
                # 新加的词若已被后来的导入挂上图，就留着，只是不再算手工归类
                if conn.execute("SELECT 1 FROM image_tags WHERE tag_id = ?", (tid,)).fetchone():
                    conn.execute("UPDATE tags SET source = 'rule' WHERE id = ?", (tid,))
                else:
                    conn.execute("DELETE FROM tags WHERE id = ?", (tid,))
        conn.execute("UPDATE op_log SET undone = 1 WHERE id = ?", (op["id"],))
    return {"id": op["id"], "summary": op["summary"]}


def _undo_alias(conn, p):
    main_id = p["main_id"]
    for snap in reversed(p["merged"]):
        t = snap["tag"]
        if conn.execute("SELECT 1 FROM tags WHERE word = ?", (t["word"],)).fetchone():
            raise OpError("「%s」已经重新出现在词典里，无法撤销这次合并" % t["word"])
        conn.execute("DELETE FROM tag_aliases WHERE alias = ?", (t["word"],))
        cols = list(t.keys())
        conn.execute("INSERT INTO tags (%s) VALUES (%s)" % (",".join(cols), ",".join("?" * len(cols))),
                     [t[c] for c in cols])
        if snap["aliases"]:
            conn.execute("UPDATE tag_aliases SET tag_id = ? WHERE alias IN (%s)" % ",".join("?" * len(snap["aliases"])),
                         [t["id"]] + snap["aliases"])
        conn.executemany("DELETE FROM image_tags WHERE image_id = ? AND tag_id = ?",
                         [(i, main_id) for i in snap["moved_images"]])
        conn.executemany("INSERT OR IGNORE INTO image_tags (image_id, tag_id, ord) VALUES (?, ?, ?)",
                         [(i, t["id"], o) for i, o in snap["image_tags"]])
        conn.executemany("INSERT OR REPLACE INTO tag_opinions (tag_id, source, origin, bucket_key, seen) "
                         "VALUES (?, ?, ?, ?, ?)",
                         [(t["id"], o["source"], o["origin"], o["bucket_key"], o["seen"]) for o in snap["opinions"]])


def _undo_bucket_delete(conn, p):
    b = p["bucket"]
    if conn.execute("SELECT 1 FROM buckets WHERE key = ?", (b["key"],)).fetchone():
        raise OpError("段「%s」已经重新建过，无法撤销删除" % b["key"])
    conn.execute("INSERT INTO buckets (id, key, name, ord, joins, builtin) VALUES (?, ?, ?, ?, ?, ?)",
                 (b["id"], b["key"], b["name"], b["ord"], b["joins"], b["builtin"]))
    order = [k for k in p["order"] if conn.execute("SELECT 1 FROM buckets WHERE key = ?", (k,)).fetchone()]
    order += [r[0] for r in conn.execute("SELECT key FROM buckets") if r[0] not in order]
    for i, k in enumerate(order):
        conn.execute("UPDATE buckets SET ord = ? WHERE key = ?", (i, k))
    unk = db.bucket_ids(conn)[db.UNKNOWN]
    conn.executemany("UPDATE tags SET bucket_id = ? WHERE id = ? AND bucket_id = ?",
                     [(b["id"], tid, unk) for tid in p["moved"]])


# ---------------------------------------------------------------------
#  统计
# ---------------------------------------------------------------------
def stats(conn):
    one = lambda sql: conn.execute(sql).fetchone()[0]
    return {
        "total": one("SELECT COUNT(*) FROM tags WHERE blocked = 0"),
        "unclassified": one("SELECT COUNT(*) FROM tags t JOIN buckets b ON b.id = t.bucket_id "
                            "WHERE b.key = 'unknown' AND t.blocked = 0"),
        "blocked": one("SELECT COUNT(*) FROM tags WHERE blocked = 1"),
        "file_conflicts": one("SELECT COUNT(*) FROM tags t WHERE " + FILE_CONFLICT),
        "machine_conflicts": one("SELECT COUNT(*) FROM tags t WHERE " + MACHINE_CONFLICT),
        "buckets": list_buckets(conn),
        "images": one("SELECT COUNT(*) FROM images"),
        "ref_tags": one("SELECT COUNT(*) FROM ref_tags"),
        "rules_error": importer.RULES["error"],
        "char_list": {"count": len(importer.CHARS["words"]), "path": importer.CHARS["path"],
                      "error": importer.CHARS["error"]},
        "can_undo": one("SELECT COUNT(*) FROM op_log WHERE undone = 0"),
    }

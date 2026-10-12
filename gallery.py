# -*- coding: utf-8 -*-
"""
图库：以图为中心的查询（列表、文件夹树、详情、词频）、实时算分段与提示词、单图增删词和调序、图片关联。
筛选可以叠加主题（themes.member_sql）。

分段永远按词典实时算（tags.bucket_id），image_tags 只存「这张图有哪些词、什么顺序」。
单图修改写 op_log（kind = image_edit），关联写 op_log（kind = image_link），撤销走 tagops.undo_last。
"""
import re

import db
import importer
import presets
import tagops
import themes

PROMPT_SEP = ", "            # 拼提示词时段与段、词与词之间的分隔符
PLACEHOLDER = "i.path LIKE 'import:%'"
DRAFT = "i.origin = 'draft'"          # 空白套图的条（也是 import: 占位路径，见 drafts.py）
SORTS = {
    "touched": "i.touched IS NULL, i.touched DESC, i.id DESC",
    "added": "i.added DESC, i.id DESC",
    "name": "i.dir, i.set_name, i.stem, i.id",
}
# sort=hit 只在按预设组筛选时有效：命中比例从高到低（同组词数相同，按命中数排即可），同比例按最近更新
SORT_HIT = "group_hit DESC, " + SORTS["touched"]
HAS_TAGS = "EXISTS (SELECT 1 FROM image_tags x WHERE x.image_id = i.id)"
# 图库内外按 dir 判断：扫描登记的图都有 dir（根目录下为 ''）；在「反推目录」里反推的图库外的图没有
SCOPES = {
    "gallery": "i.dir IS NOT NULL AND NOT (%s)" % PLACEHOLDER,
    "outside": "i.dir IS NULL AND NOT (%s)" % PLACEHOLDER,
    "placeholder": "%s AND NOT %s" % (PLACEHOLDER, DRAFT),
    "draft": DRAFT,
}
DIR_TREE_SQL = "(i.dir = ? OR i.dir LIKE ? ESCAPE '\\')"     # 这个文件夹和它的子文件夹
STATES = {
    "untagged": "NOT %s AND NOT %s" % (HAS_TAGS, DRAFT),      # 空白套图没词不算未反推
    "tagged": HAS_TAGS,
    "missing": "i.missing = 1",
}
IMAGE_COLS = ("i.id, i.path, i.stem, i.dir, i.set_name, i.origin, i.w, i.h, i.sha1, i.missing, "
              "i.tagger, i.tagged, i.touched, i.edited, i.added")


# ---------------------------------------------------------------------
#  分段与提示词
# ---------------------------------------------------------------------
def _buckets(conn):
    return [dict(r) for r in conn.execute("SELECT id, key, name, joins FROM buckets ORDER BY ord")]


def segments(conn, image_ids):
    """{image_id: [{key, name, joins, tags: [...]}]}，包含全部段（含空段、新增段），顺序即拼接顺序。"""
    buckets = _buckets(conn)
    out = {i: [{"key": b["key"], "name": b["name"], "joins": bool(b["joins"]), "tags": []} for b in buckets]
           for i in image_ids}
    if not image_ids:
        return out
    pos = {b["id"]: n for n, b in enumerate(buckets)}
    marks = ",".join("?" * len(image_ids))
    rows = conn.execute(
        "SELECT it.image_id, it.score, t.id, t.word, t.zh, t.blocked, t.bucket_id FROM image_tags it "
        "JOIN tags t ON t.id = it.tag_id WHERE it.image_id IN (%s) ORDER BY it.image_id, it.ord" % marks,
        list(image_ids)).fetchall()
    tag_ids = list({r["id"] for r in rows})
    freq = {}
    for chunk in range(0, len(tag_ids), 500):            # SQLite 参数个数有上限
        part = tag_ids[chunk:chunk + 500]
        freq.update(conn.execute("SELECT tag_id, COUNT(*) FROM image_tags WHERE tag_id IN (%s) GROUP BY tag_id"
                                 % ",".join("?" * len(part)), part).fetchall())
    for r in rows:
        out[r["image_id"]][pos[r["bucket_id"]]]["tags"].append(
            {"id": r["id"], "word": r["word"], "zh": r["zh"], "score": r["score"],
             "blocked": bool(r["blocked"]), "freq": freq.get(r["id"], 0)})
    return out


def prompt_of(segs):
    """按段顺序拼成一句，跳过不参与拼接的段和黑名单词，都用 PROMPT_SEP 连接。"""
    parts = []
    for s in segs:
        if not s["joins"]:
            continue
        words = [t["word"] for t in s["tags"] if not t["blocked"]]
        if words:
            parts.append(PROMPT_SEP.join(words))
    return PROMPT_SEP.join(parts)


def _decorate(conn, items):
    segs = segments(conn, [it["id"] for it in items])
    for it in items:
        it["placeholder"] = it["path"].startswith("import:")
        it["thumb"] = None if it["placeholder"] else (it["sha1"] or "")[:8] or "0"
        it["segments"] = segs[it["id"]]
        it["prompt"] = prompt_of(it["segments"])
    return items


# ---------------------------------------------------------------------
#  列表 / 树 / 详情
# ---------------------------------------------------------------------
def _filters(conn, args):
    where, params = [], []
    # dir：这个文件夹和它的子文件夹；dir_only=1 只看直接放在这一层的（dir 为空 + dir_only 就是根目录下的图）
    d = (args.get("dir") or "").strip("/")
    if args.get("dir_only") in ("1", 1, True, "true"):
        where.append("i.dir = ?")
        params.append(d)
    elif d:
        where.append(DIR_TREE_SQL)
        params += [d, _like_escape(d) + "/%"]
    if args.get("set"):
        where.append("i.set_name = ?")
        params.append(args["set"])
    if args.get("scope") in SCOPES:
        where.append(SCOPES[args["scope"]])
    if args.get("state") in STATES:
        where.append(STATES[args["state"]])
    if args.get("theme"):
        try:
            theme_id = int(args["theme"])
        except (TypeError, ValueError):
            raise tagops.OpError("theme 必须是主题 id")
        w, p = themes.member_sql(conn, theme_id, args.get("theme_view"))
        where.append(w)
        params += p
    cols, cparams = IMAGE_COLS, []
    if args.get("group"):
        words = presets.group_words(conn, args["group"])
        mn = presets.match_min(args.get("group_min"))
        expr, cparams = presets.hit_sql(words)
        cols += ", %s AS group_hit, %d AS group_total" % (expr, len(words))
        w, p = presets.match_where(words, mn)
        where.append(w)
        params += p
    q = importer.normalize(args.get("q") or "")
    if q:
        like = "%" + _like_escape(q) + "%"
        where.append("EXISTS (SELECT 1 FROM image_tags y JOIN tags t ON t.id = y.tag_id "
                     "WHERE y.image_id = i.id AND (t.word = ? OR t.word LIKE ? ESCAPE '\\'))")
        params += [importer._resolve_alias(conn, q), like]
    sql = "SELECT %s FROM images i" % cols
    if where:
        sql += " WHERE " + " AND ".join(where)
    sort = args.get("sort") or "touched"
    order = SORT_HIT if sort == "hit" and args.get("group") else SORTS.get(sort, SORTS["touched"])
    return sql, cparams + params, order


def _like_escape(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_images(conn, args):
    """link_of=<图片 id> 时给每项标 linked：和那张图已经关联过（不分方向），挑图模式显示「已关联」用。"""
    sql, params, order = _filters(conn, args)
    page = db.paginate(conn, sql, params, args.get("page", 1), args.get("size", 60), order)
    _decorate(conn, page["items"])
    try:
        of = int(args.get("link_of") or 0)
    except (TypeError, ValueError):
        of = 0
    if of:
        linked = _linked_ids(conn, of)
        for it in page["items"]:
            it["linked"] = it["id"] in linked
    return page


def tree(conn):
    """
    文件夹树：{dirs: [{name, path, count, own, children}], root_own, ...计数}。
    count 含子文件夹，own 只算直接放在这一层的；root_own 是图库根下直接放的图。同名文件夹在不同父目录下各算各的。
    """
    nodes, top = {}, []

    def node(path):
        if path not in nodes:
            parent, _, name = path.rpartition("/")
            n = nodes[path] = {"name": name, "path": path, "count": 0, "own": 0, "children": []}
            (node(parent)["children"] if parent else top).append(n)
        return nodes[path]

    root_own = 0
    for r in conn.execute("SELECT i.dir, COUNT(*) AS n FROM images i WHERE %s GROUP BY i.dir ORDER BY i.dir"
                          % SCOPES["gallery"]):
        if not r["dir"]:
            root_own += r["n"]
            continue
        n = node(r["dir"])
        n["own"] += r["n"]
        parts = r["dir"].split("/")
        for k in range(1, len(parts) + 1):
            nodes["/".join(parts[:k])]["count"] += r["n"]

    def sort(lst):
        lst.sort(key=lambda n: n["name"].lower())
        for n in lst:
            sort(n["children"])
    sort(top)
    one = lambda where: conn.execute("SELECT COUNT(*) FROM images i WHERE " + where).fetchone()[0]
    return {"dirs": top, "root_own": root_own, "total": one("1"), "gallery": one(SCOPES["gallery"]),
            "outside": one(SCOPES["outside"]), "placeholder": one(SCOPES["placeholder"]), "draft": one(DRAFT),
            "untagged": one(STATES["untagged"]), "missing": one(STATES["missing"])}


def stats(conn, args, limit=200):
    """
    当前筛选结果（整个结果，不只一页）每段的词频：
    {total, segments: [{key, name, joins, tags: [{id, word, zh, blocked, count, ratio}]}]}。
    黑名单词也返回（带 blocked），显示与否由前端开关决定；每段按张数从多到少，最多 limit 个。
    """
    sql, params, _ = _filters(conn, args)
    return stats_of(conn, sql, params, limit)


def stats_of(conn, sql, params, limit=200):
    """stats 的统计部分：sql 是选出图片 id 的子查询（第一列是 id），组合器按来源 id 列表统计时也用它。"""
    total = conn.execute("SELECT COUNT(*) FROM (%s)" % sql, params).fetchone()[0]
    buckets = _buckets(conn)
    out = {b["id"]: {"key": b["key"], "name": b["name"], "joins": bool(b["joins"]), "tags": []} for b in buckets}
    rows = conn.execute(
        "SELECT t.id, t.word, t.zh, t.blocked, t.bucket_id, COUNT(*) AS n FROM image_tags it "
        "JOIN tags t ON t.id = it.tag_id WHERE it.image_id IN (SELECT id FROM (%s)) "
        "GROUP BY t.id ORDER BY n DESC, t.word" % sql, params).fetchall()
    for r in rows:
        tags = out[r["bucket_id"]]["tags"]
        if len(tags) < limit:
            tags.append({"id": r["id"], "word": r["word"], "zh": r["zh"], "blocked": bool(r["blocked"]),
                         "count": r["n"], "ratio": round(r["n"] / total, 4)})
    return {"total": total, "segments": [out[b["id"]] for b in buckets]}


def get_image(conn, image_id):
    r = conn.execute("SELECT %s FROM images i WHERE i.id = ?" % IMAGE_COLS, (image_id,)).fetchone()
    if r is None:
        raise tagops.OpError("图片不存在：%s" % image_id)
    return dict(r)


def detail(conn, image_id, args):
    """
    详情，另带关联的图（gens 生成图 / sources 原图）、所在的主题（themes）、命中的预设组（groups，默认阈值），
    和在当前筛选和排序下的位置与上一张/下一张。
    """
    item = _decorate(conn, [get_image(conn, image_id)])[0]
    item["gens"] = _link_items(conn, image_id, "gen")
    item["sources"] = _link_items(conn, image_id, "src")
    item["themes"] = themes.of_image(conn, image_id)
    item["groups"] = presets.groups_of_image(conn, image_id)
    sql, params, order = _filters(conn, args)
    ids = [r[0] for r in conn.execute("SELECT id FROM (%s) i ORDER BY %s" % (sql, order), params)]
    n = ids.index(image_id) if image_id in ids else None
    item["nav"] = {"index": n, "total": len(ids),
                   "prev": ids[n - 1] if n else None,
                   "next": ids[n + 1] if n is not None and n + 1 < len(ids) else None}
    return item


# ---------------------------------------------------------------------
#  单图修改（可撤销）
# ---------------------------------------------------------------------
def _snapshot(conn, image_id):
    return [[r["tag_id"], r["ord"], r["score"]] for r in conn.execute(
        "SELECT tag_id, ord, score FROM image_tags WHERE image_id = ? ORDER BY ord", (image_id,))]


def _rewrite(conn, image_id, rows):
    """按给定顺序整图重写，ord 重新从 0 编号。rows: [[tag_id, _, score]]"""
    conn.execute("DELETE FROM image_tags WHERE image_id = ?", (image_id,))
    conn.executemany("INSERT INTO image_tags (image_id, tag_id, ord, score) VALUES (?, ?, ?, ?)",
                     [(image_id, r[0], n, r[2]) for n, r in enumerate(rows)])


def edit_tags(conn, image_id, body):
    """
    一次一个操作：
      {"add": "a, b", "bucket": "outfit"?}  加词到这张图末尾；bucket 只用来提示「落到了别的段」
      {"remove": [tag_id]}                    从这张图删掉
      {"order": [tag_id, ...]}                这几个词按给定顺序重排，占用它们原来的位置，其他词不动
    """
    ops = [k for k in ("add", "remove", "order") if k in body]
    if len(ops) != 1:
        raise tagops.OpError("一次只能做一个操作：add / remove / order")
    op = ops[0]
    with conn:
        img = get_image(conn, image_id)
        before = _snapshot(conn, image_id)
        rows = [list(r) for r in before]
        result = {"changed": 0}
        created = []
        if op == "add":
            summary, result = _add(conn, rows, body, created)
        elif op == "remove":
            ids = set(tagops._ids(body["remove"]))
            rows = [r for r in rows if r[0] not in ids]
            result["changed"] = len(before) - len(rows)
            summary = "删掉 %d 个词" % result["changed"]
        else:
            order = tagops._ids(body["order"])
            on_image = [r[0] for r in rows]
            if not set(order) <= set(on_image):
                raise tagops.OpError("要排序的词不全在这张图上")
            slots = [n for n, r in enumerate(rows) if r[0] in set(order)]
            by_id = {r[0]: r for r in rows}
            for n, tid in zip(slots, order):
                rows[n] = by_id[tid]
            result["changed"] = int([r[0] for r in rows] != on_image)
            summary = "调整词序"
        if not result["changed"]:
            return result
        _rewrite(conn, image_id, rows)
        ts = db.now()
        conn.execute("UPDATE images SET edited = ?, touched = ? WHERE id = ?", (ts, ts, image_id))
        db.log_op(conn, "image_edit", "「%s」%s" % (img["stem"] or image_id, summary),
                  {"image_id": image_id, "before": before, "created": created,
                   "edited": img["edited"], "touched": img["touched"]})
    return result


def _add(conn, rows, body, created):
    words = [importer._resolve_alias(conn, importer.normalize(w))
             for w in re.split(r"[,\n]", body.get("add") or "")]
    words = [w for w in dict.fromkeys(words) if w]
    if not words:
        raise tagops.OpError("没有可添加的词")
    ids = db.bucket_ids(conn)
    want = body.get("bucket")
    on_image = {r[0] for r in rows}
    added, already, elsewhere = [], [], []
    ts = db.now()
    for w in words:
        row = conn.execute("SELECT t.id, b.key, b.name FROM tags t JOIN buckets b ON b.id = t.bucket_id "
                           "WHERE t.word = ?", (w,)).fetchone()
        if row is None:
            # 新词按导入规则归类：反推模型角色表 > danbooru > 规则表 > 未归类
            ref, ops, key, src = importer.machine_opinion(conn, w, ids)
            tid = conn.execute("INSERT INTO tags (word, bucket_id, zh, source, created) VALUES (?, ?, ?, ?, ?)",
                               (w, ids[key], ref["zh"] if ref else None, src, ts)).lastrowid
            importer.record_machine_opinions(conn, tid, ops, ts)
            created.append(tid)
            row = conn.execute("SELECT t.id, b.key, b.name FROM tags t JOIN buckets b ON b.id = t.bucket_id "
                               "WHERE t.id = ?", (tid,)).fetchone()
        if row["id"] in on_image:
            already.append(w)
            continue
        on_image.add(row["id"])
        rows.append([row["id"], None, None])
        added.append(w)
        if want and row["key"] != want:
            elsewhere.append({"word": w, "bucket": row["key"], "bucket_name": row["name"]})
    summary = "加了 %d 个词：%s" % (len(added), ", ".join(added[:5]) + ("…" if len(added) > 5 else ""))
    return summary, {"changed": len(added), "added": added, "already": already,
                     "elsewhere": elsewhere, "created": len(created)}


def undo_image_edit(conn, p):
    """撤销单图修改：标签还原成修改前；这次新建的词没有别的图在用就删掉。"""
    image_id = p["image_id"]
    if not conn.execute("SELECT 1 FROM images WHERE id = ?", (image_id,)).fetchone():
        raise tagops.OpError("图片已经不在库里，无法撤销")
    alive = {r[0] for r in conn.execute("SELECT id FROM tags WHERE id IN (%s)" % ",".join("?" * len(p["before"])),
                                        [r[0] for r in p["before"]])} if p["before"] else set()
    _rewrite(conn, image_id, [r for r in p["before"] if r[0] in alive])
    conn.execute("UPDATE images SET edited = ?, touched = ? WHERE id = ?", (p["edited"], p["touched"], image_id))
    for tid in p.get("created", []):
        if not conn.execute("SELECT 1 FROM image_tags WHERE tag_id = ?", (tid,)).fetchone():
            conn.execute("DELETE FROM tags WHERE id = ?", (tid,))


# ---------------------------------------------------------------------
#  图片关联（可撤销）
# ---------------------------------------------------------------------
# role：从这张图的角度看对方是什么。gen = 对方是它的生成图（这张 → 对方），src = 对方是它的原图（对方 → 这张）
LINK_SIDES = {"gen": ("gen_id", "src_id"), "src": ("src_id", "gen_id")}    # (对方那一列, 这张图那一列)
LINK_NAMES = {"gen": "生成图", "src": "原图"}
LINK_COLS = "i.id, i.path, i.stem, i.sha1, i.dir, i.set_name, i.missing"


def _link_items(conn, image_id, role):
    """关联区块里的一项：够显示缩略图和位置就行，不算分段。按关联时间排。"""
    other, me = LINK_SIDES[role]
    rows = conn.execute("SELECT %s FROM images i JOIN image_links l ON i.id = l.%s WHERE l.%s = ? "
                        "ORDER BY l.created, i.id" % (LINK_COLS, other, me), (image_id,)).fetchall()
    out = []
    for r in rows:
        it = dict(r)
        sha1 = it.pop("sha1")
        it["placeholder"] = it["path"].startswith("import:")
        it["thumb"] = None if it["placeholder"] else (sha1 or "")[:8] or "0"
        out.append(it)
    return out


def _linked_ids(conn, image_id):
    """和这张图关联过的图（不分方向）。"""
    return {r[0] for r in conn.execute("SELECT gen_id FROM image_links WHERE src_id = ? "
                                       "UNION SELECT src_id FROM image_links WHERE gen_id = ?",
                                       (image_id, image_id))}


def _image_ids(values):
    try:
        out = list(dict.fromkeys(int(v) for v in values or []))
    except (TypeError, ValueError):
        raise tagops.OpError("ids 必须是整数列表")
    if not out:
        raise tagops.OpError("没有选中任何图")
    return out


def links(conn, image_id, body):
    """
    一次一个操作：
      {"role": "gen", "add": [ids]}     这几张是这张图的生成图（这张图 → 它们）
      {"role": "src", "add": [ids]}     这几张是这张图的原图（它们 → 这张图）
      {"role": ..., "remove": [ids]}    取消关联
    同一对图只关联一次：同向已有的跳过，反向已有的报错。
    """
    role = body.get("role")
    if role not in LINK_SIDES:
        raise tagops.OpError("role 只能是 gen 或 src")
    ops = [k for k in ("add", "remove") if k in body]
    if len(ops) != 1:
        raise tagops.OpError("一次只能做一个操作：add / remove")
    ids = _image_ids(body[ops[0]])
    pair = (lambda x: (image_id, x)) if role == "gen" else (lambda x: (x, image_id))   # → (src, gen)
    with conn:
        img = get_image(conn, image_id)
        name = img["stem"] or image_id
        if ops[0] == "remove":
            return _unlink(conn, [pair(x) for x in ids], "「%s」取消关联 %%d 张%s" % (name, LINK_NAMES[role]))
        if image_id in ids:
            raise tagops.OpError("不能把图关联到它自己")
        found = {r[0] for r in conn.execute("SELECT id FROM images WHERE id IN (%s)" % ",".join("?" * len(ids)),
                                            ids)}
        lost = [str(i) for i in ids if i not in found]
        if lost:
            raise tagops.OpError("图片不存在：%s" % ", ".join(lost))
        ts = db.now()
        added = []
        for x in ids:
            src, gen = pair(x)
            if _has_link(conn, src, gen):
                continue
            if _has_link(conn, gen, src):
                raise tagops.OpError("「%s」和「%s」已经关联过（方向相反），要改方向请先取消原来的关联"
                                     % (name, get_image(conn, x)["stem"] or x))
            conn.execute("INSERT INTO image_links (src_id, gen_id, created) VALUES (?, ?, ?)", (src, gen, ts))
            added.append([src, gen])
        if added:
            db.log_op(conn, "image_link", "「%s」关联 %d 张%s" % (name, len(added), LINK_NAMES[role]),
                      {"added": added, "removed": []})
    return {"changed": len(added), "skipped": len(ids) - len(added)}


def _has_link(conn, src, gen):
    return conn.execute("SELECT 1 FROM image_links WHERE src_id = ? AND gen_id = ?", (src, gen)).fetchone() is not None


def _unlink(conn, pairs, summary):
    removed = []
    for src, gen in pairs:
        r = conn.execute("SELECT created FROM image_links WHERE src_id = ? AND gen_id = ?", (src, gen)).fetchone()
        if r is None:
            continue
        conn.execute("DELETE FROM image_links WHERE src_id = ? AND gen_id = ?", (src, gen))
        removed.append([src, gen, r["created"]])
    if removed:
        db.log_op(conn, "image_link", summary % len(removed), {"added": [], "removed": removed})
    return {"changed": len(removed), "skipped": len(pairs) - len(removed)}


def undo_link(conn, p):
    """撤销关联：删掉这次加的，补回这次删的（保留原来的关联时间）；已经不在库里的图跳过。"""
    for src, gen in p["added"]:
        conn.execute("DELETE FROM image_links WHERE src_id = ? AND gen_id = ?", (src, gen))
    for src, gen, created in p["removed"]:
        if conn.execute("SELECT COUNT(*) FROM images WHERE id IN (?, ?)", (src, gen)).fetchone()[0] == 2:
            conn.execute("INSERT OR IGNORE INTO image_links (src_id, gen_id, created) VALUES (?, ?, ?)",
                         (src, gen, created))

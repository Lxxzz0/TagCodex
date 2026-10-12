# -*- coding: utf-8 -*-
"""
主题：跨目录的一组图。成员 = 规则命中的图 + 手动加入的图 − 手动排除的图。

规则每次查询时实时算，不缓存成员；规则里的词存主词，查询时再换一次主词（建主题之后可能又合并了别名）。
所有改动写 op_log（kind = theme），payload 是改动前的整个主题快照，撤销走 tagops.undo_last。
"""
import json
import re

import db
import importer
import tagops

KIND = "theme"
RULE_KEYS = ("all", "any", "none")
RULE_NAMES = {"all": "全部包含", "any": "任一包含", "none": "都不包含"}


# ---------------------------------------------------------------------
#  规则
# ---------------------------------------------------------------------
def _words(conn, value):
    """逗号 / 换行分隔的字符串或列表 → 规范化、换成主词、去重保序。"""
    if isinstance(value, str):
        value = re.split(r"[,\n]", value)
    out = [importer._resolve_alias(conn, importer.normalize(str(w))) for w in value or []]
    return [w for w in dict.fromkeys(out) if w]


def normalize_rule(conn, rule):
    """校验并规范化规则。返回 (rule 或 None, 词典里没有的词)。None / 三栏全空 = 纯手工。"""
    if rule is None:
        return None, []
    if not isinstance(rule, dict):
        raise tagops.OpError("规则格式不对")
    out = {k: _words(conn, rule.get(k)) for k in RULE_KEYS}
    if not any(out.values()):
        return None, []
    if not out["all"] and not out["any"]:
        raise tagops.OpError("「全部包含」和「任一包含」至少填一栏")
    words = [w for k in RULE_KEYS for w in out[k]]
    known = {r[0] for r in conn.execute("SELECT word FROM tags WHERE word IN (%s)" % ",".join("?" * len(words)),
                                        words)}
    return out, [w for w in dict.fromkeys(words) if w not in known]


def _has_any(words):
    """图上有这些词中的任意一个（不算黑名单词）。"""
    return ("EXISTS (SELECT 1 FROM image_tags y JOIN tags t ON t.id = y.tag_id "
            "WHERE y.image_id = i.id AND t.blocked = 0 AND t.word IN (%s))" % ",".join("?" * len(words)))


def rule_sql(conn, rule):
    """规则 → 对 images i 的 WHERE 条件和参数。没有规则时返回永假条件。"""
    if not rule:
        return "0", []
    rule = {k: _words(conn, rule.get(k)) for k in RULE_KEYS}
    where, params = [], []
    for w in rule["all"]:
        where.append(_has_any([w]))
        params.append(w)
    if rule["any"]:
        where.append(_has_any(rule["any"]))
        params += rule["any"]
    if rule["none"]:
        where.append("NOT " + _has_any(rule["none"]))
        params += rule["none"]
    return " AND ".join(where), params


def member_sql(conn, theme_id, view=None, rule=...):
    """
    主题成员 → 对 images i 的 WHERE 条件和参数。view = 'excluded' 时改为被手动排除的图。
    rule 不传就用库里存的规则；预览时传入还没保存的规则。
    """
    if view == "excluded":
        return ("i.id IN (SELECT image_id FROM image_collections WHERE collection_id = ? AND excluded = 1)",
                [theme_id])
    if rule is ...:
        rule = _rule_of(get_theme(conn, theme_id))
    rsql, params = rule_sql(conn, rule)
    return ("((%s) OR i.id IN (SELECT image_id FROM image_collections WHERE collection_id = ? AND excluded = 0)) "
            "AND i.id NOT IN (SELECT image_id FROM image_collections WHERE collection_id = ? AND excluded = 1)"
            % rsql, params + [theme_id, theme_id])


def _rule_of(row):
    return json.loads(row["rule"]) if row["rule"] else None


# ---------------------------------------------------------------------
#  查询
# ---------------------------------------------------------------------
def get_theme(conn, theme_id):
    r = conn.execute("SELECT * FROM collections WHERE id = ? AND kind = ?", (theme_id, KIND)).fetchone()
    if r is None:
        raise tagops.OpError("主题不存在：%s" % theme_id)
    return r


def _count(conn, where, params):
    return conn.execute("SELECT COUNT(*) FROM images i WHERE " + where, params).fetchone()[0]


def list_themes(conn):
    """[{id, name, rule, note, count, excluded, cover: {id, thumb} | None}]，按名称排。"""
    import gallery                       # gallery 依赖本模块，放这里避免循环导入
    out = []
    for r in conn.execute("SELECT * FROM collections WHERE kind = ? ORDER BY name", (KIND,)).fetchall():
        where, params = member_sql(conn, r["id"])
        first = conn.execute("SELECT i.id, i.path, i.sha1 FROM images i WHERE %s ORDER BY %s LIMIT 1"
                             % (where, gallery.SORTS["touched"]), params).fetchone()
        cover = None
        if first and not first["path"].startswith("import:"):
            cover = {"id": first["id"], "thumb": (first["sha1"] or "")[:8] or "0"}
        out.append({"id": r["id"], "name": r["name"], "rule": _rule_of(r), "note": r["note"],
                    "count": _count(conn, where, params),
                    "excluded": _count(conn, *member_sql(conn, r["id"], "excluded")),
                    "cover": cover})
    return out


def of_image(conn, image_id):
    """这张图在哪些主题里：[{id, name, manual}]，manual = 手动加入的（否则是规则命中）。按名称排。"""
    manual = {r[0] for r in conn.execute("SELECT collection_id FROM image_collections WHERE image_id = ? "
                                         "AND excluded = 0", (image_id,))}
    out = []
    for r in conn.execute("SELECT id, name FROM collections WHERE kind = ? ORDER BY name", (KIND,)).fetchall():
        where, params = member_sql(conn, r["id"])
        if conn.execute("SELECT 1 FROM images i WHERE i.id = ? AND " + where, [image_id] + params).fetchone():
            out.append({"id": r["id"], "name": r["name"], "manual": r["id"] in manual})
    return out


def preview(conn, body):
    """{rule, theme_id?} → {count, unknown_words}：对话框里改规则时实时显示命中张数。"""
    rule, unknown = normalize_rule(conn, body.get("rule"))
    theme_id = body.get("theme_id")
    if theme_id:
        get_theme(conn, theme_id)
        where, params = member_sql(conn, theme_id, rule=rule)
    else:
        where, params = rule_sql(conn, rule)
    return {"count": _count(conn, where, params), "unknown_words": unknown}


# ---------------------------------------------------------------------
#  修改（可撤销）
# ---------------------------------------------------------------------
def _name(conn, name, exclude_id=None):
    name = (name or "").strip()
    if not name:
        raise tagops.OpError("主题名不能为空")
    r = conn.execute("SELECT id FROM collections WHERE kind = ? AND name = ?", (KIND, name)).fetchone()
    if r and r["id"] != exclude_id:
        raise tagops.OpError("已经有叫「%s」的主题" % name)
    return name


def _snapshot(conn, theme_id):
    r = conn.execute("SELECT * FROM collections WHERE id = ?", (theme_id,)).fetchone()
    if r is None:
        return None
    return {"row": dict(r), "members": [[m["image_id"], m["excluded"]] for m in conn.execute(
        "SELECT image_id, excluded FROM image_collections WHERE collection_id = ? ORDER BY image_id", (theme_id,))]}


def _log(conn, theme_id, before, summary):
    db.log_op(conn, "theme", summary, {"theme_id": theme_id, "before": before})


def _rule_json(rule):
    return json.dumps(rule, ensure_ascii=False) if rule else None


def create(conn, body):
    with conn:
        name = _name(conn, body.get("name"))
        rule, unknown = normalize_rule(conn, body.get("rule"))
        tid = conn.execute("INSERT INTO collections (name, kind, rule, note) VALUES (?, ?, ?, ?)",
                           (name, KIND, _rule_json(rule), (body.get("note") or "").strip() or None)).lastrowid
        _log(conn, tid, None, "新建主题「%s」" % name)
    return {"id": tid, "unknown_words": unknown}


def update(conn, theme_id, body):
    """{name?, rule?, note?}；rule: null 变成纯手工。"""
    with conn:
        before = _snapshot(conn, get_theme(conn, theme_id)["id"])
        row = before["row"]
        name = _name(conn, body["name"], theme_id) if "name" in body else row["name"]
        rule_text, unknown = row["rule"], []
        if "rule" in body:
            rule, unknown = normalize_rule(conn, body["rule"])
            rule_text = _rule_json(rule)
        note = ((body.get("note") or "").strip() or None) if "note" in body else row["note"]
        if (name, rule_text, note) == (row["name"], row["rule"], row["note"]):
            return {"changed": 0, "unknown_words": unknown}
        conn.execute("UPDATE collections SET name = ?, rule = ?, note = ? WHERE id = ?",
                     (name, rule_text, note, theme_id))
        _log(conn, theme_id, before, "修改主题「%s」" % name)
    return {"changed": 1, "unknown_words": unknown}


def delete(conn, theme_id):
    with conn:
        before = _snapshot(conn, get_theme(conn, theme_id)["id"])
        conn.execute("DELETE FROM collections WHERE id = ?", (theme_id,))
        _log(conn, theme_id, before, "删除主题「%s」" % before["row"]["name"])
    return {"ok": True}


def members(conn, theme_id, body):
    """
    一次一个操作：
      {"add": [ids]}      手动加入（原来被排除的也改成加入）
      {"remove": [ids]}   移出：手动加入的删掉；规则命中的记为排除
      {"restore": [ids]}  取消排除
    """
    import gallery
    ops = [k for k in ("add", "remove", "restore") if k in body]
    if len(ops) != 1:
        raise tagops.OpError("一次只能做一个操作：add / remove / restore")
    op = ops[0]
    ids = gallery._image_ids(body[op])
    with conn:
        before = _snapshot(conn, get_theme(conn, theme_id)["id"])
        name = before["row"]["name"]
        marks = ",".join("?" * len(ids))
        if op == "add":
            found = {r[0] for r in conn.execute("SELECT id FROM images WHERE id IN (%s)" % marks, ids)}
            lost = [str(i) for i in ids if i not in found]
            if lost:
                raise tagops.OpError("图片不存在：%s" % ", ".join(lost))
            conn.executemany("INSERT OR REPLACE INTO image_collections (image_id, collection_id, excluded) "
                             "VALUES (?, ?, 0)", [(i, theme_id) for i in ids])
            summary = "「%s」加入 %%d 张图" % name
        elif op == "remove":
            rsql, params = rule_sql(conn, _rule_of(before["row"]))
            hit = {r[0] for r in conn.execute("SELECT i.id FROM images i WHERE i.id IN (%s) AND (%s)"
                                              % (marks, rsql), ids + params)}
            conn.execute("DELETE FROM image_collections WHERE collection_id = ? AND excluded = 0 "
                         "AND image_id IN (%s)" % marks, [theme_id] + ids)
            conn.executemany("INSERT OR IGNORE INTO image_collections (image_id, collection_id, excluded) "
                             "VALUES (?, ?, 1)", [(i, theme_id) for i in ids if i in hit])
            summary = "「%s」移出 %%d 张图" % name
        else:
            conn.execute("DELETE FROM image_collections WHERE collection_id = ? AND excluded = 1 "
                         "AND image_id IN (%s)" % marks, [theme_id] + ids)
            summary = "「%s」恢复 %%d 张被排除的图" % name
        diff = set(map(tuple, before["members"])) ^ set(map(tuple, _snapshot(conn, theme_id)["members"]))
        n = len({image_id for image_id, _ in diff})     # 从「加入」变「排除」在对称差里有两行，按图数算
        if not n:
            return {"changed": 0}
        _log(conn, theme_id, before, summary % n)
    return {"changed": n}


def undo(conn, p):
    """撤销：删掉现在的主题，再把改动前的快照原样插回（保留 id）；已经不在库里的图跳过。"""
    conn.execute("DELETE FROM collections WHERE id = ?", (p["theme_id"],))
    before = p["before"]
    if before is None:
        return
    row = before["row"]
    cols = list(row)
    conn.execute("INSERT INTO collections (%s) VALUES (%s)" % (",".join(cols), ",".join("?" * len(cols))),
                 [row[c] for c in cols])
    conn.executemany("INSERT INTO image_collections (image_id, collection_id, excluded) "
                     "SELECT ?, ?, ? WHERE EXISTS (SELECT 1 FROM images WHERE id = ?)",
                     [(i, row["id"], ex, i) for i, ex in before["members"]])

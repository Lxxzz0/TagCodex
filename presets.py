# -*- coding: utf-8 -*-
"""
预设：属于某一段的一组词（三期组合器往槽里填的内容）。同一段里不能重名，不同段可以同名（同名的算一个预设组）。

内容存 ", " 分隔的词：去空、按规范化写法去重、保持顺序，词本身照原样存（手填的画师名可能不在词典里）。
LoRA 存多行文本，每行一个 <lora:名字:权重>（组合器输出时放在这段最前面）。
所有改动写 op_log（kind = preset），payload 是改动前的快照，撤销走 tagops.undo_last。
"""
import re

import db
import importer
import tagops

SEP = ", "
FIELDS = ("name", "bucket_id", "content", "lora", "note", "starred")


class PresetExists(tagops.OpError):
    """同一段里已有同名预设；existing 是那条预设，界面据此问要不要覆盖。"""

    def __init__(self, msg, existing):
        super().__init__(msg)
        self.existing = existing


class PresetGroupConflict(tagops.OpError):
    """存预设组时有段已有同名预设；conflicts 是那些预设，界面据此让用户逐段合并。"""

    def __init__(self, msg, conflicts):
        super().__init__(msg)
        self.conflicts = conflicts


def normalize_content(text):
    if isinstance(text, (list, tuple)):
        text = ",".join(str(x) for x in text)
    out, seen = [], set()
    for w in re.split(r"[,\n]", text or ""):
        w = " ".join(w.split())
        key = importer.normalize(w)
        if key and key not in seen:
            seen.add(key)
            out.append(w)
    return SEP.join(out)


def normalize_lora(text):
    """
    每行一个：「名字:权重」「<lora:名字:权重>」或只写名字（权重默认 1）→ 统一成 <lora:名字:权重>，多行用换行连。
    逗号也当分隔。格式不对抛 OpError；空返回 None。
    """
    out = []
    for line in re.split(r"[\n,]", text or ""):
        line = line.strip()
        if not line:
            continue
        m = importer.LORA_RE.match(line)
        body = (m.group(1) if m else line).strip()
        name, weight = body, "1"
        if ":" in body:
            head, tail = body.rsplit(":", 1)
            try:
                float(tail)
                name, weight = head.strip(), tail.strip()
            except ValueError:
                pass
        if not name or "<" in name or ">" in name:
            raise tagops.OpError("LoRA 格式不对：%s（写 名字:权重 或 <lora:名字:权重>）" % line)
        tag = "<lora:%s:%s>" % (name, weight)
        if tag not in out:
            out.append(tag)
    return "\n".join(out) or None


def lora_tags(lora):
    """存着的 LoRA 文本 → [<lora:...>]"""
    return [x for x in (lora or "").split("\n") if x]


def _row(r):
    return {"id": r["id"], "name": r["name"], "bucket": r["bucket"], "bucket_name": r["bucket_name"],
            "content": r["content"], "lora": r["lora"], "note": r["note"], "starred": bool(r["starred"]),
            "group": r["group_size"]}


# group_size：同名预设有几个段（> 1 就是一个预设组）
SELECT = ("SELECT p.*, b.key AS bucket, b.name AS bucket_name, "
          "(SELECT COUNT(*) FROM presets x WHERE x.name = p.name) AS group_size FROM presets p "
          "JOIN buckets b ON b.id = p.bucket_id")


def list_presets(conn, bucket=None):
    """星标在前，然后按段顺序、名称。bucket 给了就只列这一段。"""
    sql, params = SELECT, []
    if bucket:
        sql += " WHERE b.key = ?"
        params.append(bucket)
    return [_row(r) for r in conn.execute(sql + " ORDER BY p.starred DESC, b.ord, p.name", params)]


def groups(conn):
    """
    预设组：同名预设出现在两个以上段的。[{name, presets: [{id, bucket, bucket_name}], text}]，组内按段顺序。
    text 是「复制整组」用的：各段内容按段顺序连起来，跳过不参与拼接的段，跨段重复的词只留第一次，不含 LoRA。
    """
    out, words = {}, {}
    for r in conn.execute("SELECT p.id, p.name, p.content, b.key, b.name AS bucket_name, b.joins FROM presets p "
                          "JOIN buckets b ON b.id = p.bucket_id ORDER BY p.name, b.ord"):
        out.setdefault(r["name"], []).append({"id": r["id"], "bucket": r["key"], "bucket_name": r["bucket_name"]})
        if r["joins"]:
            words.setdefault(r["name"], []).extend(w.strip() for w in re.split(r"[,\n]", r["content"] or ""))
    return [{"name": k, "presets": v, "text": SEP.join(_dedupe(words.get(k, [])))} for k, v in out.items() if len(v) > 1]


def _dedupe(words):
    """按规范化写法去重保序，写法保持原样。"""
    out, seen = [], set()
    for w in words:
        key = importer.normalize(w)
        if key and key not in seen:
            seen.add(key)
            out.append(w)
    return out


# ---------------------------------------------------------------------
#  按预设组匹配图（图库筛选）
# ---------------------------------------------------------------------
MATCH_MIN = 50          # 默认阈值：图上含组里词的比例（%）


def group_words(conn, name):
    """组里各段预设的词（不含 LoRA）：规范化、换成主词、去重保序。"""
    words = []
    for r in conn.execute("SELECT p.content FROM presets p JOIN buckets b ON b.id = p.bucket_id "
                          "WHERE p.name = ? ORDER BY b.ord", (name,)):
        words += [importer._resolve_alias(conn, importer.normalize(w)) for w in re.split(r"[,\n]", r[0] or "")]
    return [w for w in dict.fromkeys(words) if w]


def match_min(value):
    """阈值参数：1–100 的整数，不给用默认。"""
    if value is None or value == "":
        return MATCH_MIN
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise tagops.OpError("命中阈值要是 1–100 的整数")
    if not 1 <= v <= 100:
        raise tagops.OpError("命中阈值要是 1–100 的整数")
    return v


def hit_sql(words):
    """对 images i 的命中数表达式和参数：图上有这些词中的几个（黑名单词也算）。"""
    if not words:
        return "0", []
    return ("(SELECT COUNT(DISTINCT t.word) FROM image_tags y JOIN tags t ON t.id = y.tag_id "
            "WHERE y.image_id = i.id AND t.word IN (%s))" % ",".join("?" * len(words)), list(words))


def match_where(words, mn):
    """算这个组的图：命中数 × 100 ≥ 阈值 × 词数。没有词时永假。"""
    if not words:
        return "0", []
    expr, params = hit_sql(words)
    return "%s * 100 >= %d" % (expr, mn * len(words)), params


def match_counts(conn, mn=MATCH_MIN):
    """每个预设组的词数和阈值下命中的图数：[{name, total, count}]，按名字排序。"""
    out = []
    for (name,) in conn.execute("SELECT DISTINCT name FROM presets ORDER BY name"):
        words = group_words(conn, name)
        where, params = match_where(words, mn)
        n = conn.execute("SELECT COUNT(*) FROM images i WHERE " + where, params).fetchone()[0]
        out.append({"name": name, "total": len(words), "count": n})
    return out


def groups_of_image(conn, image_id, mn=MATCH_MIN):
    """这张图命中 ≥ 阈值的预设组：[{name, hit, total}]，按命中比例从高到低。"""
    have = {r[0] for r in conn.execute("SELECT t.word FROM image_tags y JOIN tags t ON t.id = y.tag_id "
                                       "WHERE y.image_id = ?", (image_id,))}
    out = []
    for (name,) in conn.execute("SELECT DISTINCT name FROM presets ORDER BY name"):
        words = group_words(conn, name)
        hit = sum(1 for w in words if w in have)
        if words and hit * 100 >= mn * len(words):
            out.append({"name": name, "hit": hit, "total": len(words)})
    out.sort(key=lambda g: -g["hit"] / g["total"])
    return out


def get_preset(conn, preset_id):
    r = conn.execute(SELECT + " WHERE p.id = ?", (preset_id,)).fetchone()
    if r is None:
        raise tagops.OpError("预设不存在：%s" % preset_id)
    return _row(r)


def _clean(conn, body, current=None):
    """body 里给了的字段校验后覆盖 current，返回完整的一行（不含 id）。"""
    row = dict(current or {"name": "", "bucket_id": None, "content": "", "lora": None, "note": None,
                           "starred": 0})
    if "name" in body:
        row["name"] = (body["name"] or "").strip()
    if "bucket" in body:
        row["bucket_id"] = tagops._bucket(conn, body["bucket"])["id"]
    if "content" in body:
        row["content"] = normalize_content(body["content"])
    if "lora" in body:
        row["lora"] = normalize_lora(body["lora"])
    if "note" in body:
        row["note"] = (body["note"] or "").strip() or None
    if "starred" in body:
        row["starred"] = int(bool(body["starred"]))
    if not row["name"]:
        raise tagops.OpError("预设名不能为空")
    if row["bucket_id"] is None:
        raise tagops.OpError("要选预设属于哪一段")
    if not row["content"] and not row["lora"]:
        raise tagops.OpError("预设内容不能为空")
    return row


def _same_name(conn, row, exclude_id=None):
    r = conn.execute("SELECT id FROM presets WHERE name = ? AND bucket_id = ? AND id IS NOT ?",
                     (row["name"], row["bucket_id"], exclude_id)).fetchone()
    return r["id"] if r else None


def _snapshot(conn, preset_id):
    r = conn.execute("SELECT * FROM presets WHERE id = ?", (preset_id,)).fetchone()
    return dict(r) if r else None


def _log(conn, preset_id, before, summary):
    db.log_op(conn, "preset", summary, {"preset_id": preset_id, "before": before})


def _write(conn, preset_id, row):
    conn.execute("UPDATE presets SET %s WHERE id = ?" % ", ".join("%s = ?" % f for f in FIELDS),
                 [row[f] for f in FIELDS] + [preset_id])


def create(conn, body):
    """{name, bucket, content, lora?, note?, starred?, overwrite?}；同段重名时不带 overwrite 抛 PresetExists。"""
    with conn:
        row = _clean(conn, body)
        dup = _same_name(conn, row)
        if dup is not None:
            if not body.get("overwrite"):
                raise PresetExists("这一段里已经有叫「%s」的预设" % row["name"], get_preset(conn, dup))
            before = _snapshot(conn, dup)
            _write(conn, dup, row)
            _log(conn, dup, before, "覆盖预设「%s」" % row["name"])
            return get_preset(conn, dup)
        pid = conn.execute("INSERT INTO presets (%s) VALUES (%s)" % (",".join(FIELDS), ",".join("?" * len(FIELDS))),
                           [row[f] for f in FIELDS]).lastrowid
        _log(conn, pid, None, "新建预设「%s」" % row["name"])
    return get_preset(conn, pid)


def create_group(conn, body):
    """
    {name, items: [{bucket, content}], overwrite?}：一次给多段建同名预设（预设组），整组只写一条 op_log。
    有段重名时不带 overwrite 抛 PresetGroupConflict、什么都不写；带了就只换那条的内容（LoRA、备注、星标不动），
    内容没变的段跳过。返回这次写过的预设。
    """
    name = (body.get("name") or "").strip()
    items = body.get("items") or []
    if not items:
        raise tagops.OpError("没有选任何词")
    with conn:
        rows, seen = [], set()
        for it in items:
            row = _clean(conn, {"name": name, "bucket": it.get("bucket"), "content": it.get("content")})
            if row["bucket_id"] in seen:
                raise tagops.OpError("同一段出现了两次：%s" % it.get("bucket"))
            seen.add(row["bucket_id"])
            rows.append((row, _same_name(conn, row)))
        dups = [d for _, d in rows if d is not None]
        if dups and not body.get("overwrite"):
            raise PresetGroupConflict("有 %d 段已经有叫「%s」的预设" % (len(dups), name),
                                      [get_preset(conn, d) for d in dups])
        logged, out = [], []
        for row, dup in rows:
            if dup is None:
                pid = conn.execute("INSERT INTO presets (%s) VALUES (%s)" % (",".join(FIELDS), ",".join("?" * len(FIELDS))),
                                   [row[f] for f in FIELDS]).lastrowid
                logged.append({"preset_id": pid, "before": None})
            else:
                before = _snapshot(conn, dup)
                if before["content"] == row["content"]:
                    continue
                conn.execute("UPDATE presets SET content = ? WHERE id = ?", (row["content"], dup))
                pid = dup
                logged.append({"preset_id": pid, "before": before})
            out.append(pid)
        if logged:
            merged = sum(1 for x in logged if x["before"] is not None)
            db.log_op(conn, "preset", "存预设组「%s」：新建 %d 段，合并 %d 段" % (name, len(logged) - merged, merged),
                      {"items": logged})
    return [get_preset(conn, pid) for pid in out]


def save_group(conn, body):
    """
    {name, new_name?, items: [{bucket, content, lora?}], starred?}：整组保存（预设组编辑器）。items 是保存后的完整组：
    原来有的段更新内容、LoRA、名字（备注不动；给了 starred 就整组设成它），没有的段新建，原来有、现在没有的段删除。
    新建或改名时库里已有这个名字就报错。整组只写一条 op_log；什么都没变不记。返回保存后的组内预设（按段顺序）。
    """
    old_name = (body.get("name") or "").strip()
    name = (body.get("new_name") or old_name).strip()
    items = body.get("items") or []
    if not name:
        raise tagops.OpError("预设组名不能为空")
    if not items:
        raise tagops.OpError("预设组里至少要有一段")
    with conn:
        if name != old_name and conn.execute("SELECT 1 FROM presets WHERE name = ?", (name,)).fetchone():
            raise tagops.OpError("已经有叫「%s」的预设（组）" % name)
        old = {r["bucket_id"]: dict(r) for r in conn.execute("SELECT * FROM presets WHERE name = ?", (old_name,))}             if old_name else {}
        rows, seen = [], set()
        for it in items:
            row = _clean(conn, {"name": name, "bucket": it.get("bucket"), "content": it.get("content"),
                                "lora": it.get("lora")})
            if row["bucket_id"] in seen:
                raise tagops.OpError("同一段出现了两次：%s" % it.get("bucket"))
            seen.add(row["bucket_id"])
            rows.append(row)
        starred = int(bool(body["starred"])) if "starred" in body else None
        logged = []
        for row in rows:
            before = old.get(row["bucket_id"])
            if starred is not None:
                row["starred"] = starred
            elif before is not None:
                row["starred"] = before["starred"]
            elif old:
                row["starred"] = max(o["starred"] for o in old.values())        # 新加的段跟着组
            if before is None:
                pid = conn.execute("INSERT INTO presets (%s) VALUES (%s)" % (",".join(FIELDS), ",".join("?" * len(FIELDS))),
                                   [row[f] for f in FIELDS]).lastrowid
                logged.append({"preset_id": pid, "before": None})
            elif any(before[f] != row[f] for f in ("name", "content", "lora", "starred")):
                conn.execute("UPDATE presets SET name = ?, content = ?, lora = ?, starred = ? WHERE id = ?",
                             (row["name"], row["content"], row["lora"], row["starred"], before["id"]))
                logged.append({"preset_id": before["id"], "before": before})
        for bid, before in old.items():
            if bid not in seen:
                conn.execute("DELETE FROM presets WHERE id = ?", (before["id"],))
                logged.append({"preset_id": before["id"], "before": before})
        if logged:
            summary = "新建预设组「%s」" % name if not old else (
                "修改预设组「%s」" % name if name == old_name else "修改预设组「%s」→「%s」" % (old_name, name))
            db.log_op(conn, "preset", summary, {"items": logged})
    return [_row(r) for r in conn.execute(SELECT + " WHERE p.name = ? ORDER BY b.ord", (name,))]


def delete_group(conn, name):
    """删掉这个名字的所有预设（整个预设组），记一条 op_log。"""
    name = (name or "").strip()
    with conn:
        old = [dict(r) for r in conn.execute("SELECT * FROM presets WHERE name = ?", (name,))]
        if not old:
            raise tagops.OpError("预设组不存在：%s" % name)
        conn.execute("DELETE FROM presets WHERE name = ?", (name,))
        db.log_op(conn, "preset", "删除预设组「%s」（%d 段）" % (name, len(old)),
                  {"items": [{"preset_id": o["id"], "before": o} for o in old]})
    return {"ok": True}


def update(conn, preset_id, body):
    with conn:
        before = _snapshot(conn, get_preset(conn, preset_id)["id"])
        row = _clean(conn, body, {f: before[f] for f in FIELDS})
        if row == {f: before[f] for f in FIELDS}:
            return get_preset(conn, preset_id)
        if _same_name(conn, row, preset_id) is not None:
            raise tagops.OpError("这一段里已经有叫「%s」的预设" % row["name"])
        _write(conn, preset_id, row)
        _log(conn, preset_id, before, "修改预设「%s」" % row["name"])
    return get_preset(conn, preset_id)


def delete(conn, preset_id):
    with conn:
        before = _snapshot(conn, get_preset(conn, preset_id)["id"])
        conn.execute("DELETE FROM presets WHERE id = ?", (preset_id,))
        _log(conn, preset_id, before, "删除预设「%s」" % before["name"])
    return {"ok": True}


def undo(conn, p):
    """撤销：删掉现在的这条，再把改动前的快照原样插回（保留 id）。所属段已被删时无法撤销。预设组（items）倒序逐条还原。"""
    if "items" in p:
        for x in reversed(p["items"]):
            undo(conn, x)
        return
    before = p["before"]
    if before is not None and not conn.execute("SELECT 1 FROM buckets WHERE id = ?",
                                               (before["bucket_id"],)).fetchone():
        raise tagops.OpError("预设所属的段已经删了，无法撤销")
    conn.execute("DELETE FROM presets WHERE id = ?", (p["preset_id"],))
    if before is None:
        return
    if conn.execute("SELECT 1 FROM presets WHERE name = ? AND bucket_id = ?",
                    (before["name"], before["bucket_id"])).fetchone():
        raise tagops.OpError("这一段里已经有叫「%s」的预设，无法撤销" % before["name"])
    cols = list(before)
    conn.execute("INSERT INTO presets (%s) VALUES (%s)" % (",".join(cols), ",".join("?" * len(cols))),
                 [before[c] for c in cols])

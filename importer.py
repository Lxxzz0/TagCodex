# -*- coding: utf-8 -*-
"""
xlsx / txt / danbooru 参考表导入。

归类优先级：文件分节（file_wins 打开时）> 已有的手工/文件归类 > danbooru > tag_rules > unknown。
可重复执行：词按规范化后的主词 upsert，图按 import:<目录名>/<文件名> upsert，
图的标签整体重写。
"""
import csv
import glob
import json
import os
import re
import sys

import db

SECTION_RE = re.compile(r"^\s*\[(.+?)\]\s*$")
GLUED_WORDS = 4          # 一个标签超过这么多单词，疑似节内换行粘连
SKIP_XLSX_COLS = {"序号", "图", "文件名", "完整提示词"}
LORA_RE = re.compile(r"^<lora:(.+)>$", re.I)   # 组合器导出的 LoRA 标记，导入时跳过，不建成词

# danbooru 表的 (大类, 小类) → 段。
# 不在表里的（二次元角色、画面量质、艺术家、无法分类、物品/动物……）视为没意见，交给规则表
_DANBOORU_GROUPS = [
    ("character", "人物", ["对象", "身份（职业）"]),
    ("appearance", "人物", ["头发发型", "眼睛", "翅膀", "肩部", "腿部", "腹部", "腰部", "指甲", "身材",
                            "胸部", "性器官", "皮肤", "年龄", "脸型", "眉毛", "瞳孔", "鼻子", "面部"]),
    ("expression", "人物", ["嘴巴", "牙齿", "舌头"]),
    ("expression", "表情动作", ["哭", "笑", "生气", "不开心", "蔑视", "其他表情", "颜文字"]),
    ("pose", "表情动作", ["基础动作", "手部动作", "手抓着某物", "手放在某地", "手部拿着某物", "腿部动作",
                          "性爱动作", "其他动作"]),
    ("pose", "镜头", ["人物视觉朝向"]),
    ("camera", "镜头", ["人物构图", "特写镜头", "其他沟通", "镜头角度", "效果"]),
    ("outfit", "服饰", ["上半身服装", "其他装饰物", "围巾", "头部装饰物", "手部装饰物", "正装", "脸部装饰物",
                        "腿部装饰物", "衣服套装", "衣服花纹", "衣服装饰", "衣服风格", "袜子", "裙子", "裤子", "鞋子"]),
    ("artist", "画面", ["艺术家风格", "艺术派系", "艺术类型", "艺术风格", "画笔", "颜色"]),
    ("mood", "画面", ["光照"]),
    ("mood", "环境", ["氛围"]),
    ("scene", "画面", ["背景"]),
    ("scene", "场景", ["城市", "室内", "室外"]),
    ("scene", "环境", ["云", "大自然", "天气", "天空", "季节", "水"]),
    ("scene", "物品", ["乐器", "其他物品", "学习用品", "成人玩具", "数码设备", "植物", "武器", "食物", "餐具"]),
]
DANBOORU_TO_KEY = {(cat, sub): key for key, cat, subs in _DANBOORU_GROUPS for sub in subs}


class ImportFailed(Exception):
    """给界面看的人话错误。"""


# ---------------------------------------------------------------------
#  tag_rules：项目内置的规则表（tag_rules.py），失败不崩
# ---------------------------------------------------------------------
RULES = {"module": None, "error": None}


def load_rules():
    try:
        import tag_rules
        if not callable(getattr(tag_rules, "classify", None)):
            raise AttributeError("tag_rules.py 里没有 classify()")
        RULES.update(module=tag_rules, error=None)
    except Exception as e:
        RULES.update(module=None, error="规则表加载失败，未命中的词将全部进未归类：%s" % e)
    return RULES


def normalize(tag):
    mod = RULES["module"]
    if mod is not None and hasattr(mod, "normalize"):
        return mod.normalize(tag)
    # 与 tag_rules.normalize 一致的兜底实现
    t = tag.strip().lower().replace("_", " ")
    while "  " in t:
        t = t.replace("  ", " ")
    return t


# 规则表只认旧六段，它的一个旧段覆盖了十段里的几个新段；意见落在覆盖范围内不算机器分歧
RULE_COVERS = {
    "character": ("appearance", "expression"),
    "pose": ("expression",),
    "scene": ("mood",),
}


def rule_bucket(word):
    mod = RULES["module"]
    if mod is None:
        return None
    try:
        return mod.classify(word, []) or None
    except Exception:
        return None


def _humanize_io(path, e):
    if isinstance(e, PermissionError):
        return ImportFailed("文件被占用（可能正在 Excel 里打开），请先关闭再导入：%s" % path)
    if isinstance(e, FileNotFoundError):
        return ImportFailed("找不到文件：%s" % path)
    return ImportFailed("读取失败：%s（%s）" % (path, e))


# ---------------------------------------------------------------------
#  解析
# ---------------------------------------------------------------------
def header_lookup(conn):
    """节名/列名 → 段 key。当前段的 key、中文名都认，内置旧中文名兜底。"""
    lk = dict(db.LEGACY_BUCKET_NAMES)
    for key, name in db.BUILTIN_BUCKETS:
        lk[name] = key
        lk[key] = key
    for r in conn.execute("SELECT key, name FROM buckets"):
        lk[r["name"]] = r["key"]
        lk[r["key"]] = r["key"]
    return {k.strip().lower(): v for k, v in lk.items()}


def split_section(text):
    """一节的内容切成词：节内多行用空格粘起来，再按逗号切。"""
    return [x.strip() for x in (text or "").split(",") if x.strip()]


def parse_txt(path, lookup):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            text = f.read()
    except Exception as e:
        raise _humanize_io(path, e)
    sections, cur, buf = [], None, []

    def flush():
        if cur is not None:
            sections.append((lookup.get(cur.lower()), cur, split_section(" ".join(buf))))

    for line in text.splitlines():
        m = SECTION_RE.match(line)
        if m:
            flush()
            cur, buf = m.group(1).strip(), []
        elif cur is not None:
            buf.append(line)
    flush()
    return sections


def _cell_str(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def parse_xlsx(path, lookup):
    try:
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception as e:
        raise _humanize_io(path, e)
    try:
        ws = wb.worksheets[0]
        rows = ws.iter_rows(values_only=True)
        head = [_cell_str(h) for h in (next(rows, None) or [])]
        if "文件名" not in head:
            raise ImportFailed("表头里没有「文件名」列：%s" % path)
        name_col = head.index("文件名")
        sec_cols = [(i, h) for i, h in enumerate(head) if h and h not in SKIP_XLSX_COLS]
        records, no_name = [], 0
        for row in rows:
            row = list(row) + [None] * (len(head) - len(row))
            stem = _cell_str(row[name_col])
            if not stem:
                if any(_cell_str(row[i]) for i, _ in sec_cols):
                    no_name += 1
                continue
            sections = []
            for i, h in sec_cols:
                # 单元格里的换行同 txt 节内换行：粘成空格
                text = " ".join(_cell_str(row[i]).splitlines())
                sections.append((lookup.get(h.lower()), h, split_section(text)))
            records.append((stem, sections))
        return records, no_name
    finally:
        wb.close()


# ---------------------------------------------------------------------
#  danbooru 参考表
# ---------------------------------------------------------------------
def import_ref(conn, xlsx_path):
    """整表重灌 ref_tags。返回统计。"""
    if not xlsx_path or not os.path.isfile(xlsx_path):
        raise ImportFailed("找不到 danbooru 表：%s 。运行 install.bat 下载，或在 config.json 的 danbooru_xlsx 填路径"
                           % (xlsx_path or "（config.json 的 danbooru_xlsx 没填）"))
    try:
        from openpyxl import load_workbook
        wb = load_workbook(xlsx_path, read_only=True)
    except Exception as e:
        raise _humanize_io(xlsx_path, e)
    try:
        rows = wb.worksheets[0].iter_rows(values_only=True)
        head = [_cell_str(h).lower() for h in next(rows)]
        need = ["english", "chinese", "category", "subcategory"]
        if any(c not in head for c in need):
            raise ImportFailed("danbooru 表缺少列：%s" % [c for c in need if c not in head])
        ix = [head.index(c) for c in need]
        out = {}
        for row in rows:
            en, zh, cat, sub = (_cell_str(row[i]) if i < len(row) else "" for i in ix)
            w = normalize(en)
            if not w:
                continue
            out[w] = (w, zh or None, cat, sub, DANBOORU_TO_KEY.get((cat, sub)))
    finally:
        wb.close()
    with conn:
        conn.execute("DELETE FROM ref_tags")
        conn.executemany("INSERT INTO ref_tags (word, zh, category, subcategory, bucket_key) "
                         "VALUES (?, ?, ?, ?, ?)", out.values())
    with_key = sum(1 for v in out.values() if v[4])
    return {"rows": len(out), "with_bucket": with_key, "with_zh": sum(1 for v in out.values() if v[1])}


def remap_ref(conn):
    """映射改了以后，用库里已存的大类小类重算 ref_tags.bucket_key，不用重读 xlsx。"""
    pairs = conn.execute("SELECT DISTINCT category, subcategory FROM ref_tags").fetchall()
    conn.executemany("UPDATE ref_tags SET bucket_key = ? WHERE category IS ? AND subcategory IS ?",
                     [(DANBOORU_TO_KEY.get((c, s)), c, s) for c, s in pairs])


def ref_lookup(conn, word):
    r = conn.execute("SELECT zh, bucket_key FROM ref_tags WHERE word = ?", (word,)).fetchone()
    if r is None and "\\" in word:
        # 反推输出带转义 \( \)，参考表里没有
        r = conn.execute("SELECT zh, bucket_key FROM ref_tags WHERE word = ?",
                         (word.replace("\\", ""),)).fetchone()
    return r


# ---------------------------------------------------------------------
#  反推模型的角色表：模型 csv 里 category=4 的词就是角色名，归到角色
#  只读 csv，不需要 onnxruntime；加载不到只是少一层意见，不报红
# ---------------------------------------------------------------------
CHARACTER_CATEGORY = "4"
CHARS = {"words": frozenset(), "path": None, "error": None}


def load_char_list(tagger_cfg):
    tagger_cfg = tagger_cfg or {}
    path = os.path.join(tagger_cfg.get("models_dir") or "", (tagger_cfg.get("model") or "") + ".csv")
    try:
        with open(path, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            next(reader)
            words = frozenset(normalize(row[1]) for row in reader if len(row) > 2 and row[2] == CHARACTER_CATEGORY)
        CHARS.update(words=words, path=path, error=None)
    except (OSError, StopIteration, csv.Error) as e:
        CHARS.update(words=frozenset(), path=path, error="反推模型角色表加载失败，角色名不会自动归到角色：%s" % e)
    return CHARS


def char_bucket(word):
    """模型角色表里的词归角色；反推输出的括号带转义 \\( \\)，表里没有，去掉再查。"""
    return "character" if word.replace("\\", "") in CHARS["words"] else None


# ---------------------------------------------------------------------
#  入库
# ---------------------------------------------------------------------
MACHINE_SOURCES = ("tagger", "danbooru", "rule")     # 机器意见的来源，按优先级


def machine_opinion(conn, word, ids):
    """
    机器归类：反推模型角色表 > danbooru > 规则表 > 未归类。
    返回 (参考表行, {来源: 意见段 key 或 None}, 段 key, 来源)。
    """
    ref = ref_lookup(conn, word)
    ops = {"tagger": char_bucket(word), "danbooru": ref["bucket_key"] if ref else None, "rule": rule_bucket(word)}
    for src in MACHINE_SOURCES:
        if ops[src] in ids:
            return ref, ops, ops[src], src
    return ref, ops, db.UNKNOWN, "rule"


def record_machine_opinions(conn, tag_id, ops, ts):
    """机器意见每次覆盖。ops: machine_opinion 返回的 {来源: 意见}"""
    for src, key in ops.items():
        conn.execute("DELETE FROM tag_opinions WHERE tag_id = ? AND source = ?", (tag_id, src))
        if key:
            conn.execute("INSERT INTO tag_opinions (tag_id, source, origin, bucket_key, seen) "
                         "VALUES (?, ?, '', ?, ?)", (tag_id, src, key, ts))


def reclassify_machine(conn, summary):
    """
    按当前映射重算机器来源（tagger / danbooru / rule）的词，刷新它们的机器意见；手工、文件来源不动。
    归属变了的词写一条 op_log，可整体撤销。返回改了几个词。调用方负责事务。
    """
    ids = db.bucket_ids(conn)
    key_of = {v: k for k, v in ids.items()}
    ts = db.now()
    changes = []
    rows = conn.execute("SELECT id, word, bucket_id, source FROM tags WHERE source IN (%s)"
                        % ",".join("?" * len(MACHINE_SOURCES)), MACHINE_SOURCES).fetchall()
    for r in rows:
        _ref, ops, key, src = machine_opinion(conn, r["word"], ids)
        before = (key_of.get(r["bucket_id"]), r["source"])
        if before != (key, src):
            conn.execute("UPDATE tags SET bucket_id = ?, source = ? WHERE id = ?", (ids[key], src, r["id"]))
            changes.append({"id": r["id"], "before": {"bucket": before[0], "source": before[1]},
                            "after": {"bucket": key, "source": src}})
        record_machine_opinions(conn, r["id"], ops, ts)
    if changes:
        moved = sum(1 for c in changes if c["before"]["bucket"] != c["after"]["bucket"])
        db.log_op(conn, "reclassify", "%s：%d 个词改段" % (summary, moved), {"tags": changes})
    return len(changes)


def run_pending_reclassify(conn):
    """库刚从七段升到十段（db.init_db 置了标记）时，重算参考表和机器来源的词。要在 load_rules / load_char_list 之后调用。"""
    if not conn.execute("SELECT 1 FROM meta WHERE key = ?", (db.RECLASSIFY_FLAG,)).fetchone():
        return None
    with conn:
        remap_ref(conn)
        n = reclassify_machine(conn, "十段迁移：重算机器归类")
        conn.execute("DELETE FROM meta WHERE key = ?", (db.RECLASSIFY_FLAG,))
    return n


def _resolve_alias(conn, word):
    r = conn.execute("SELECT t.word FROM tag_aliases a JOIN tags t ON t.id = a.tag_id "
                     "WHERE a.alias = ?", (word,)).fetchone()
    return r["word"] if r else word


def _import_records(conn, records, set_name, origin_file, file_wins, report):
    """
    records: [(stem, [(key|None, 原节名|None, [原始标签...])], extra?)]
    extra 只有反推时才有：{"path": 图片绝对路径, "scores": {原始标签: 置信度}, "tagger": 模型与参数}
    原节名为 None 表示没有分节（反推），不报「不认识的节名」。
    """
    ids = db.bucket_ids(conn)
    key_of = {v: k for k, v in ids.items()}
    ts = db.now()

    # 1) 逐图整理标签序列，收集文件意见
    per_image = []                       # (stem, [word...], {word: score}, extra)
    file_keys = {}                       # word -> {key}
    for rec in records:
        stem, sections = rec[0], rec[1]
        extra = rec[2] if len(rec) > 2 else {}
        raw_scores = extra.get("scores") or {}
        words, seen, scores = [], set(), {}
        for key, header, raws in sections:
            if key is None and raws and header is not None:
                report["unknown_headers"].add(header)
            for raw in raws:
                if LORA_RE.match(raw.strip()):
                    report["lora_skipped"] += 1
                    continue
                if len(raw.split()) > GLUED_WORDS:
                    report["glued"].append({"file": origin_file, "stem": stem,
                                            "section": header or "", "tag": raw})
                w = _resolve_alias(conn, normalize(raw))
                if not w:
                    continue
                if raw in raw_scores:
                    # 别名合并后多个写法落到同一主词，取最高置信度
                    scores[w] = max(scores.get(w, 0.0), float(raw_scores[raw]))
                if w in seen:
                    continue
                seen.add(w)
                words.append(w)
                if key is not None:
                    file_keys.setdefault(w, set()).add(key)
        per_image.append((stem, words, scores, extra))

    # 2) 逐词决定归属
    changes = []
    all_words = list(dict.fromkeys(w for _, ws, _s, _e in per_image for w in ws))
    for w in all_words:
        ref, ops, machine_key, machine_src = machine_opinion(conn, w, ids)

        fk = file_keys.get(w, set())
        if len(fk) == 1 and next(iter(fk)) != machine_key:
            report["file_vs_machine"] += 1

        row = conn.execute("SELECT id, bucket_id, source, zh FROM tags WHERE word = ?",
                           (w,)).fetchone()
        if file_wins and len(fk) == 1 and next(iter(fk)) in ids:
            target = (next(iter(fk)), "file")
        elif file_wins and len(fk) > 1:
            report["ambiguous"].add(w)
            target = None                # 有歧义不猜：已有的保持，新词走机器归类
        elif row is not None and row["source"] in ("manual", "file"):
            target = None
        else:
            target = (machine_key, machine_src)
        if target is None and row is None:
            target = (machine_key, machine_src)

        zh = ref["zh"] if ref else None
        if row is None:
            cur = conn.execute(
                "INSERT INTO tags (word, bucket_id, zh, source, created) VALUES (?, ?, ?, ?, ?)",
                (w, ids[target[0]], zh, target[1], ts))
            tag_id = cur.lastrowid
            report["tags_new"] += 1
            if target[1] == "file":
                changes.append({"id": tag_id, "before": {"bucket": machine_key, "source": machine_src},
                                "after": {"bucket": target[0], "source": "file"}})
        else:
            tag_id = row["id"]
            if zh and not row["zh"]:
                conn.execute("UPDATE tags SET zh = ? WHERE id = ?", (zh, tag_id))
            if target is not None:
                before = (key_of.get(row["bucket_id"]), row["source"])
                if before != target:
                    conn.execute("UPDATE tags SET bucket_id = ?, source = ? WHERE id = ?",
                                 (ids[target[0]], target[1], tag_id))
                    if before[0] != target[0]:
                        report["tags_changed"] += 1
                    else:
                        report["tags_locked"] += 1
                    if target[1] == "file":
                        changes.append({"id": tag_id,
                                        "before": {"bucket": before[0], "source": before[1]},
                                        "after": {"bucket": target[0], "source": "file"}})

        record_machine_opinions(conn, tag_id, ops, ts)

    # 3) 图与图-标签关系，整图重写
    tag_id_of = {}
    for stem, words, scores, extra in per_image:
        image_id = _upsert_image(conn, set_name, stem, extra, ts, report)
        if conn.execute("SELECT edited FROM images WHERE id = ?", (image_id,)).fetchone()[0]:
            # 手工改过的图默认不覆盖，勾了「也覆盖手工改过的图」才整图重写
            name = "%s/%s" % (set_name, stem)
            if not report["overwrite_edited"]:
                report["edited_skipped"].append(name)
                continue
            report["edited_overwritten"].append(name)
        if extra.get("path"):
            conn.execute("UPDATE images SET tagger = ?, tagged = ?, origin = 'tagged' WHERE id = ?",
                         (extra.get("tagger"), ts, image_id))
        conn.execute("UPDATE images SET touched = ?, edited = NULL WHERE id = ?", (ts, image_id))
        conn.execute("DELETE FROM image_tags WHERE image_id = ?", (image_id,))
        for i, w in enumerate(words):
            if w not in tag_id_of:
                tag_id_of[w] = conn.execute("SELECT id FROM tags WHERE word = ?", (w,)).fetchone()[0]
            conn.execute("INSERT INTO image_tags (image_id, tag_id, ord, score) VALUES (?, ?, ?, ?)",
                         (image_id, tag_id_of[w], i, scores.get(w)))

        # 文件意见只在「以文件分节为准」时记录；机器生成的分节没有保留价值
        origin = "%s#%s" % (origin_file, stem)
        conn.execute("DELETE FROM tag_opinions WHERE source = 'file' AND origin = ?", (origin,))
    if file_wins:
        for rec in records:
            stem, sections = rec[0], rec[1]
            origin = "%s#%s" % (origin_file, stem)
            for key, _h, raws in sections:
                if key is None:
                    continue
                for raw in raws:
                    w = _resolve_alias(conn, normalize(raw))
                    if w in tag_id_of:
                        conn.execute("INSERT OR REPLACE INTO tag_opinions "
                                     "(tag_id, source, origin, bucket_key, seen) VALUES (?, 'file', ?, ?, ?)",
                                     (tag_id_of[w], origin, key, ts))

    if changes:
        moved = sum(1 for c in changes if c["before"]["bucket"] != c["after"]["bucket"])
        db.log_op(conn, "import_file", "以文件分节为准导入 %s：%d 个词改段，%d 个词锁定为文件来源"
                  % (os.path.basename(origin_file), moved, len(changes) - moved), {"tags": changes})
        report["logged_changes"] += len(changes)


def _upsert_image(conn, set_name, stem, extra, ts, report):
    """
    找到或新建这张图，返回 id。
    反推（extra 带真实路径）：先按真实路径找，再把同名占位行升级成真实路径，都没有就新建。
    文件导入：先找占位行，再找同目录同名的真实图片（反推过或扫描登记的）；同名的不止一张时不猜，新建占位并报告。
    """
    placeholder = "import:%s/%s" % (set_name, stem)
    real = extra.get("path")
    if real:
        r = conn.execute("SELECT id FROM images WHERE path = ?", (real,)).fetchone()
        if r is None:
            r = conn.execute("SELECT id FROM images WHERE path = ?", (placeholder,)).fetchone()
            if r is not None:
                conn.execute("UPDATE images SET path = ?, origin = 'tagged' WHERE id = ?", (real, r["id"]))
                report["images_upgraded"] += 1
        if r is None:
            image_id = conn.execute(
                "INSERT INTO images (path, stem, set_name, origin, added) VALUES (?, ?, ?, 'tagged', ?)",
                (real, stem, set_name, ts)).lastrowid
            report["images_new"] += 1
        else:
            image_id = r["id"]
            report["images_updated"] += 1
        return image_id

    r = conn.execute("SELECT id FROM images WHERE path = ?", (placeholder,)).fetchone()
    if r is None:
        found = conn.execute("SELECT id FROM images WHERE set_name = ? AND stem = ? AND path NOT LIKE 'import:%'",
                             (set_name, stem)).fetchall()
        if len(found) == 1:
            r = found[0]
        elif len(found) > 1:
            report["image_ambiguous"].append("%s/%s" % (set_name, stem))
    if r is None:
        report["images_new"] += 1
        return conn.execute(
            "INSERT INTO images (path, stem, set_name, origin, added) VALUES (?, ?, ?, 'import', ?)",
            (placeholder, stem, set_name, ts)).lastrowid
    report["images_updated"] += 1
    return r["id"]


def _new_report(file_wins, overwrite_edited=False):
    return {"file_wins": file_wins, "overwrite_edited": overwrite_edited,
            "edited_skipped": [], "edited_overwritten": [], "files": 0, "images_new": 0, "images_updated": 0,
            "images_upgraded": 0, "image_ambiguous": [],
            "tags_new": 0, "tags_changed": 0, "tags_locked": 0, "file_vs_machine": 0, "logged_changes": 0,
            "glued": [], "ambiguous": set(), "unknown_headers": set(), "no_name_rows": 0, "lora_skipped": 0,
            "rules_error": RULES["error"]}


def _finish(report):
    report["ambiguous"] = sorted(report["ambiguous"])
    report["unknown_headers"] = sorted(report["unknown_headers"])
    return report


def import_xlsx(conn, path, file_wins=False, overwrite_edited=False):
    path = os.path.abspath(path)
    report = _new_report(file_wins, overwrite_edited)
    records, report["no_name_rows"] = parse_xlsx(path, header_lookup(conn))
    set_name = os.path.basename(os.path.dirname(path))
    report["set_name"] = set_name
    with conn:
        _import_records(conn, records, set_name, path, file_wins, report)
    report["files"] = 1
    return _finish(report)


def import_txt_dir(conn, directory, file_wins=False, overwrite_edited=False):
    directory = os.path.abspath(directory)
    if not os.path.isdir(directory):
        raise ImportFailed("目录不存在：%s" % directory)
    files = sorted(glob.glob(os.path.join(directory, "*.txt")))
    if not files:
        raise ImportFailed("目录里没有 txt：%s" % directory)
    report = _new_report(file_wins, overwrite_edited)
    lookup = header_lookup(conn)
    set_name = os.path.basename(directory)
    report["set_name"] = set_name
    with conn:
        for f in files:
            stem = os.path.splitext(os.path.basename(f))[0]
            _import_records(conn, [(stem, parse_txt(f, lookup))], set_name, f, file_wins, report)
            report["files"] += 1
    return _finish(report)


# ---------------------------------------------------------------------
#  命令行：python importer.py ref | xlsx <path> | txt <dir>  [--file-wins] [--db <path>]
# ---------------------------------------------------------------------
def main(argv):
    from app import CONFIG
    args = [a for a in argv if not a.startswith("--")]
    file_wins = "--file-wins" in argv
    db_path = CONFIG["db_path"]
    if "--db" in argv:
        db_path = argv[argv.index("--db") + 1]
        args.remove(db_path)
    load_rules()
    load_char_list(CONFIG.get("tagger"))
    conn = db.connect(db_path)
    db.init_db(conn)
    run_pending_reclassify(conn)
    try:
        if args[:1] == ["ref"]:
            rep = import_ref(conn, CONFIG["danbooru_xlsx"])
        elif args[:1] == ["xlsx"] and len(args) > 1:
            rep = import_xlsx(conn, args[1], file_wins)
        elif args[:1] == ["txt"] and len(args) > 1:
            rep = import_txt_dir(conn, args[1], file_wins)
        else:
            print("用法：python importer.py ref | xlsx <path> | txt <dir> [--file-wins] [--db <path>]")
            return 2
    except ImportFailed as e:
        print("导入失败：%s" % e)
        return 1
    finally:
        conn.close()
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

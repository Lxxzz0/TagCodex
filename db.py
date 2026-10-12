# -*- coding: utf-8 -*-
"""建表、连接、通用查询。不依赖 Flask。"""
import json
import os
import re
import sqlite3
from datetime import datetime

SCHEMA_VERSION = 8

# 十个内置段，顺序即拼接顺序；key 锁死，中文名可改
BUILTIN_BUCKETS = [
    ("character", "角色"),
    ("appearance", "外貌"),
    ("outfit", "服饰"),
    ("expression", "表情"),
    ("pose", "姿势"),
    ("camera", "镜头"),
    ("scene", "环境"),
    ("mood", "氛围"),
    ("artist", "画风"),
    ("unknown", "未归类"),
]
UNKNOWN = "unknown"
# 改过名的内置段的旧中文名：旧的 txt / xlsx 里仍写这个节名，导入时照认
LEGACY_BUCKET_NAMES = {"人物": "character"}
# 置位后由 importer.run_pending_reclassify 按新映射重算机器来源的词，算完清掉
RECLASSIFY_FLAG = "reclassify_pending"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS buckets (
  id      INTEGER PRIMARY KEY,
  key     TEXT    NOT NULL UNIQUE,
  name    TEXT    NOT NULL,
  ord     INTEGER NOT NULL,
  joins   INTEGER NOT NULL DEFAULT 1,
  builtin INTEGER NOT NULL DEFAULT 0
);

-- bucket_id 永不为 NULL，未归类指向 unknown 那一行
-- source: manual 手工 / file 文件分节 / danbooru 参考表 / rule 规则表
CREATE TABLE IF NOT EXISTS tags (
  id        INTEGER PRIMARY KEY,
  word      TEXT    NOT NULL UNIQUE,
  bucket_id INTEGER NOT NULL REFERENCES buckets(id),
  zh        TEXT,
  note      TEXT,
  blocked   INTEGER NOT NULL DEFAULT 0,
  source    TEXT    NOT NULL DEFAULT 'manual',
  created   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tags_bucket ON tags(bucket_id);

-- 只放手工合并的同义词，alias 存规范化后的写法
CREATE TABLE IF NOT EXISTS tag_aliases (
  alias  TEXT PRIMARY KEY,
  tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE
);

-- 每次导入对一个词给出的归类意见，用来筛分歧
-- source: file / danbooru / rule；origin: file 时是来源文件路径，其余为空串
CREATE TABLE IF NOT EXISTS tag_opinions (
  tag_id     INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  source     TEXT    NOT NULL,
  origin     TEXT    NOT NULL DEFAULT '',
  bucket_key TEXT    NOT NULL,
  seen       TEXT    NOT NULL,
  PRIMARY KEY (tag_id, source, origin)
);

-- danbooru_tags.xlsx 参考表；bucket_key 为按大类小类映射后的段 key，没意见则为 NULL
CREATE TABLE IF NOT EXISTS ref_tags (
  word        TEXT PRIMARY KEY,
  zh          TEXT,
  category    TEXT,
  subcategory TEXT,
  bucket_key  TEXT
);

-- 操作日志，payload 是撤销所需的改动前后值（JSON）
CREATE TABLE IF NOT EXISTS op_log (
  id      INTEGER PRIMARY KEY,
  ts      TEXT    NOT NULL,
  kind    TEXT    NOT NULL,
  summary TEXT    NOT NULL,
  payload TEXT    NOT NULL,
  undone  INTEGER NOT NULL DEFAULT 0
);

-- path：从 txt/xlsx 导入的是占位值 import:<set_name>/<stem>；反推过、扫描过的是真实绝对路径
-- origin：import 文件导入 / tagged 管理器内反推 / scan 扫图库登记、还没有提示词
-- size + mtime：扫描时判断文件没动过；missing：上次扫描时文件已不在
-- touched：最后一次写入标签的时间；edited：最后一次单图手工修改的时间
-- dir：图片所在目录相对图库根的路径，/ 分隔，根目录下的图为 ''；图库外的图、仅导入记录、空白套图为 NULL
-- set_name：图片所在目录的名字（套图）；仅导入记录和空白套图是导入时 / 建套图时给的名字
-- zone / category / char_name：schema 8 起废弃（原来的 区 / 分级 / 角色 固定层级），不再读写
CREATE TABLE IF NOT EXISTS images (
  id        INTEGER PRIMARY KEY,
  path      TEXT NOT NULL UNIQUE,
  stem      TEXT,
  sha1      TEXT,
  w         INTEGER,
  h         INTEGER,
  char_name TEXT,
  set_name  TEXT,
  origin    TEXT NOT NULL DEFAULT 'gen',
  model     TEXT,
  seed      TEXT,
  rating    INTEGER NOT NULL DEFAULT 0,
  starred   INTEGER NOT NULL DEFAULT 0,
  added     TEXT NOT NULL,
  tagger    TEXT,
  tagged    TEXT,
  size      INTEGER,
  mtime     REAL,
  missing   INTEGER NOT NULL DEFAULT 0,
  touched   TEXT,
  edited    TEXT,
  category  TEXT,
  zone      TEXT,
  dir       TEXT
);
CREATE INDEX IF NOT EXISTS idx_images_char ON images(char_name, set_name);
CREATE INDEX IF NOT EXISTS idx_images_stem ON images(stem);
CREATE INDEX IF NOT EXISTS idx_images_sha1 ON images(sha1);

CREATE TABLE IF NOT EXISTS image_tags (
  image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
  tag_id   INTEGER NOT NULL REFERENCES tags(id)   ON DELETE CASCADE,
  ord      INTEGER NOT NULL DEFAULT 0,
  score    REAL,
  PRIMARY KEY (image_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_it_tag ON image_tags(tag_id);

-- 主题（kind = 'theme'）。rule：{"all": [...], "any": [...], "none": [...]} 的 JSON，存主词；NULL = 纯手工
CREATE TABLE IF NOT EXISTS collections (
  id    INTEGER PRIMARY KEY,
  name  TEXT NOT NULL,
  kind  TEXT NOT NULL,
  rule  TEXT,
  cover INTEGER REFERENCES images(id),
  note  TEXT,
  UNIQUE (name, kind)
);

-- 主题成员：excluded 0 = 手动加入，1 = 手动排除（规则命中也不算）
CREATE TABLE IF NOT EXISTS image_collections (
  image_id      INTEGER NOT NULL REFERENCES images(id)      ON DELETE CASCADE,
  collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
  excluded      INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (image_id, collection_id)
);

-- 图片关联：src 是提示词来源的图，gen 是用它生成的图。多对多、有方向，同一对图只关联一次（不建反向的）
CREATE TABLE IF NOT EXISTS image_links (
  src_id  INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
  gen_id  INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
  created TEXT    NOT NULL,
  PRIMARY KEY (src_id, gen_id)
);
CREATE INDEX IF NOT EXISTS idx_links_gen ON image_links(gen_id);

-- 预设：同一段里不能重名，不同段可以同名（同名的算一个预设组）；content 是 ", " 分隔的词
CREATE TABLE IF NOT EXISTS presets (
  id        INTEGER PRIMARY KEY,
  name      TEXT    NOT NULL,
  bucket_id INTEGER NOT NULL REFERENCES buckets(id),
  content   TEXT    NOT NULL,
  lora      TEXT,
  note      TEXT,
  starred   INTEGER NOT NULL DEFAULT 0,
  UNIQUE (name, bucket_id)
);
"""


# 旧库补列：(表, 列, 列定义)。CREATE TABLE IF NOT EXISTS 不会给已有的表加列
MIGRATIONS = [
    ("images", "tagger", "TEXT"),        # schema 2：反推模型和参数
    ("images", "tagged", "TEXT"),        # schema 2：反推时间
    ("image_tags", "score", "REAL"),     # schema 2：反推置信度，文件导入的为空
    ("images", "size", "INTEGER"),       # schema 3：文件字节数
    ("images", "mtime", "REAL"),         # schema 3：文件修改时间
    ("images", "missing", "INTEGER NOT NULL DEFAULT 0"),  # schema 3：文件已不在
    ("images", "touched", "TEXT"),       # schema 3：最后一次写入标签的时间
    ("images", "edited", "TEXT"),        # schema 3：最后一次单图手工修改的时间
    ("images", "category", "TEXT"),      # schema 4：分级
    ("images", "zone", "TEXT"),          # schema 6：区
    ("image_collections", "excluded", "INTEGER NOT NULL DEFAULT 0"),  # schema 7：主题手动排除
    ("images", "dir", "TEXT"),           # schema 8：所在目录相对图库根的路径，不限层级
]
# 依赖补出来的列的索引，补列之后再建（写在 SCHEMA 里旧库会因为列还不存在而失败）
POST_MIGRATION_SQL = """
DROP INDEX IF EXISTS idx_images_category;
DROP INDEX IF EXISTS idx_images_zone;
CREATE INDEX IF NOT EXISTS idx_images_dir ON images(dir);
CREATE INDEX IF NOT EXISTS idx_ic_collection ON image_collections(collection_id, excluded);
"""


def now():
    return datetime.now().isoformat(timespec="seconds")


def connect(path):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _old_bucket_version(conn):
    """已有的库返回它的 schema 版本；新库（还没有段）返回 None。"""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'buckets'").fetchone():
        return None
    if not conn.execute("SELECT 1 FROM buckets LIMIT 1").fetchone():
        return None
    r = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    return int(r[0]) if r else 0


def _backup(conn, old_version):
    """升级前整库备份到 <库文件>.v<旧版本>.bak；内存库不备份。失败直接抛出，不往下迁移。"""
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    if not path:
        return None
    dst = "%s.v%d.bak" % (path, old_version)
    if os.path.exists(dst):
        dst = "%s.v%d.%s.bak" % (path, old_version, datetime.now().strftime("%Y%m%d%H%M%S"))
    conn.execute("VACUUM INTO ?", (dst,))
    return dst


def _upgrade_buckets_v5(conn):
    """七段 → 十段：人物改名角色、补三个新段、按新顺序重排；词的归属留给 importer 重算。"""
    conn.execute("UPDATE buckets SET name = '角色' WHERE key = 'character' AND name = '人物'")
    names = {r["name"].lower() for r in conn.execute("SELECT name FROM buckets")}
    for key, name in BUILTIN_BUCKETS:
        if conn.execute("SELECT 1 FROM buckets WHERE key = ?", (key,)).fetchone():
            continue
        if name.lower() in names:
            name += "（内置）"              # 用户自己建过同名段，避免 txt 分节解析时撞名
        conn.execute("INSERT INTO buckets (key, name, ord, joins, builtin) VALUES (?, ?, 0, 1, 1)",
                     (key, name))
    # 内置段按新顺序在前，用户自己加的段保持相对顺序，统一插在未归类前
    builtin = [k for k, _ in BUILTIN_BUCKETS if k != UNKNOWN]
    have = [r["key"] for r in conn.execute("SELECT key FROM buckets ORDER BY ord, id")]
    custom = [k for k in have if k not in builtin and k != UNKNOWN]
    order = [k for k in builtin if k in have] + custom + [UNKNOWN]
    for i, k in enumerate(order):
        conn.execute("UPDATE buckets SET ord = ? WHERE key = ?", (i, k))
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, '1')", (RECLASSIFY_FLAG,))


def _fill_dir_v8(conn):
    """
    schema 8：按旧的 区 / 分级 换算 dir。路径里找最后一处连续的「区/分级」两段，dir 从区开始到图片所在目录为止
    （收件箱的图就是 区/分级/未反推[/子目录]）。找不到的留空，下次扫描时补上。
    """
    rows = conn.execute("SELECT id, path, zone, category FROM images "
                        "WHERE zone IS NOT NULL AND category IS NOT NULL AND path NOT LIKE 'import:%'").fetchall()
    for r in rows:
        parts = [p for p in re.split(r"[\\/]", r["path"]) if p][:-1]
        for i in range(len(parts) - 2, -1, -1):
            if parts[i] == r["zone"] and parts[i + 1] == r["category"]:
                conn.execute("UPDATE images SET dir = ? WHERE id = ?", ("/".join(parts[i:]), r["id"]))
                break


def init_db(conn):
    """建表并预置十个内置段；可重复执行。旧库先备份再升级（七段的还要升级成十段）。"""
    old = _old_bucket_version(conn)
    if old is not None and old < SCHEMA_VERSION:
        _backup(conn, old)
    upgrading = old is not None and old < 5
    with conn:
        conn.executescript(SCHEMA)
        for table, col, ddl in MIGRATIONS:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)}
            if col not in cols:
                conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, ddl))
                if col == "touched":
                    # schema 3 之前的图都有标签，按反推时间或入库时间补上，排序才不会把它们全压到最后
                    conn.execute("UPDATE images SET touched = COALESCE(tagged, added)")
        conn.executescript(POST_MIGRATION_SQL)
        if upgrading:
            _upgrade_buckets_v5(conn)
        if old is not None and old < 8:
            _fill_dir_v8(conn)
        for i, (key, name) in enumerate(BUILTIN_BUCKETS):
            conn.execute(
                "INSERT OR IGNORE INTO buckets (key, name, ord, joins, builtin) "
                "VALUES (?, ?, ?, 1, 1)", (key, name, i))
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                     (str(SCHEMA_VERSION),))


def log_op(conn, kind, summary, payload):
    """记一条可撤销的操作。payload: {"tags": [{"id", "before": {...}, "after": {...}}], ...}"""
    conn.execute("INSERT INTO op_log (ts, kind, summary, payload) VALUES (?, ?, ?, ?)",
                 (now(), kind, summary, json.dumps(payload, ensure_ascii=False)))


def bucket_ids(conn):
    """{key: id}"""
    return {r["key"]: r["id"] for r in conn.execute("SELECT id, key FROM buckets")}


def paginate(conn, sql, params=(), page=1, size=60, order_by=None, max_size=500):
    """统一分页，返回 {items, total, page, size}。sql 是不带 ORDER/LIMIT 的 SELECT。"""
    page, size = _page_args(page, size, max_size)
    total = conn.execute("SELECT COUNT(*) FROM (%s)" % sql, params).fetchone()[0]
    q = sql
    if order_by:
        q += " ORDER BY " + order_by
    q += " LIMIT ? OFFSET ?"
    rows = conn.execute(q, tuple(params) + (size, (page - 1) * size)).fetchall()
    return {"items": [dict(r) for r in rows], "total": total, "page": page, "size": size}


def _page_args(page, size, max_size):
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    try:
        size = min(max_size, max(1, int(size)))
    except (TypeError, ValueError):
        size = 60
    return page, size


def paginate_list(items, page=1, size=60, max_size=500):
    """已经算好的列表按同样的结构分页（组合器预览用）。"""
    page, size = _page_args(page, size, max_size)
    return {"items": items[(page - 1) * size:page * size], "total": len(items), "page": page, "size": size}

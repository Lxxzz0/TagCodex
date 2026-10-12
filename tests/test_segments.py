# -*- coding: utf-8 -*-
"""十段：danbooru 大类小类映射、七段旧库升级（备份、改名、重排、重算机器归类）、规则表旧段不算分歧。临时库，不依赖真实数据。"""
import glob
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db        # noqa: E402
import importer  # noqa: E402
import tagops    # noqa: E402

OLD_BUCKETS = [("artist", "画风"), ("character", "人物"), ("outfit", "服饰"), ("pose", "姿势"),
               ("camera", "镜头"), ("scene", "环境"), ("unknown", "未归类")]


class MappingTest(unittest.TestCase):
    def test_danbooru_mapping(self):
        m = importer.DANBOORU_TO_KEY
        cases = {
            ("人物", "对象"): "character",
            ("人物", "身份（职业）"): "character",
            ("人物", "眼睛"): "appearance",
            ("人物", "面部"): "appearance",
            ("人物", "嘴巴"): "expression",
            ("表情动作", "颜文字"): "expression",
            ("表情动作", "手部拿着某物"): "pose",
            ("镜头", "人物视觉朝向"): "pose",
            ("镜头", "人物构图"): "camera",
            ("服饰", "鞋子"): "outfit",
            ("画面", "颜色"): "artist",
            ("画面", "光照"): "mood",
            ("环境", "氛围"): "mood",
            ("环境", "天气"): "scene",
            ("物品", "食物"): "scene",
        }
        for pair, key in cases.items():
            self.assertEqual(m.get(pair), key, pair)
        for pair in [("画面", "画面量质"), ("二次元角色", ""), ("艺术家", ""), ("艺术家", "设定"),
                     ("物品", "动物"), ("无法分类", "")]:
            self.assertIsNone(m.get(pair), "没意见：%s" % (pair,))
        builtin = {k for k, _ in db.BUILTIN_BUCKETS}
        self.assertLessEqual(set(m.values()), builtin, "映射只落到内置段")


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.saved = (dict(importer.RULES), dict(importer.CHARS))
        importer.RULES.update(module=None, error=None)
        importer.CHARS.update(words=frozenset(), path=None, error=None)
        self.tmp = tempfile.mkdtemp(prefix="tagcodex_seg_")
        self.path = os.path.join(self.tmp, "t.db")

    def tearDown(self):
        importer.RULES.clear(); importer.RULES.update(self.saved[0])
        importer.CHARS.clear(); importer.CHARS.update(self.saved[1])
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------- 工具 ----------
    def make_v4(self, character_name="人物", custom=("extra", "道具")):
        """造一个七段的 schema 4 旧库：自定义段在未归类前，几个机器词和一个手工词。"""
        conn = db.connect(self.path)
        conn.executescript(db.SCHEMA)
        with conn:
            order = OLD_BUCKETS[:-1] + [custom] + OLD_BUCKETS[-1:]
            for i, (key, name) in enumerate(order):
                conn.execute("INSERT INTO buckets (key, name, ord, joins, builtin) VALUES (?, ?, ?, 1, ?)",
                             (key, character_name if key == "character" else name, i, int(key != custom[0])))
            conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '4')")
            ids = db.bucket_ids(conn)
            # 词, 旧段, 来源, danbooru 大类, 小类
            for word, key, src, cat, sub in [
                ("long hair", "character", "danbooru", "人物", "头发发型"),
                ("smile", "pose", "danbooru", "表情动作", "笑"),
                ("glowing", "scene", "danbooru", "画面", "光照"),
                ("highres", "scene", "danbooru", "画面", "画面量质"),
                ("blue eyes", "character", "manual", "人物", "眼睛"),
                ("tears", "pose", "file", "表情动作", "哭"),
            ]:
                conn.execute("INSERT INTO ref_tags (word, category, subcategory, bucket_key) VALUES (?, ?, ?, ?)",
                             (word, cat, sub, key))
                conn.execute("INSERT INTO tags (word, bucket_id, source, created) VALUES (?, ?, ?, '2026-10-01')",
                             (word, ids[key], src))
        conn.close()

    def upgrade(self):
        conn = db.connect(self.path)
        db.init_db(conn)
        n = importer.run_pending_reclassify(conn)
        return conn, n

    def tag(self, conn, word):
        return tuple(conn.execute("SELECT b.key, t.source FROM tags t JOIN buckets b ON b.id = t.bucket_id "
                                  "WHERE t.word = ?", (word,)).fetchone())

    def backups(self):
        return glob.glob(self.path + ".v4*.bak")

    # ---------- 用例 ----------
    def test_upgrade_v4(self):
        self.make_v4()
        conn, n = self.upgrade()
        try:
            self.assertEqual(len(self.backups()), 1, "升级前备份")
            rows = conn.execute("SELECT key, name FROM buckets ORDER BY ord").fetchall()
            self.assertEqual([r["key"] for r in rows],
                             ["character", "appearance", "outfit", "expression", "pose", "camera",
                              "scene", "mood", "artist", "extra", "unknown"], "自定义段保持在未归类前")
            self.assertEqual(dict((r["key"], r["name"]) for r in rows)["character"], "角色")

            self.assertEqual(self.tag(conn, "long hair"), ("appearance", "danbooru"))
            self.assertEqual(self.tag(conn, "smile"), ("expression", "danbooru"))
            self.assertEqual(self.tag(conn, "glowing"), ("mood", "danbooru"))
            self.assertEqual(self.tag(conn, "highres"), ("unknown", "rule"), "质量词没意见，进未归类")
            self.assertEqual(self.tag(conn, "blue eyes"), ("character", "manual"), "手工的不动")
            self.assertEqual(self.tag(conn, "tears"), ("pose", "file"), "文件来源的不动")
            self.assertEqual(conn.execute("SELECT bucket_key FROM ref_tags WHERE word = 'smile'").fetchone()[0],
                             "expression", "参考表按新映射重算")

            self.assertEqual(n, 4)
            ops = tagops.list_ops(conn)
            self.assertEqual([o["kind"] for o in ops], ["reclassify"], "只记一条")
            self.assertIn("4 个词改段", ops[0]["summary"])
            self.assertIsNone(conn.execute("SELECT 1 FROM meta WHERE key = ?", (db.RECLASSIFY_FLAG,)).fetchone())

            tagops.undo_last(conn)
            self.assertEqual(self.tag(conn, "long hair"), ("character", "danbooru"), "整体可撤销")
            self.assertEqual(self.tag(conn, "highres"), ("scene", "danbooru"))
        finally:
            conn.close()

        conn, n = self.upgrade()
        try:
            self.assertIsNone(n, "已经是新版本，不再重算")
            self.assertEqual(len(self.backups()), 1, "不再备份")
        finally:
            conn.close()

    def test_upgrade_keeps_user_names(self):
        self.make_v4(character_name="角色们", custom=("looks", "外貌"))
        conn, _ = self.upgrade()
        try:
            names = {r["key"]: r["name"] for r in conn.execute("SELECT key, name FROM buckets")}
            self.assertEqual(names["character"], "角色们", "用户改过名的不动")
            self.assertEqual(names["looks"], "外貌")
            self.assertEqual(names["appearance"], "外貌（内置）", "和用户的段撞名时加后缀")
        finally:
            conn.close()

    def test_fresh_db_has_no_backup_or_reclassify(self):
        conn, n = self.upgrade()
        try:
            self.assertIsNone(n)
            self.assertEqual(self.backups(), [])
            self.assertEqual([r[0] for r in conn.execute("SELECT key FROM buckets ORDER BY ord")],
                             [k for k, _ in db.BUILTIN_BUCKETS])
        finally:
            conn.close()

    def test_legacy_header_name(self):
        conn, _ = self.upgrade()
        try:
            self.assertEqual(importer.header_lookup(conn)["人物"], "character", "旧 txt 的「人物」节照认")
        finally:
            conn.close()


class RuleCoverTest(unittest.TestCase):
    """规则表只认旧六段：它说 character、danbooru 说 appearance 不算分歧；说 pose、danbooru 说 appearance 算。"""

    def test_machine_conflict(self):
        tmp = tempfile.mkdtemp(prefix="tagcodex_seg_")
        conn = db.connect(os.path.join(tmp, "t.db"))
        try:
            db.init_db(conn)
            ids = db.bucket_ids(conn)
            with conn:
                for word, rule in [("long hair", "character"), ("blush", "pose")]:
                    tid = conn.execute("INSERT INTO tags (word, bucket_id, source, created) "
                                       "VALUES (?, ?, 'danbooru', '2026-10-11')", (word, ids["appearance"])).lastrowid
                    importer.record_machine_opinions(conn, tid, {"danbooru": "appearance", "rule": rule}, "x")
            words = [r[0] for r in conn.execute("SELECT word FROM tags t WHERE " + tagops.MACHINE_CONFLICT)]
            self.assertEqual(words, ["blush"])
        finally:
            conn.close()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

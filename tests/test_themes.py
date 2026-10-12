# -*- coding: utf-8 -*-
"""主题测试：规则三栏、别名、黑名单词、手动加入 / 排除 / 恢复、和目录筛选叠加、撤销、旧库补列。临时库。"""
import glob
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db        # noqa: E402
import gallery   # noqa: E402
import importer  # noqa: E402
import tagops    # noqa: E402
import themes    # noqa: E402


class ThemeTest(unittest.TestCase):
    def setUp(self):
        self.saved_rules = dict(importer.RULES)
        importer.RULES.update(module=None, error=None)
        self.tmp = tempfile.mkdtemp(prefix="tagcodex_themes_")
        self.conn = db.connect(os.path.join(self.tmp, "t.db"))
        db.init_db(self.conn)
        self.a = self.load("套1", "a", ["1girl", "bikini", "beach"])
        self.b = self.load("套2", "b", ["1girl", "bikini", "pool"])
        self.c = self.load("套3", "c", ["1boy", "bikini", "beach"])
        self.d = self.load("套4", "d", ["1girl", "school uniform"])
        with self.conn:
            self.conn.execute("UPDATE images SET dir = '生成/套' WHERE id = ?", (self.b,))

    def tearDown(self):
        self.conn.close()
        importer.RULES.clear()
        importer.RULES.update(self.saved_rules)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------- 工具 ----------
    def load(self, set_name, stem, words):
        with self.conn:
            importer._import_records(self.conn, [(stem, [(None, None, words)])], set_name, "x.txt", False,
                                     importer._new_report(False))
        return self.conn.execute("SELECT id FROM images WHERE stem = ?", (stem,)).fetchone()[0]

    def tid(self, word):
        return self.conn.execute("SELECT id FROM tags WHERE word = ?", (word,)).fetchone()[0]

    def ids(self, **args):
        return sorted(it["id"] for it in gallery.list_images(self.conn, args)["items"])

    def theme(self, rule=None, name="泳装"):
        return themes.create(self.conn, {"name": name, "rule": rule})["id"]

    def members(self, theme_id, view=None):
        return self.ids(theme=theme_id, theme_view=view)

    # ---------- 规则 ----------
    def test_rule_columns(self):
        t = self.theme({"all": "bikini", "any": "beach, pool", "none": "1boy"})
        self.assertEqual(self.members(t), sorted([self.a, self.b]))
        t2 = self.theme({"all": ["1girl", "bikini"]}, name="女孩泳装")
        self.assertEqual(self.members(t2), sorted([self.a, self.b]))

    def test_rule_normalized_and_alias(self):
        tagops.merge_alias(self.conn, self.tid("beach"), [self.tid("pool")])
        t = self.theme({"all": "Bikini", "any": "pool", "none": "1boy"})
        self.assertEqual(themes.get_theme(self.conn, t)["rule"],
                         '{"all": ["bikini"], "any": ["beach"], "none": ["1boy"]}', "存规范化后的主词")
        self.assertEqual(self.members(t), sorted([self.a, self.b]))

    def test_alias_merged_after_create(self):
        t = self.theme({"any": "pool"})
        self.assertEqual(self.members(t), [self.b])
        tagops.merge_alias(self.conn, self.tid("beach"), [self.tid("pool")])
        self.assertEqual(self.members(t), sorted([self.a, self.b, self.c]), "查询时再换主词")

    def test_blocked_word_not_matched(self):
        t = self.theme({"any": "pool"})
        tagops.block(self.conn, [self.tid("pool")])
        self.assertEqual(self.members(t), [])

    def test_rule_validation(self):
        with self.assertRaises(tagops.OpError):
            self.theme({"none": "1boy"})
        r = themes.create(self.conn, {"name": "x", "rule": {"any": "nothing here, beach"}})
        self.assertEqual(r["unknown_words"], ["nothing here"])
        t = self.theme({"all": " , "}, name="空规则")
        self.assertIsNone(themes.get_theme(self.conn, t)["rule"], "三栏全空 = 纯手工")
        self.assertEqual(self.members(t), [])

    def test_name_rules(self):
        self.theme()
        with self.assertRaises(tagops.OpError):
            self.theme()
        with self.assertRaises(tagops.OpError):
            self.theme(name="  ")

    def test_combine_with_folder_filter(self):
        t = self.theme({"all": "bikini"})
        self.assertEqual(self.ids(theme=t, dir="生成"), [self.b])

    # ---------- 成员 ----------
    def test_manual_add_remove_restore(self):
        t = self.theme({"any": "beach"})
        self.assertEqual(self.members(t), sorted([self.a, self.c]))
        self.assertEqual(themes.members(self.conn, t, {"add": [self.d]})["changed"], 1)
        self.assertEqual(self.members(t), sorted([self.a, self.c, self.d]))
        # 移出：手动加入的直接删，规则命中的记排除
        self.assertEqual(themes.members(self.conn, t, {"remove": [self.d, self.c]})["changed"], 2)
        self.assertEqual(self.members(t), [self.a])
        self.assertEqual(self.members(t, "excluded"), [self.c])
        # 排除的图之后规则变了也不回来
        themes.update(self.conn, t, {"rule": {"all": "bikini"}})
        self.assertEqual(self.members(t), sorted([self.a, self.b]))
        self.assertEqual(themes.members(self.conn, t, {"restore": [self.c]})["changed"], 1)
        self.assertEqual(self.members(t), sorted([self.a, self.b, self.c]))
        self.assertEqual(themes.members(self.conn, t, {"restore": [self.c]})["changed"], 0, "没变化")

    def test_of_image_and_detail(self):
        t1 = self.theme({"any": "beach"}, name="海边")
        t2 = self.theme(name="手选")
        themes.members(self.conn, t2, {"add": [self.a]})
        self.assertEqual([(x["name"], x["manual"]) for x in themes.of_image(self.conn, self.a)],
                         [("手选", True), ("海边", False)])
        themes.members(self.conn, t1, {"remove": [self.a]})
        self.assertEqual([x["id"] for x in gallery.detail(self.conn, self.a, {})["themes"]], [t2], "排除的不算")
        self.assertEqual(themes.of_image(self.conn, self.d), [])

    def test_add_excluded_becomes_member(self):
        t = self.theme({"any": "beach"})
        themes.members(self.conn, t, {"remove": [self.a]})
        themes.members(self.conn, t, {"add": [self.a]})
        self.assertEqual(self.members(t), sorted([self.a, self.c]))
        self.assertEqual(self.members(t, "excluded"), [])

    def test_member_errors(self):
        t = self.theme()
        with self.assertRaises(tagops.OpError):
            themes.members(self.conn, t, {"add": [99999]})
        with self.assertRaises(tagops.OpError):
            themes.members(self.conn, t, {"add": [self.a], "remove": [self.b]})
        with self.assertRaises(tagops.OpError):
            themes.members(self.conn, 99999, {"add": [self.a]})

    def test_list_and_preview(self):
        t = self.theme({"any": "beach"})
        themes.members(self.conn, t, {"remove": [self.c]})
        [row] = themes.list_themes(self.conn)
        self.assertEqual((row["count"], row["excluded"], row["rule"]["any"]), (1, 1, ["beach"]))
        self.assertIsNone(row["cover"], "仅导入记录没有缩略图")
        self.assertEqual(themes.preview(self.conn, {"rule": {"any": "bikini"}})["count"], 3)
        self.assertEqual(themes.preview(self.conn, {"rule": {"any": "bikini"}, "theme_id": t})["count"], 2,
                         "带主题时扣掉排除的图")

    # ---------- 撤销 ----------
    def test_undo(self):
        t = self.theme({"any": "beach"})
        themes.members(self.conn, t, {"add": [self.d]})
        themes.update(self.conn, t, {"name": "海边", "note": "备注"})
        themes.delete(self.conn, t)
        self.assertEqual(themes.list_themes(self.conn), [])
        tagops.undo_last(self.conn)                                  # 撤销删除
        self.assertEqual(themes.get_theme(self.conn, t)["name"], "海边")
        self.assertEqual(self.members(t), sorted([self.a, self.c, self.d]), "成员一起恢复")
        tagops.undo_last(self.conn)                                  # 撤销改名
        self.assertEqual(themes.get_theme(self.conn, t)["name"], "泳装")
        tagops.undo_last(self.conn)                                  # 撤销加入
        self.assertEqual(self.members(t), sorted([self.a, self.c]))
        tagops.undo_last(self.conn)                                  # 撤销新建
        with self.assertRaises(tagops.OpError):
            themes.get_theme(self.conn, t)

    def test_no_change_no_log(self):
        t = self.theme()
        n = self.conn.execute("SELECT COUNT(*) FROM op_log").fetchone()[0]
        self.assertEqual(themes.update(self.conn, t, {"name": "泳装"})["changed"], 0)
        self.assertEqual(themes.members(self.conn, t, {"remove": [self.a]})["changed"], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM op_log").fetchone()[0], n)


    # ---------- 词频 ----------
    def test_stats(self):
        tagops.assign(self.conn, [self.tid("bikini")], "outfit")
        tagops.block(self.conn, [self.tid("pool")])
        t = self.theme({"all": "bikini"})
        st = gallery.stats(self.conn, {"theme": t, "size": 1})
        self.assertEqual(st["total"], 3, "统计整个筛选结果，不受分页影响")
        segs = {s["key"]: s for s in st["segments"]}
        self.assertEqual(list(segs)[-1], "unknown", "段顺序跟词典")
        self.assertEqual([(x["word"], x["count"], x["ratio"]) for x in segs["outfit"]["tags"]],
                         [("bikini", 3, 1.0)])
        unk = [(x["word"], x["count"], x["blocked"]) for x in segs["unknown"]["tags"]]
        self.assertEqual(unk[0], ("1girl", 2, False), "按张数从多到少")
        self.assertIn(("pool", 1, True), unk, "黑名单词带标记返回")
        self.assertEqual(gallery.stats(self.conn, {"theme": t, "dir": "没有"})["total"], 0)


class UpgradeTest(unittest.TestCase):
    def test_upgrade_v6_adds_excluded(self):
        tmp = tempfile.mkdtemp(prefix="tagcodex_themes_up_")
        try:
            path = os.path.join(tmp, "t.db")
            conn = db.connect(path)
            db.init_db(conn)
            with conn:                   # 退回 schema 6 的样子：image_collections 没有 excluded
                conn.execute("DROP TABLE image_collections")
                conn.execute("CREATE TABLE image_collections (image_id INTEGER NOT NULL, "
                             "collection_id INTEGER NOT NULL, PRIMARY KEY (image_id, collection_id))")
                conn.execute("UPDATE meta SET value = '6' WHERE key = 'schema_version'")
            conn.close()
            conn = db.connect(path)
            db.init_db(conn)
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(image_collections)")}
            self.assertIn("excluded", cols)
            self.assertEqual(len(glob.glob(path + ".v6*.bak")), 1, "升级前备份")
            conn.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

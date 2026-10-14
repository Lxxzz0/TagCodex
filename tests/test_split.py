# -*- coding: utf-8 -*-
"""粘贴拆分：按词典把一段提示词拆到各段（已归类、别名、未归类、分歧、词典没有、黑名单、LoRA、去重），多段一次添加与撤销。临时库。"""
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


class SplitTest(unittest.TestCase):
    def setUp(self):
        self.saved_rules = dict(importer.RULES)
        importer.RULES.update(module=None, error=None)
        self.tmp = tempfile.mkdtemp(prefix="tagcodex_split_")
        self.conn = db.connect(os.path.join(self.tmp, "t.db"))
        db.init_db(self.conn)
        tagops.add_words(self.conn, "character", "1girl, solo")
        tagops.add_words(self.conn, "outfit", "bikini, watermark")
        tagops.add_words(self.conn, "scene", "beach")
        tagops.block(self.conn, [self.tid("watermark")])
        tagops.merge_alias(self.conn, self.tid("bikini"), [self.add_unknown("swimsuit bikini")])
        self.add_unknown("smile")
        # 有分歧：两个文件意见不同，词现在在服饰
        cid = self.add_unknown("hat")
        with self.conn:
            self.conn.execute("UPDATE tags SET bucket_id = (SELECT id FROM buckets WHERE key = 'outfit'), source = 'file' "
                              "WHERE id = ?", (cid,))
            for origin, key in (("a.txt", "outfit"), ("b.txt", "appearance")):
                self.conn.execute("INSERT INTO tag_opinions (tag_id, source, origin, bucket_key, seen) "
                                  "VALUES (?, 'file', ?, ?, '')", (cid, origin, key))

    def tearDown(self):
        self.conn.close()
        importer.RULES.clear()
        importer.RULES.update(self.saved_rules)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def tid(self, word):
        return self.conn.execute("SELECT id FROM tags WHERE word = ?", (word,)).fetchone()[0]

    def add_unknown(self, word):
        with self.conn:
            return self.conn.execute("INSERT INTO tags (word, bucket_id, source, created) VALUES "
                                     "(?, (SELECT id FROM buckets WHERE key = 'unknown'), 'rule', '')", (word,)).lastrowid

    def bucket_of(self, word):
        return self.conn.execute("SELECT b.key FROM tags t JOIN buckets b ON b.id = t.bucket_id WHERE t.word = ?",
                                 (word,)).fetchone()[0]

    def test_split(self):
        r = tagops.split_text(self.conn, "beach, 1girl,\nSwimsuit Bikini, <lora:x:0.8>, 1Girl, watermark, smile, "
                                         "hat, new_word, , solo, bikini")
        self.assertEqual([(s["key"], s["name"], s["words"], s["blocked"], s["conflict"]) for s in r["segments"]], [
            ("character", "角色", ["1girl", "solo"], [], []),
            ("outfit", "服饰", ["Swimsuit Bikini", "watermark", "hat"], ["watermark"], ["hat"]),
            ("scene", "环境", ["beach"], [], []),
            ("unknown", "未归类", ["smile"], [], []),
        ], "按段顺序；写法原样；别名认到主词所在段并和主词去重；黑名单词照常放进段；"
           "有分歧的放进现在的段并标出；未归类的在未归类段")
        self.assertEqual(r["unknown"], [{"word": "new_word"}], "只有词典里没有的词")
        self.assertEqual(r["lora_skipped"], 1)
        self.assertEqual(tagops.split_text(self.conn, " , \n"), {"segments": [], "unknown": [], "lora_skipped": 0})

    def test_add_to_many_buckets_once(self):
        before = self.conn.execute("SELECT COUNT(*) FROM op_log").fetchone()[0]
        r = tagops.add_words(self.conn, None, None, [{"bucket": "expression", "text": "smile, new_word"},
                                                     {"bucket": "appearance", "text": "hat"}])
        self.assertEqual((r["created"], r["moved"]), (1, 2))
        self.assertEqual((self.bucket_of("smile"), self.bucket_of("new word"), self.bucket_of("hat")),
                         ("expression", "expression", "appearance"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM op_log").fetchone()[0], before + 1, "只记一条操作")
        self.assertIn("2 段", self.conn.execute("SELECT summary FROM op_log ORDER BY id DESC").fetchone()[0])
        tagops.undo_last(self.conn)
        self.assertEqual((self.bucket_of("smile"), self.bucket_of("hat")), ("unknown", "outfit"), "一次撤销全部还原")
        self.assertIsNone(self.conn.execute("SELECT 1 FROM tags WHERE word = 'new word'").fetchone())

    def test_add_same_word_two_buckets_fails(self):
        before = self.conn.execute("SELECT COUNT(*) FROM op_log").fetchone()[0]
        with self.assertRaises(tagops.OpError):
            tagops.add_words(self.conn, None, None, [{"bucket": "expression", "text": "smile"},
                                                     {"bucket": "pose", "text": "Smile"}])
        self.assertEqual(self.bucket_of("smile"), "unknown", "什么都不写")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM op_log").fetchone()[0], before)
        with self.assertRaises(tagops.OpError):
            tagops.add_words(self.conn, None, None, [])
        with self.assertRaises(tagops.OpError):
            tagops.add_words(self.conn, None, None, ["smile"])


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""空白套图测试：新建、序号接续、删条与撤销、改名、重名报错、写入组合结果、不能写图库里的图、和图库 / 组合器的配合。临时库。"""
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import composer  # noqa: E402
import db        # noqa: E402
import drafts    # noqa: E402
import gallery   # noqa: E402
import importer  # noqa: E402
import presets   # noqa: E402
import tagops    # noqa: E402


class DraftTest(unittest.TestCase):
    def setUp(self):
        self.saved_rules = dict(importer.RULES)
        importer.RULES.update(module=None, error=None)
        self.tmp = tempfile.mkdtemp(prefix="tagcodex_drafts_")
        self.conn = db.connect(os.path.join(self.tmp, "t.db"))
        db.init_db(self.conn)
        with self.conn:      # 一张图库里的图（仅导入记录），用来确认写入不碰它
            importer._import_records(self.conn, [("a", [(None, None, ["1girl", "beach"])])], "套1", "x.txt", False,
                                     importer._new_report(False))
        self.real = self.conn.execute("SELECT id FROM images WHERE stem = 'a'").fetchone()[0]
        tagops.assign(self.conn, [self.tid("1girl")], "character")

    def tearDown(self):
        self.conn.close()
        importer.RULES.clear()
        importer.RULES.update(self.saved_rules)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def tid(self, word):
        return self.conn.execute("SELECT id FROM tags WHERE word = ?", (word,)).fetchone()[0]

    def stems(self, name="泳装"):
        return [r[0] for r in self.conn.execute("SELECT stem FROM images WHERE set_name = ? ORDER BY stem", (name,))]

    def words(self, image_id):
        return [r[0] for r in self.conn.execute("SELECT t.word FROM image_tags y JOIN tags t ON t.id = y.tag_id "
                                                "WHERE y.image_id = ? ORDER BY y.ord", (image_id,))]

    def src(self, name="泳装"):
        return {"filter": {"scope": "draft", "set": name, "sort": "name"}}

    # ---------- 新建 / 加条 / 删条 / 改名 ----------
    def test_create_add_delete_undo(self):
        r = drafts.create(self.conn, {"name": " 泳装 ", "count": 3})
        self.assertEqual((r["name"], self.stems()), ("泳装", ["泳装_001", "泳装_002", "泳装_003"]))
        path = self.conn.execute("SELECT path, origin FROM images WHERE id = ?", (r["ids"][0],)).fetchone()
        self.assertEqual(tuple(path), ("import:泳装/泳装_001", "draft"))
        drafts.delete_items(self.conn, {"ids": [r["ids"][1]]})
        drafts.add_items(self.conn, {"name": "泳装", "count": 2})
        self.assertEqual(self.stems(), ["泳装_001", "泳装_003", "泳装_004", "泳装_005"], "中间的空号不补，接着最大号")
        self.assertEqual(drafts.list_drafts(self.conn), [{"name": "泳装", "count": 4, "filled": 0}])
        tagops.undo_last(self.conn)                       # 撤销加条
        tagops.undo_last(self.conn)                       # 撤销删条：原样回来
        self.assertEqual(self.stems(), ["泳装_001", "泳装_002", "泳装_003"])
        self.assertIn(r["ids"][1], [x[0] for x in self.conn.execute("SELECT id FROM images")])
        tagops.undo_last(self.conn)                       # 撤销新建
        self.assertEqual(self.stems(), [])

    def test_errors(self):
        drafts.create(self.conn, {"name": "泳装", "count": 1})
        for body in ({"name": "泳装", "count": 1}, {"name": "套1", "count": 1}, {"name": " ", "count": 1},
                     {"name": "a/b", "count": 1}, {"name": "x", "count": 0}, {"name": "x", "count": 201},
                     {"name": "x", "count": "多"}):
            with self.assertRaises(tagops.OpError, msg=body):
                drafts.create(self.conn, body)
        with self.assertRaises(tagops.OpError):
            drafts.add_items(self.conn, {"name": "套1", "count": 1})
        with self.assertRaises(tagops.OpError):
            drafts.delete_items(self.conn, {"ids": [self.real]})
        with self.assertRaises(tagops.OpError):
            drafts.rename(self.conn, {"name": "泳装", "new_name": "套1"})

    def test_rename_and_delete(self):
        ids = drafts.create(self.conn, {"name": "泳装", "count": 2})["ids"]
        gallery.edit_tags(self.conn, ids[0], {"add": "beach"})
        drafts.rename(self.conn, {"name": "泳装", "new_name": "海边"})
        self.assertEqual(self.stems("海边"), ["海边_001", "海边_002"])
        self.assertEqual(self.conn.execute("SELECT path FROM images WHERE id = ?", (ids[0],)).fetchone()[0],
                         "import:海边/海边_001")
        drafts.delete(self.conn, {"name": "海边"})
        self.assertEqual(self.stems("海边"), [])
        tagops.undo_last(self.conn)
        self.assertEqual(self.words(ids[0]), ["beach"], "删除整个套图后撤销，词也回来")
        tagops.undo_last(self.conn)
        self.assertEqual(self.stems(), ["泳装_001", "泳装_002"])

    # ---------- 图库 / 组合器 ----------
    def test_gallery_scope_and_composer(self):
        drafts.create(self.conn, {"name": "泳装", "count": 2})
        ids = lambda **a: [it["stem"] for it in gallery.list_images(self.conn, a)["items"]]
        self.assertEqual(sorted(ids(scope="draft")), ["泳装_001", "泳装_002"])
        self.assertEqual(ids(scope="placeholder"), ["a"], "仅导入记录不含空白套图")
        self.assertEqual(ids(state="untagged"), [], "空白的条不算未反推")
        t = gallery.tree(self.conn)
        self.assertEqual((t["draft"], t["placeholder"], t["untagged"]), (2, 1, 0))
        rows, skipped, _ = composer.compute(self.conn, self.src(), {"scene": {"mode": "replace", "text": "pool"}})
        self.assertEqual(([r["stem"] for r in rows], skipped), (["泳装_001", "泳装_002"], 0), "空白的条不跳过")
        self.assertEqual(rows[0]["prompt"], "pool")
        out = os.path.join(self.tmp, "out")
        composer.export(self.conn, {"gallery_root": os.path.join(self.tmp, "图库"), "thumbs_dir": self.tmp},
                        {"source": self.src(), "slots": {}, "name": "x", "dir": out, "txt": True, "xlsx": False})
        self.assertEqual(sorted(f for f in os.listdir(out) if f.endswith(".txt")), ["泳装_001.txt", "泳装_002.txt"])

    # ---------- 写入 ----------
    def test_apply(self):
        ids = drafts.create(self.conn, {"name": "泳装", "count": 2})["ids"]
        gallery.edit_tags(self.conn, ids[1], {"add": "smile"})
        slots = {"character": {"mode": "auto", "text": "<lora:girl:0.8>, 1girl, Solo_Pose"},
                 "pose": {"mode": "append", "text": "standing"}}
        r = drafts.apply(self.conn, {"source": self.src(), "slots": slots})
        self.assertEqual(r, {"count": 2, "lora": 2}, "手写的 LoRA 标记不写，返回跳过几个")
        self.assertEqual(self.words(ids[0]), ["1girl", "solo pose", "standing"])
        self.assertEqual(self.words(ids[1]), ["1girl", "solo pose", "standing", "smile"], "原来的词按段保留（smile 在未归类，排最后）")
        segs = {s["key"]: [t["word"] for t in s["tags"]] for s in gallery.segments(self.conn, [ids[0]])[ids[0]]}
        self.assertEqual((segs["character"], segs["pose"]), (["1girl", "solo pose"], ["standing"]),
                         "新词进它所在槽的段")
        self.assertEqual(self.conn.execute("SELECT source FROM tags WHERE word = 'standing'").fetchone()[0], "manual")
        tagops.undo_last(self.conn)
        self.assertEqual((self.words(ids[0]), self.words(ids[1])), ([], ["smile"]), "整批一次撤销")
        self.assertIsNone(self.conn.execute("SELECT 1 FROM tags WHERE word = 'standing'").fetchone(), "新建的词也删掉")
        self.assertEqual(json.loads(self.conn.execute("SELECT payload FROM op_log ORDER BY id DESC LIMIT 1")
                                    .fetchone()[0])["op"], "apply")

    def test_apply_only_selected(self):
        ids = drafts.create(self.conn, {"name": "泳装", "count": 3})["ids"]
        r = drafts.apply(self.conn, {"source": {"ids": [ids[0], ids[2]]},
                                     "slots": {"pose": {"mode": "replace", "text": "sitting"}}})
        self.assertEqual(r["count"], 2)
        self.assertEqual([self.words(i) for i in ids], [["sitting"], [], ["sitting"]], "没选的条不动")

    def test_apply_refuses_real_images(self):
        drafts.create(self.conn, {"name": "泳装", "count": 1})
        with self.assertRaises(tagops.OpError):
            drafts.apply(self.conn, {"source": {"ids": [self.real]}, "slots": {}})
        with self.assertRaises(tagops.OpError):
            drafts.apply(self.conn, {"source": {"filter": {"scope": "placeholder"}}, "slots": {}})
        self.assertEqual(self.words(self.real), ["1girl", "beach"])


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""预设测试：内容规范化、同段重名与覆盖、不同段同名、修改、删除、撤销、存预设组与合并、有预设的段不能删。临时库。"""
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db        # noqa: E402
import presets   # noqa: E402
import tagops    # noqa: E402


class PresetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tagcodex_presets_")
        self.conn = db.connect(os.path.join(self.tmp, "t.db"))
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, name="泳装", bucket="outfit", content="bikini, sarong", **kw):
        return presets.create(self.conn, dict(name=name, bucket=bucket, content=content, **kw))

    def test_content_normalized(self):
        p = self.make(content=" bikini ,\nBikini, , sarong,  side_tie   bikini ")
        self.assertEqual(p["content"], "bikini, sarong, side_tie bikini", "去空、按规范化写法去重、保持顺序")
        self.assertEqual(presets.normalize_content(["a", "b", "a"]), "a, b")
        self.assertEqual((p["bucket"], p["bucket_name"]), ("outfit", "服饰"))

    def test_validation(self):
        with self.assertRaises(tagops.OpError):
            self.make(name=" ")
        with self.assertRaises(tagops.OpError):
            self.make(content=" , ")
        with self.assertRaises(tagops.OpError):
            self.make(bucket="nope")
        self.assertEqual(self.make(content="", lora="<lora:x:1>")["lora"], "<lora:x:1>", "只有 LoRA 也行")

    def test_same_name(self):
        p = self.make()
        with self.assertRaises(presets.PresetExists) as cm:
            self.make(content="swimsuit")
        self.assertEqual(cm.exception.existing["id"], p["id"])
        q = self.make(content="swimsuit", overwrite=True)
        self.assertEqual((q["id"], q["content"]), (p["id"], "swimsuit"))
        self.assertEqual(self.make(bucket="scene", content="beach")["name"], "泳装", "不同段可以同名")
        self.assertEqual([x["bucket"] for x in presets.list_presets(self.conn)], ["outfit", "scene"])
        self.assertEqual(len(presets.list_presets(self.conn, "scene")), 1)

    def test_update_and_list_order(self):
        a = self.make()
        b = self.make(name="女仆", content="maid")
        with self.assertRaises(tagops.OpError):
            presets.update(self.conn, b["id"], {"name": "泳装"})
        presets.update(self.conn, b["id"], {"starred": True, "note": "常用"})
        self.assertEqual([x["id"] for x in presets.list_presets(self.conn)], [b["id"], a["id"]], "星标在前")
        n = self.conn.execute("SELECT COUNT(*) FROM op_log").fetchone()[0]
        presets.update(self.conn, a["id"], {"content": "bikini,sarong"})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM op_log").fetchone()[0], n, "没变化不记日志")

    def test_undo(self):
        p = self.make()
        presets.update(self.conn, p["id"], {"content": "swimsuit"})
        presets.delete(self.conn, p["id"])
        self.assertEqual(presets.list_presets(self.conn), [])
        tagops.undo_last(self.conn)
        self.assertEqual(presets.get_preset(self.conn, p["id"])["content"], "swimsuit")
        tagops.undo_last(self.conn)
        self.assertEqual(presets.get_preset(self.conn, p["id"])["content"], "bikini, sarong")
        tagops.undo_last(self.conn)
        self.assertEqual(presets.list_presets(self.conn), [])

    def test_undo_overwrite(self):
        p = self.make()
        self.make(content="swimsuit", overwrite=True)
        tagops.undo_last(self.conn)
        self.assertEqual(presets.get_preset(self.conn, p["id"])["content"], "bikini, sarong")

    def test_groups_and_lora(self):
        self.make(name="角色C", bucket="character", content="ruan mei (honkai: star rail)", lora="ruanmei:0.8")
        self.make(name="角色C", bucket="appearance", content="long hair")
        self.make(name="单个", bucket="scene", content="beach")
        [grp] = presets.groups(self.conn)
        self.assertEqual((grp["name"], [p["bucket"] for p in grp["presets"]]), ("角色C", ["character", "appearance"]))
        rows = {(p["name"], p["bucket"]): p for p in presets.list_presets(self.conn)}
        self.assertEqual((rows[("角色C", "character")]["group"], rows[("单个", "scene")]["group"]), (2, 1))
        self.assertEqual(rows[("角色C", "character")]["lora"], "<lora:ruanmei:0.8>")
        with self.assertRaises(tagops.OpError):
            self.make(name="坏", lora="<oops>")

    def test_group_text_for_copy(self):
        self.make(name="角色C", bucket="character", content="ruan mei (honkai: star rail), 1girl", lora="ruanmei:0.8")
        self.make(name="角色C", bucket="appearance", content="long_hair, 1girl")
        self.make(name="角色C", bucket="unknown", content="smile")
        tagops.patch_buckets(self.conn, {"joins": {"key": "unknown", "joins": False}})
        [grp] = presets.groups(self.conn)
        self.assertEqual(grp["text"], "ruan mei (honkai: star rail), 1girl, long_hair",
                         "按段顺序、跨段去重、写法原样、跳过不拼接的段、不带 LoRA")

    def group(self, items, name="角色B", **kw):
        return presets.create_group(self.conn, dict(name=name, items=[{"bucket": k, "content": v} for k, v in items],
                                                    **kw))

    def test_group_create(self):
        out = self.group([("character", "surtr (arknights)"), ("appearance", "red hair, long hair")])
        self.assertEqual([(p["bucket"], p["group"]) for p in out], [("character", 2), ("appearance", 2)])
        [grp] = presets.groups(self.conn)
        self.assertEqual(grp["name"], "角色B")
        self.assertEqual(self.group([("scene", "beach")], name="单段")[0]["group"], 1, "只有一段就是普通预设")
        with self.assertRaises(tagops.OpError):
            self.group([("scene", "a"), ("scene", "b")], name="重复段")
        with self.assertRaises(tagops.OpError):
            self.group([], name="空")
        with self.assertRaises(tagops.OpError):
            self.group([("scene", "a")], name=" ")
        self.assertEqual(len(presets.list_presets(self.conn)), 3, "校验失败的都没写")

    def test_group_conflict_and_merge(self):
        old = self.make(name="角色B", bucket="appearance", content="red hair, ahoge", lora="x:0.5", starred=True)
        items = [("character", "surtr (arknights)"), ("appearance", "red hair, ahoge, purple eyes")]
        with self.assertRaises(presets.PresetGroupConflict) as cm:
            self.group(items)
        self.assertEqual([p["id"] for p in cm.exception.conflicts], [old["id"]])
        self.assertEqual(len(presets.list_presets(self.conn)), 1, "冲突时什么都不写")
        out = self.group(items, overwrite=True)
        merged = presets.get_preset(self.conn, old["id"])
        self.assertEqual((merged["content"], merged["lora"], merged["starred"]),
                         ("red hair, ahoge, purple eyes", "<lora:x:0.5>", True), "只换内容，LoRA 和星标不动")
        self.assertEqual(len(out), 2)
        n = len(tagops.list_ops(self.conn))
        self.assertEqual(self.group(items, overwrite=True), [], "内容没变的段跳过")
        self.assertEqual(len(tagops.list_ops(self.conn)), n, "什么都没改就不记操作")

    def test_group_undo_once(self):
        old = self.make(name="角色B", bucket="appearance", content="red hair")
        self.group([("character", "surtr (arknights)"), ("appearance", "red hair, long hair")], overwrite=True)
        tagops.undo_last(self.conn)
        self.assertEqual([(p["bucket"], p["content"]) for p in presets.list_presets(self.conn)],
                         [("appearance", "red hair")], "一次撤销整组：新建的删掉，合并的还原")
        self.assertEqual(presets.get_preset(self.conn, old["id"])["id"], old["id"])

    def save(self, items, name="", **kw):
        return presets.save_group(self.conn, dict(name=name, items=[dict(bucket=k, content=v, **x) for k, v, x in items],
                                                  **kw))

    def test_save_group_edit(self):
        old = self.group([("character", "surtr (arknights)"), ("appearance", "red hair")])
        self.make(name="角色B", bucket="outfit", content="black dress", starred=True)   # 第三段，带星标
        out = self.save([("character", "surtr (arknights)", {"lora": "surtr:0.8"}),
                         ("appearance", "red hair, long hair", {}), ("scene", "volcano", {})],
                        name="角色B", new_name="角色B（精二）")
        self.assertEqual([(p["bucket"], p["name"], p["content"]) for p in out],
                         [("character", "角色B（精二）", "surtr (arknights)"),
                          ("appearance", "角色B（精二）", "red hair, long hair"),
                          ("scene", "角色B（精二）", "volcano")], "改名、改内容、加段、去段（服饰被删）")
        self.assertEqual(out[0]["lora"], "<lora:surtr:0.8>")
        self.assertEqual(out[0]["id"], old[0]["id"], "原来有的段保留 id，槽里的引用不断")
        self.assertEqual([p["name"] for p in presets.list_presets(self.conn)], ["角色B（精二）"] * 3)
        tagops.undo_last(self.conn)
        self.assertEqual(sorted((p["bucket"], p["name"], p["content"], p["starred"])
                                for p in presets.list_presets(self.conn)),
                         [("appearance", "角色B", "red hair", False), ("character", "角色B", "surtr (arknights)", False),
                          ("outfit", "角色B", "black dress", True)], "一次撤销整组，删掉的段连星标一起回来")

    def test_save_group_new_and_errors(self):
        self.make(name="泳装", bucket="outfit", content="bikini")
        with self.assertRaises(tagops.OpError) as cm:
            self.save([("scene", "beach", {})], new_name="泳装")
        self.assertIn("已经有叫「泳装」", str(cm.exception), "新建撞名：哪怕不在同一段也报错")
        for bad in ([], [("scene", "a", {}), ("scene", "b", {})], [("scene", " , ", {})]):
            with self.assertRaises(tagops.OpError):
                self.save(bad, new_name="新组")
        with self.assertRaises(tagops.OpError):
            self.save([("scene", "a", {})])
        self.assertEqual(len(presets.list_presets(self.conn)), 1, "出错什么都不写")
        out = self.save([("scene", "beach", {}), ("outfit", "swimsuit", {})], new_name="海边")
        self.assertEqual([p["bucket"] for p in out], ["outfit", "scene"], "按段顺序返回")
        n = len(tagops.list_ops(self.conn))
        self.save([("scene", "beach", {}), ("outfit", "swimsuit", {})], name="海边")
        self.assertEqual(len(tagops.list_ops(self.conn)), n, "什么都没变不记操作")
        with self.assertRaises(tagops.OpError):
            self.save([("scene", "beach", {})], name="海边", new_name="泳装")

    def test_save_group_starred_and_delete(self):
        self.group([("character", "surtr (arknights)"), ("appearance", "red hair")])
        self.save([("character", "surtr (arknights)", {}), ("appearance", "red hair", {})], name="角色B", starred=True)
        self.assertEqual([p["starred"] for p in presets.list_presets(self.conn)], [True, True], "整组加星标")
        out = self.save([("character", "surtr (arknights)", {}), ("appearance", "red hair", {}),
                         ("outfit", "black dress", {})], name="角色B")
        self.assertEqual([p["starred"] for p in out], [True, True, True], "不给 starred：原有的不动，新加的段跟着组")
        presets.delete_group(self.conn, "角色B")
        self.assertEqual(presets.list_presets(self.conn), [])
        with self.assertRaises(tagops.OpError):
            presets.delete_group(self.conn, "角色B")
        tagops.undo_last(self.conn)
        self.assertEqual(len(presets.list_presets(self.conn)), 3, "一次撤销整组删除")

    def test_bucket_with_presets_cannot_be_deleted(self):
        self.make()
        with self.assertRaises(tagops.OpError):
            tagops.patch_buckets(self.conn, {"delete": "outfit"})


if __name__ == "__main__":
    unittest.main()

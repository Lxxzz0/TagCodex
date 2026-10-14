# -*- coding: utf-8 -*-
"""图库查询、单图修改与图片关联测试：临时库，不依赖真实数据；规则表临时关掉，归类结果可预期。"""
import os
import shutil
import sys
import tempfile
import time
import unittest

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import app       # noqa: E402
import db        # noqa: E402
import gallery   # noqa: E402
import importer  # noqa: E402
import presets   # noqa: E402
import scanner   # noqa: E402
import tagdict   # noqa: E402
import tagger    # noqa: E402
import tagops    # noqa: E402


class GalleryTest(unittest.TestCase):
    def setUp(self):
        self.saved_rules = dict(importer.RULES)
        importer.RULES.update(module=None, error=None)
        self.tmp = tempfile.mkdtemp(prefix="tagcodex_gallery_")
        self.conn = db.connect(os.path.join(self.tmp, "t.db"))
        db.init_db(self.conn)
        # 两张图：a 在图库里（反推/nsfw/角色A/套1），b 是仅导入记录
        self.a = self.load("套1", "a", ["1girl", "bikini", "long hair", "beach", "smile"])
        self.b = self.load("套2", "b", ["1girl", "school uniform"])
        with self.conn:
            self.conn.execute("UPDATE images SET path = 'D:/g/反推/nsfw/角色A/套1/a.png', dir = '反推/nsfw/角色A/套1', "
                              "sha1 = "
                              "'0123456789abcdef', touched = '2026-10-11T10:00:00' WHERE id = ?", (self.a,))
            self.conn.execute("UPDATE images SET touched = '2026-10-11T09:00:00' WHERE id = ?", (self.b,))
        self.assign(["1girl", "long hair"], "character")
        self.assign(["bikini", "school uniform"], "outfit")
        self.assign(["beach"], "scene")

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

    def assign(self, words, key):
        tagops.assign(self.conn, [self.tid(w) for w in words], key)

    def segs(self, image_id):
        return {s["key"]: [t["word"] for t in s["tags"]] for s in gallery.segments(self.conn, [image_id])[image_id]}

    def words(self, image_id):
        return [r[0] for r in self.conn.execute("SELECT t.word FROM image_tags it JOIN tags t ON t.id = it.tag_id "
                                                "WHERE it.image_id = ? ORDER BY it.ord", (image_id,))]

    # ---------- 分段与提示词 ----------
    def test_segments_follow_dictionary(self):
        s = self.segs(self.a)
        self.assertEqual(list(s), ["character", "appearance", "outfit", "expression", "pose", "camera",
                                   "scene", "mood", "artist", "unknown"])
        self.assertEqual(s["character"], ["1girl", "long hair"])
        self.assertEqual(s["unknown"], ["smile"])
        tagops.patch_buckets(self.conn, {"add": {"key": "extra", "name": "道具"}})
        self.assign(["smile"], "extra")
        s = self.segs(self.a)
        self.assertEqual(list(s)[-2:], ["extra", "unknown"], "新增段出现在图上，插在未归类前")
        self.assertEqual((s["extra"], s["unknown"]), (["smile"], []))

    def test_prompt_skips_blocked_and_non_joining(self):
        item = gallery.list_images(self.conn, {"scope": "gallery"})["items"][0]
        self.assertEqual(item["prompt"], "1girl, long hair, bikini, beach, smile")
        tagops.block(self.conn, [self.tid("bikini")])
        tagops.patch_buckets(self.conn, {"joins": {"key": "unknown", "joins": False}})
        item = gallery.list_images(self.conn, {"scope": "gallery"})["items"][0]
        self.assertEqual(item["prompt"], "1girl, long hair, beach")
        outfit = [s for s in item["segments"] if s["key"] == "outfit"][0]
        self.assertEqual(outfit["tags"][0]["blocked"], True, "黑名单词仍然返回，由界面决定显示与否")
        self.assertEqual([t["freq"] for s in item["segments"] if s["key"] == "character" for t in s["tags"]], [2, 1])

    def test_segment_text_for_copy(self):
        tagops.block(self.conn, [self.tid("long hair")])
        tagops.patch_buckets(self.conn, {"joins": {"key": "unknown", "joins": False}})
        segs = {s["key"]: s["text"] for s in gallery.segments(self.conn, [self.a])[self.a]}
        self.assertEqual(segs["character"], "1girl", "按段复制不带黑名单词")
        self.assertEqual(segs["unknown"], "smile", "不参与拼接的段也有文本")
        self.assertEqual(segs["pose"], "")

    # ---------- 列表 / 树 / 详情 ----------
    def test_filters_and_sort(self):
        ids = lambda **a: [it["id"] for it in gallery.list_images(self.conn, a)["items"]]
        self.assertEqual(ids(), [self.a, self.b], "默认按最近更新提示词")
        self.assertEqual(ids(sort="name"), [self.b, self.a])
        self.assertEqual(ids(scope="placeholder"), [self.b])
        self.assertEqual(ids(scope="gallery"), [self.a])
        self.assertEqual(ids(dir="反推/nsfw", set="套1"), [self.a])
        self.assertEqual(ids(dir="反推/nsfw/角色A/套1", dir_only="1"), [self.a])
        self.assertEqual(ids(dir="反推/nsfw", dir_only="1"), [], "dir_only 不含子文件夹")
        self.assertEqual(ids(dir="反推/ns"), [], "按整段文件夹名匹配，不是字符串前缀")
        self.assertEqual(ids(q="School_Uniform"), [self.b], "搜词先规范化")
        self.assertEqual(ids(q="hair"), [self.a], "部分匹配")
        self.assertEqual(ids(q="100%"), [])
        self.assertEqual(ids(state="untagged"), [])
        tagops.merge_alias(self.conn, self.tid("bikini"), [self.tid("school uniform")])
        self.assertEqual(ids(q="school uniform"), [self.a, self.b], "按别名找到主词")
        item = gallery.list_images(self.conn, {"scope": "gallery"})["items"][0]
        self.assertEqual((item["thumb"], item["placeholder"]), ("01234567", False))
        b = gallery.list_images(self.conn, {"scope": "placeholder"})["items"][0]
        self.assertEqual((b["thumb"], b["placeholder"]), (None, True))

    def test_tree(self):
        def add(path, d, stem):
            self.conn.execute("INSERT INTO images (path, stem, dir, set_name, origin, added) "
                              "VALUES (?, ?, ?, ?, 'scan', '2026')", (path, stem, d, d.rpartition("/")[2] or "g"))
        with self.conn:      # 根目录下一张、同名文件夹在不同父目录下、深层文件夹、带 _ 的文件夹名，再加一张图库外的
            add("D:/g/top.png", "", "top")
            add("D:/g/反推/nsfw/未反推/n.png", "反推/nsfw/未反推", "n")
            add("D:/g/反推/sfw/角色A/套9/s.png", "反推/sfw/角色A/套9", "s")
            add("D:/g/生成/nsfw/角色A/套1/g.png", "生成/nsfw/角色A/套1", "g")
            add("D:/g/生成/x_y/z.png", "生成/x_y", "z")
            self.conn.execute("INSERT INTO images (path, stem, set_name, origin, added) "
                              "VALUES ('D:/别处/o.png', 'o', '别处', 'tagged', '2026')")
        t = gallery.tree(self.conn)
        self.assertEqual(t["root_own"], 1)
        flat = {}

        def walk(nodes):
            for n in nodes:
                flat[n["path"]] = (n["name"], n["count"], n["own"])
                walk(n["children"])
        walk(t["dirs"])
        self.assertEqual([n["name"] for n in t["dirs"]], ["反推", "生成"])
        self.assertEqual(flat["反推"], ("反推", 3, 0), "count 含子文件夹")
        self.assertEqual(flat["反推/nsfw"], ("nsfw", 2, 0))
        self.assertEqual(flat["反推/nsfw/角色A/套1"], ("套1", 1, 1))
        self.assertEqual(flat["生成/nsfw/角色A/套1"], ("套1", 1, 1), "不同父目录下的同名文件夹各算各的")
        self.assertEqual(flat["反推/nsfw/未反推"], ("未反推", 1, 1), "原来的收件箱只是普通文件夹")
        self.assertEqual((t["total"], t["gallery"], t["placeholder"], t["outside"], t["missing"]), (8, 6, 1, 1, 0))

        ids = lambda **a: sorted(it["stem"] for it in gallery.list_images(self.conn, a)["items"])
        self.assertEqual(ids(dir="反推"), ["a", "n", "s"])
        self.assertEqual(ids(dir="生成/nsfw"), ["g"])
        self.assertEqual(ids(dir="", dir_only="1"), ["top"], "根目录下直接放的")
        self.assertEqual(ids(dir="生成/x_y"), ["z"])
        self.assertEqual(ids(dir="生成/xxy"), [], "文件夹名里的 _ 不当通配符")
        self.assertEqual(ids(dir="/反推/nsfw/"), ["a", "n"], "首尾的 / 去掉")
        self.assertEqual(ids(scope="outside"), ["o"])
        self.assertEqual(ids(dir="反推", state="untagged"), ["n", "s"], "和状态叠加：原来的收件箱 · 未反推")
        self.assertEqual(ids(dir="反推", sort="name"), ["a", "n", "s"])

    def test_detail_nav(self):
        d = gallery.detail(self.conn, self.a, {})
        self.assertEqual(d["nav"], {"index": 0, "total": 2, "prev": None, "next": self.b})
        d = gallery.detail(self.conn, self.b, {})
        self.assertEqual((d["nav"]["prev"], d["nav"]["next"]), (self.a, None))
        d = gallery.detail(self.conn, self.b, {"scope": "gallery"})
        self.assertEqual(d["nav"]["index"], None, "不在当前筛选里")
        with self.assertRaises(tagops.OpError):
            gallery.detail(self.conn, 999, {})

    # ---------- 按预设组筛选 ----------
    def group(self, name, **segs):
        presets.save_group(self.conn, {"name": "", "new_name": name,
                                       "items": [{"bucket": k, "content": v} for k, v in segs.items()]})

    def test_group_filter(self):
        # 组里 4 个词：a 命中 1girl / long hair / bikini（75%），b 只命中 1girl（25%）；Long_Hair 先规范化
        self.group("泳装", character="1girl, Long_Hair", outfit="bikini, sarong")
        page = lambda **a: gallery.list_images(self.conn, dict(group="泳装", **a))["items"]
        ids = lambda **a: [it["id"] for it in page(**a)]
        self.assertEqual(ids(), [self.a], "默认阈值 50%")
        self.assertEqual(ids(group_min=75), [self.a], "正好等于阈值也算")
        self.assertEqual(ids(group_min=76), [])
        self.assertEqual(ids(group_min=25, sort="hit"), [self.a, self.b])
        self.assertEqual([(it["group_hit"], it["group_total"]) for it in page(group_min=25, sort="hit")],
                         [(3, 4), (1, 4)])
        self.assertEqual(ids(group_min=25, scope="placeholder"), [self.b], "和其他筛选叠加")
        self.assertEqual(ids(group_min=25, sort="hit", q="school"), [self.b])
        self.assertEqual(gallery.stats(self.conn, {"group": "泳装"})["total"], 1, "词频也认")
        d = gallery.detail(self.conn, self.b, {"group": "泳装", "group_min": 25, "sort": "hit"})
        self.assertEqual((d["nav"]["prev"], d["groups"]), (self.a, []), "详情固定按 50% 列所在的组")
        self.assertEqual(gallery.detail(self.conn, self.a, {})["groups"], [{"name": "泳装", "hit": 3, "total": 4}])
        self.assertEqual(presets.match_counts(self.conn), [{"name": "泳装", "total": 4, "count": 1}])
        self.assertEqual(presets.match_counts(self.conn, 25)[0]["count"], 2)
        for bad in ("0", "101", "x"):
            with self.assertRaises(tagops.OpError):
                ids(group_min=bad)
        self.assertEqual(ids(sort="hit"), [self.a])
        self.assertEqual(gallery.list_images(self.conn, {"sort": "hit"})["total"], 2, "没选组时 hit 排序按最近更新")

    def test_group_alias_blocked_and_empty(self):
        tagops.block(self.conn, [self.tid("beach")])
        self.group("海边", scene="beach", outfit="school uniform")
        self.assertEqual(presets.group_words(self.conn, "海边"), ["school uniform", "beach"], "按段顺序")
        tagops.merge_alias(self.conn, self.tid("bikini"), [self.tid("school uniform")])
        self.assertEqual(presets.group_words(self.conn, "海边"), ["bikini", "beach"], "组里的词换成主词")
        ids = [it["id"] for it in gallery.list_images(self.conn, {"group": "海边", "group_min": 100})["items"]]
        self.assertEqual(ids, [self.a], "黑名单词也算命中")
        presets.create(self.conn, {"name": "只有LoRA", "bucket": "character", "content": "", "lora": "x:1"})
        self.assertEqual(gallery.list_images(self.conn, {"group": "只有LoRA", "group_min": 1})["total"], 0)
        self.assertEqual(gallery.list_images(self.conn, {"group": "不存在"})["total"], 0)

    # ---------- 单图修改 ----------
    def test_add_words_and_undo(self):
        r = gallery.edit_tags(self.conn, self.a, {"add": "Smile, ocean, ocean, beach_towel", "bucket": "scene"})
        self.assertEqual(r["added"], ["ocean", "beach towel"])
        self.assertEqual(r["already"], ["smile"])
        self.assertEqual(r["created"], 2)
        self.assertEqual([e["word"] for e in r["elsewhere"]], ["ocean", "beach towel"], "新词进了未归类，不是环境")
        self.assertEqual(self.words(self.a)[-2:], ["ocean", "beach towel"])
        self.assertEqual(self.words(self.b), ["1girl", "school uniform"], "不影响别的图")
        img = gallery.get_image(self.conn, self.a)
        self.assertTrue(img["edited"])
        self.assertNotEqual(img["touched"], "2026-10-11T10:00:00")
        src = self.conn.execute("SELECT source FROM tags WHERE word = 'ocean'").fetchone()[0]
        self.assertEqual(src, "rule")

        tagops.undo_last(self.conn)
        self.assertEqual(self.words(self.a), ["1girl", "bikini", "long hair", "beach", "smile"])
        self.assertIsNone(self.conn.execute("SELECT 1 FROM tags WHERE word = 'ocean'").fetchone(), "新建的词撤销后删掉")
        img = gallery.get_image(self.conn, self.a)
        self.assertEqual((img["edited"], img["touched"]), (None, "2026-10-11T10:00:00"))

    def test_undo_keeps_created_word_used_elsewhere(self):
        gallery.edit_tags(self.conn, self.a, {"add": "ocean"})
        self.load("套3", "c", ["ocean"])            # 普通导入不记日志
        tagops.undo_last(self.conn)
        self.assertNotIn("ocean", self.words(self.a))
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM tags WHERE word = 'ocean'").fetchone())

    def test_remove_and_undo_keeps_scores(self):
        with self.conn:
            self.conn.execute("UPDATE image_tags SET score = 0.42 WHERE image_id = ? AND tag_id = ?",
                              (self.a, self.tid("beach")))
        r = gallery.edit_tags(self.conn, self.a, {"remove": [self.tid("bikini"), self.tid("school uniform")]})
        self.assertEqual(r["changed"], 1)
        self.assertEqual(self.words(self.a), ["1girl", "long hair", "beach", "smile"])
        self.assertEqual(gallery.edit_tags(self.conn, self.a, {"remove": [self.tid("bikini")]})["changed"], 0)
        self.assertEqual(len(tagops.list_ops(self.conn)), 4, "初始化 3 次归位 + 1 次删除；没改动不记日志")
        tagops.undo_last(self.conn)
        self.assertEqual(self.words(self.a), ["1girl", "bikini", "long hair", "beach", "smile"])
        self.assertEqual(self.conn.execute("SELECT score FROM image_tags WHERE image_id = ? AND tag_id = ?",
                                           (self.a, self.tid("beach"))).fetchone()[0], 0.42)

    def test_reorder_within_segment(self):
        # a 的顺序：1girl bikini long_hair beach smile；角色段是 1girl、long hair
        gallery.edit_tags(self.conn, self.a, {"order": [self.tid("long hair"), self.tid("1girl")]})
        self.assertEqual(self.words(self.a), ["long hair", "bikini", "1girl", "beach", "smile"], "其他词位置不动")
        self.assertEqual(self.segs(self.a)["character"], ["long hair", "1girl"])
        same = gallery.edit_tags(self.conn, self.a, {"order": [self.tid("long hair"), self.tid("1girl")]})
        self.assertEqual(same["changed"], 0)
        with self.assertRaises(tagops.OpError):
            gallery.edit_tags(self.conn, self.a, {"order": [self.tid("school uniform")]})
        tagops.undo_last(self.conn)
        self.assertEqual(self.segs(self.a)["character"], ["1girl", "long hair"])

    def test_bad_requests(self):
        for body in ({}, {"add": "x", "remove": [1]}, {"add": " , "}, {"remove": ["x"]}):
            with self.assertRaises(tagops.OpError):
                gallery.edit_tags(self.conn, self.a, body)
        with self.assertRaises(tagops.OpError):
            gallery.edit_tags(self.conn, 999, {"add": "x"})

    # ---------- 图片关联 ----------
    def link_pairs(self):
        return sorted(tuple(r) for r in self.conn.execute("SELECT src_id, gen_id FROM image_links"))

    def test_links_both_sides_and_undo(self):
        c = self.load("套3", "c", ["solo"])
        r = gallery.links(self.conn, self.a, {"role": "gen", "add": [self.b, c]})
        self.assertEqual((r["changed"], r["skipped"]), (2, 0))
        self.assertEqual(self.link_pairs(), [(self.a, self.b), (self.a, c)])
        r = gallery.links(self.conn, c, {"role": "src", "add": [self.a, self.b]})
        self.assertEqual((r["changed"], r["skipped"]), (1, 1), "同向已有的跳过")
        self.assertEqual(self.link_pairs(), [(self.a, self.b), (self.a, c), (self.b, c)], "多对多")

        d = gallery.detail(self.conn, self.a, {})
        self.assertEqual(([x["id"] for x in d["gens"]], d["sources"]), ([self.b, c], []))
        self.assertEqual((d["gens"][0]["placeholder"], d["gens"][0]["thumb"]), (True, None))
        d = gallery.detail(self.conn, c, {})
        self.assertEqual(sorted(x["id"] for x in d["sources"]), [self.a, self.b])
        self.assertNotIn("sha1", d["sources"][0])

        page = gallery.list_images(self.conn, {"link_of": self.b})["items"]
        self.assertEqual({it["id"]: it["linked"] for it in page}, {self.a: True, self.b: False, c: True},
                         "不分方向都算已关联")

        r = gallery.links(self.conn, c, {"role": "src", "remove": [self.a, self.a]})
        self.assertEqual(r["changed"], 1)
        self.assertEqual(self.link_pairs(), [(self.a, self.b), (self.b, c)])
        self.assertEqual(gallery.links(self.conn, c, {"role": "src", "remove": [self.a]})["changed"], 0)
        tagops.undo_last(self.conn)
        self.assertEqual(self.link_pairs(), [(self.a, self.b), (self.a, c), (self.b, c)], "撤销取消关联")
        tagops.undo_last(self.conn)
        self.assertEqual(self.link_pairs(), [(self.a, self.b), (self.a, c)], "撤销关联")
        tagops.undo_last(self.conn)
        self.assertEqual(self.link_pairs(), [])

    def test_link_rules(self):
        gallery.links(self.conn, self.a, {"role": "gen", "add": [self.b]})
        n = len(tagops.list_ops(self.conn))
        with self.assertRaisesRegex(tagops.OpError, "方向相反"):
            gallery.links(self.conn, self.a, {"role": "src", "add": [self.b]})
        with self.assertRaisesRegex(tagops.OpError, "方向相反"):
            gallery.links(self.conn, self.b, {"role": "gen", "add": [self.a]})
        self.assertEqual(gallery.links(self.conn, self.b, {"role": "src", "add": [self.a]})["changed"], 0,
                         "从另一边加同一条，等于已有")
        self.assertEqual(len(tagops.list_ops(self.conn)), n, "没改动不记日志")
        for body in ({"role": "gen", "add": [self.a]}, {"role": "x", "add": [self.b]}, {"role": "gen"},
                     {"role": "gen", "add": [self.b], "remove": [self.b]}, {"role": "gen", "add": []},
                     {"role": "gen", "add": ["x"]}, {"role": "gen", "add": [999]}):
            with self.assertRaises(tagops.OpError, msg=str(body)):
                gallery.links(self.conn, self.a, body)
        with self.assertRaises(tagops.OpError):
            gallery.links(self.conn, 999, {"role": "gen", "add": [self.a]})
        self.assertEqual(self.link_pairs(), [(self.a, self.b)], "出错时一条都不加")


class GalleryApiTest(unittest.TestCase):
    """走 HTTP：扫描 → 列表 / 树 / 详情 → 单图修改 → 撤销 → 原图和缩略图。"""

    def setUp(self):
        self.saved = dict(app.CONFIG)
        self.tmp = tempfile.mkdtemp(prefix="tagcodex_gapi_")
        app.CONFIG.update(db_path=os.path.join(self.tmp, "t.db"), tag_dict_path=os.path.join(self.tmp, "td.json"),
                          thumbs_dir=os.path.join(self.tmp, "thumbs"), gallery_root=os.path.join(self.tmp, "g"))
        app.init()
        self.c = app.app.test_client()
        self.path = os.path.join(self.tmp, "g", "反推", "nsfw", "角色A", "套1", "a.png")
        os.makedirs(os.path.dirname(self.path))
        Image.new("RGB", (500, 300), (9, 9, 9)).save(self.path)
        conn = db.connect(app.CONFIG["db_path"])
        with conn:
            importer._import_records(conn, [("a", [(None, None, ["1girl", "bikini"])])], "套1", "x.txt", False,
                                     importer._new_report(False))
            importer._import_records(conn, [("b", [(None, None, ["solo"])])], "套2", "x.txt", False,
                                     importer._new_report(False))
        conn.close()

    def tearDown(self):
        deadline = time.time() + 10
        while scanner.JOB.get("running") and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(tagdict.DEBOUNCE_SECONDS + 0.3)
        app.CONFIG.clear()
        app.CONFIG.update(self.saved)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def scan(self):
        r = self.c.post("/api/scan", json={})
        self.assertEqual(r.status_code, 200, r.get_json())
        deadline = time.time() + 10
        while time.time() < deadline:
            job = self.c.get("/api/scan/status").get_json()["job"]
            if not job["running"]:
                return job
            time.sleep(0.05)
        self.fail("扫描超时")

    def test_flow(self):
        job = self.scan()
        self.assertEqual((job["phase"], job["report"]["backfilled"]), ("完成", [self.path]))
        tree = self.c.get("/api/images/tree").get_json()
        top = tree["dirs"][0]
        self.assertEqual((top["path"], top["count"], top["children"][0]["path"], tree["placeholder"], tree["root_own"]),
                         ("反推", 1, "反推/nsfw", 1, 0))
        self.assertNotIn("legacy", self.c.get("/api/scan/status").get_json())

        page = self.c.get("/api/images?scope=gallery").get_json()
        self.assertEqual(page["total"], 1)
        item = page["items"][0]
        iid = item["id"]
        self.assertEqual(item["prompt"], "1girl, bikini")

        r = self.c.get("/thumb/%d.webp?v=%s" % (iid, item["thumb"]))
        self.assertEqual((r.status_code, r.mimetype), (200, "image/webp"))
        self.assertGreater(r.cache_control.max_age, 86400)
        r.close()
        os.remove(scanner.thumb_path(app.CONFIG, iid))
        r = self.c.get("/thumb/%d.webp" % iid)
        self.assertEqual(r.status_code, 200, "缺了当场生成")
        r.close()
        r = self.c.get("/image/%d" % iid)
        self.assertEqual(r.status_code, 200)
        r.close()
        pid = self.c.get("/api/images?scope=placeholder").get_json()["items"][0]["id"]
        for url in ("/image/%d" % pid, "/thumb/%d.webp" % pid, "/image/999"):
            self.assertEqual(self.c.get(url).status_code, 404, url)

        r = self.c.post("/api/images/%d/tags" % iid, json={"add": "smile"})
        self.assertEqual(r.get_json()["added"], ["smile"])
        d = self.c.get("/api/images/%d?scope=gallery" % iid).get_json()
        self.assertEqual((d["prompt"], d["nav"]["total"]), ("1girl, bikini, smile", 1))
        self.assertEqual(self.c.post("/api/images/%d/tags" % iid, json={}).status_code, 400)
        self.assertEqual(self.c.get("/api/images/999").status_code, 400)
        self.c.post("/api/oplog/undo")
        self.assertEqual(self.c.get("/api/images/%d" % iid).get_json()["prompt"], "1girl, bikini")

        r = self.c.post("/api/images/%d/links" % iid, json={"role": "gen", "add": [pid]})
        self.assertEqual(r.get_json()["changed"], 1)
        self.assertEqual([x["id"] for x in self.c.get("/api/images/%d" % pid).get_json()["sources"]], [iid])
        r = self.c.post("/api/images/%d/links" % pid, json={"role": "gen", "add": [iid]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("方向相反", r.get_json()["error"])
        page = self.c.get("/api/images?link_of=%d" % iid).get_json()
        self.assertEqual({it["id"]: it["linked"] for it in page["items"]}, {iid: False, pid: True})
        self.c.post("/api/oplog/undo")
        self.assertEqual(self.c.get("/api/images/%d" % iid).get_json()["gens"], [])

    def test_scan_errors(self):
        r = self.c.post("/api/scan", json={"root": os.path.join(self.tmp, "没有")})
        self.assertEqual(r.status_code, 400)
        tagger.JOB["running"] = True
        try:
            self.assertEqual(self.c.post("/api/scan", json={}).status_code, 409)
        finally:
            tagger.JOB["running"] = False


if __name__ == "__main__":
    unittest.main()


class CharListTest(unittest.TestCase):
    """反推模型角色表：csv 里 category=4 的词归角色，优先级在 danbooru 和规则表之前，手工归类不动。"""

    def setUp(self):
        self.saved = (dict(importer.RULES), dict(importer.CHARS))
        importer.RULES.update(module=None, error=None)
        self.tmp = tempfile.mkdtemp(prefix="tagcodex_chars_")
        with open(os.path.join(self.tmp, "m.csv"), "w", encoding="utf-8", newline="") as f:
            f.write("tag_id,name,category,count\n1,1girl,0,100\n2,surtr_(arknights),4,50\n3,Eris_Greyrat,4,10\n")
        importer.load_char_list({"models_dir": self.tmp, "model": "m"})
        self.conn = db.connect(os.path.join(self.tmp, "t.db"))
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        importer.RULES.clear(); importer.RULES.update(self.saved[0])
        importer.CHARS.clear(); importer.CHARS.update(self.saved[1])
        shutil.rmtree(self.tmp, ignore_errors=True)

    def load(self, words, stem="a"):
        with self.conn:
            importer._import_records(self.conn, [(stem, [(None, None, words)])], "套1", "x.txt", False,
                                     importer._new_report(False))

    def tag(self, word):
        return self.conn.execute("SELECT b.key, t.source FROM tags t JOIN buckets b ON b.id = t.bucket_id "
                                 "WHERE t.word = ?", (word,)).fetchone()

    def test_character_words_go_to_character(self):
        self.assertEqual(importer.CHARS["words"], {"surtr (arknights)", "eris greyrat"})
        self.load(["surtr \(arknights\)", "eris greyrat", "1girl"])
        self.assertEqual(tuple(self.tag("surtr \(arknights\)")), ("character", "tagger"), "转义括号也能对上")
        self.assertEqual(tuple(self.tag("eris greyrat")), ("character", "tagger"))
        self.assertEqual(tuple(self.tag("1girl")), ("unknown", "rule"), "普通词不受影响")
        ops = [tuple(r) for r in self.conn.execute(
            "SELECT o.source, o.bucket_key FROM tag_opinions o JOIN tags t ON t.id = o.tag_id WHERE t.word = 'eris greyrat'")]
        self.assertEqual(ops, [("tagger", "character")])

    def test_manual_wins_and_image_add_uses_char_list(self):
        self.load(["eris greyrat"])
        tid = self.conn.execute("SELECT id FROM tags WHERE word = 'eris greyrat'").fetchone()[0]
        tagops.assign(self.conn, [tid], "scene")
        self.load(["eris greyrat"], stem="b")
        self.assertEqual(tuple(self.tag("eris greyrat")), ("scene", "manual"), "手工归过的不动")
        iid = self.conn.execute("SELECT id FROM images WHERE stem = 'b'").fetchone()[0]
        r = gallery.edit_tags(self.conn, iid, {"add": "Surtr_(arknights)", "bucket": "scene"})
        self.assertEqual(r["elsewhere"][0]["bucket"], "character", "图上加词也按角色表归")

    def test_missing_csv_or_deleted_bucket_falls_back(self):
        tagops.patch_buckets(self.conn, {"delete": "character"})
        self.load(["eris greyrat"])
        self.assertEqual(tuple(self.tag("eris greyrat")), ("unknown", "rule"), "角色段被删了就往下落")
        importer.load_char_list({"models_dir": os.path.join(self.tmp, "没有"), "model": "m"})
        self.assertEqual(importer.CHARS["words"], frozenset())
        self.assertIn("角色表加载失败", importer.CHARS["error"])

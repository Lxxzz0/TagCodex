# -*- coding: utf-8 -*-
"""
接口回归测试：临时数据库 + 真实数据（config.json 里的 danbooru 表 + 测试数据目录）。
运行：.venv\\Scripts\\python.exe -m unittest discover -s tests -v
测试数据目录用环境变量 TAGCODEX_TEST_DATA 指定（目录里要有 反推记录.xlsx）；没设或找不到时整组跳过。
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import app      # noqa: E402
import tagdict  # noqa: E402

TEST_DATA = os.environ.get("TAGCODEX_TEST_DATA", "")
XLSX = os.path.join(TEST_DATA, "反推记录.xlsx")
HAVE_DATA = os.path.isfile(XLSX) and os.path.isfile(app.CONFIG.get("danbooru_xlsx", ""))


@unittest.skipUnless(HAVE_DATA, "缺少真实测试数据或 danbooru 表")
class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="tagcodex_test_")
        cls.saved = dict(app.CONFIG)
        app.CONFIG["db_path"] = os.path.join(cls.tmp, "t.db")
        app.CONFIG["tag_dict_path"] = os.path.join(cls.tmp, "tag_dict.json")
        app.CONFIG["thumbs_dir"] = os.path.join(cls.tmp, "thumbs")
        app.init()
        cls.c = app.app.test_client()
        cls.post("/api/import/ref")

    @classmethod
    def tearDownClass(cls):
        app.CONFIG.clear()
        app.CONFIG.update(cls.saved)
        time.sleep(tagdict.DEBOUNCE_SECONDS + 0.5)   # 等防抖写完再删目录
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def post(cls, url, body=None, code=200):
        r = cls.c.post(url, json=body or {})
        assert r.status_code == code, (url, r.status_code, r.get_json())
        return r.get_json()

    def get(self, url):
        r = self.c.get(url)
        self.assertEqual(r.status_code, 200, r.get_json())
        return r.get_json()

    def counts(self):
        s = self.get("/api/stats")
        return (s["total"], s["unclassified"], s["blocked"], s["file_conflicts"],
                s["machine_conflicts"], [(b["key"], b["count"]) for b in s["buckets"]])

    def ids(self):
        return {t["word"]: t["id"] for t in self.get("/api/tags?size=500&blocked=all")["items"]}

    def setUp(self):
        self.post("/api/import/xlsx", {"path": XLSX})
        self.post("/api/import/txt", {"dir": TEST_DATA})

    def test_import_repeatable(self):
        before = self.counts()
        r = self.post("/api/import/txt", {"dir": TEST_DATA})
        self.assertEqual(r["images_new"], 0)
        self.assertEqual(r["tags_new"], 0)
        self.assertEqual(self.counts(), before)

    def test_pagination_shape(self):
        p = self.get("/api/tags?size=3&page=2")
        self.assertEqual(set(p), {"items", "total", "page", "size"})
        self.assertEqual((p["page"], p["size"], len(p["items"])), (2, 3, 3))

    def test_assign_and_undo(self):
        base = self.counts()
        ids = self.ids()
        # 角色名导入时已按反推模型角色表进了角色，归到别的段才会改变各段计数
        r = self.post("/api/tags/assign", {"ids": [ids["sangonomiya kokomi"]], "bucket": "scene"})
        self.assertEqual(r["changed"], 1)
        self.assertNotEqual(self.counts(), base)
        self.post("/api/oplog/undo")
        self.assertEqual(self.counts(), base)

    def test_alias_merge_and_undo(self):
        base = self.get("/api/tags?q=hair&size=200")["items"]
        ids = self.ids()
        self.post("/api/tags/alias", {"main_id": ids["long hair"], "ids": [ids["very long hair"]]})
        merged = self.get("/api/tags?q=very long hair")["items"]
        self.assertEqual([(t["word"], t["aliases"]) for t in merged], [("long hair", "very long hair")])
        self.post("/api/oplog/undo")
        self.assertEqual(self.get("/api/tags?q=hair&size=200")["items"], base)

    def test_bucket_delete_and_undo(self):
        base = self.counts()
        self.c.patch("/api/buckets", json={"add": {"key": "light", "name": "光影"}})
        ids = self.ids()
        self.post("/api/tags/assign", {"ids": [ids["from below"]], "bucket": "light"})
        self.assertEqual(self.c.patch("/api/buckets", json={"delete": "unknown"}).status_code, 400)
        self.c.patch("/api/buckets", json={"delete": "light"})
        self.assertEqual(self.get("/api/tags?q=from below")["items"][0]["bucket"], "unknown")
        self.post("/api/oplog/undo")                      # 段和词都回来
        self.assertIn(("light", 1), self.counts()[5])
        self.post("/api/oplog/undo")                      # 撤销归位
        self.c.patch("/api/buckets", json={"delete": "light"})
        self.assertEqual(self.counts(), base)

    def test_tag_dict_export(self):
        ids = self.ids()
        self.post("/api/tags/block", {"ids": [ids["solo"]]})
        r = self.post("/api/tagdict/export")
        with open(app.CONFIG["tag_dict_path"], encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual(d["version"], 2)
        self.assertEqual(d["buckets"][-1]["key"], "unknown")
        self.assertIn("solo", d["blocked"])
        self.assertNotIn("solo", d["tags"])
        self.assertEqual(r["counts"]["tags"], len(d["tags"]))
        self.post("/api/oplog/undo")

    def test_full_export(self):
        r = self.c.get("/api/export/full?format=csv")
        self.assertTrue(r.data.startswith(b"\xef\xbb\xbf"))
        self.assertIn("word,zh,bucket", r.data.decode("utf-8-sig").splitlines()[0])
        j = self.c.get("/api/export/full?format=json").get_json()
        self.assertTrue(j["tags"] and j["buckets"])

    def test_user_errors_are_400(self):
        self.post("/api/import/xlsx", {"path": os.path.join(TEST_DATA, "不存在.xlsx")}, code=400)
        self.post("/api/tags/assign", {"ids": [], "bucket": "character"}, code=400)
        self.assertEqual(self.c.patch("/api/buckets", json={"add": {"key": "Bad Key", "name": "x"}}).status_code, 400)

    def test_themes_and_presets(self):
        th = self.post("/api/themes", {"name": "女孩", "rule": {"any": "1girl"}})
        self.assertEqual(self.post("/api/themes", {"name": "女孩"}, code=400)["error"], "已经有叫「女孩」的主题")
        n = self.get("/api/images?theme=%d&size=1" % th["id"])["total"]
        self.assertGreater(n, 0)
        self.assertEqual(self.post("/api/themes/preview", {"rule": {"any": "1girl"}})["count"], n)
        st = self.get("/api/images/stats?theme=%d" % th["id"])
        self.assertEqual(st["total"], n)
        first = self.get("/api/images?theme=%d&size=1" % th["id"])["items"][0]["id"]
        self.post("/api/themes/%d/members" % th["id"], {"remove": [first]})
        self.assertEqual(self.get("/api/images?theme=%d&theme_view=excluded" % th["id"])["total"], 1)
        self.assertEqual(self.c.patch("/api/themes/%d" % th["id"], json={"name": "女孩们"}).status_code, 200)
        self.assertEqual(self.c.delete("/api/themes/%d" % th["id"]).status_code, 200)
        self.post("/api/oplog/undo")
        self.assertEqual([t["name"] for t in self.get("/api/themes")], ["女孩们"])
        # 预设：同段重名 409 带已有的那条
        p = self.post("/api/presets", {"name": "泳装", "bucket": "outfit", "content": "bikini"})
        dup = self.post("/api/presets", {"name": "泳装", "bucket": "outfit", "content": "x"}, code=409)
        self.assertEqual(dup["existing"]["id"], p["id"])
        self.assertEqual(self.c.patch("/api/presets/%d" % p["id"], json={"starred": True}).get_json()["starred"],
                         True)
        self.assertEqual(len(self.get("/api/presets?bucket=outfit")), 1)
        self.assertEqual(self.c.delete("/api/presets/%d" % p["id"]).status_code, 200)
        self.post("/api/oplog/undo")
        self.assertEqual(len(self.get("/api/presets")), 1)
        self.post("/api/oplog/undo")                 # 撤销星标，再撤销新建
        self.post("/api/oplog/undo")
        self.assertEqual(self.get("/api/presets"), [])
        # 预设组：重名 409 带 conflicts，覆盖后一次撤销整组
        grp = {"name": "角色B", "items": [{"bucket": "character", "content": "surtr (arknights)"},
                                            {"bucket": "appearance", "content": "red hair"}]}
        self.assertEqual(len(self.post("/api/presets/group", grp)), 2)
        grp["items"][1]["content"] = "red hair, long hair"
        dup = self.post("/api/presets/group", grp, code=409)
        self.assertEqual(sorted(p["bucket"] for p in dup["conflicts"]), ["appearance", "character"])
        self.assertEqual(len(self.post("/api/presets/group", dict(grp, overwrite=True))), 1)
        self.post("/api/presets/group", {"name": "x", "items": []}, code=400)
        r = self.c.put("/api/presets/group", json={"name": "角色B", "items": [
            {"bucket": "appearance", "content": "red hair, long hair"}]})
        self.assertEqual((r.status_code, [p["bucket"] for p in r.get_json()]), (200, ["appearance"]))
        m = self.get("/api/presets/groups/match?min=100")
        self.assertEqual([(x["name"], x["total"]) for x in m], [("角色B", 2)])
        self.assertEqual(self.c.get("/api/presets/groups/match?min=0").status_code, 400)
        self.assertIn("items", self.get("/api/images?group=%E5%8F%B2%E5%B0%94%E7%89%B9%E5%B0%94&sort=hit"))
        self.assertEqual(self.c.get("/api/images?group=x&group_min=x").status_code, 400)
        self.assertEqual(self.c.put("/api/presets/group", json={"name": "角色B", "items": []}).status_code, 400)
        self.assertEqual(self.c.delete("/api/presets/group", json={"name": "角色B"}).status_code, 200)
        self.assertEqual(self.get("/api/presets"), [])
        self.post("/api/oplog/undo")
        self.post("/api/oplog/undo")
        self.post("/api/oplog/undo")
        self.post("/api/oplog/undo")
        self.assertEqual(self.get("/api/presets"), [])
        self.post("/api/oplog/undo")                 # 主题改回原名，清掉，免得影响别的用例
        self.post("/api/oplog/undo")
        self.post("/api/oplog/undo")
        self.assertEqual(self.get("/api/themes"), [])

    def test_drafts(self):
        r = self.post("/api/drafts", {"name": "接口草稿", "count": 2})
        self.assertEqual(len(r["ids"]), 2)
        self.post("/api/drafts", {"name": "接口草稿", "count": 1}, code=400)
        self.assertEqual(len(self.post("/api/drafts/items", {"name": "接口草稿", "count": 1})["ids"]), 1)
        src = {"filter": {"scope": "draft", "set": "接口草稿", "sort": "name"}}
        self.assertEqual(self.post("/api/composer/apply", {"source": src, "slots": {
            "scene": {"mode": "replace", "text": "beach"}}})["count"], 3)
        real = next(it["id"] for it in self.get("/api/images?size=50&state=tagged")["items"] if it["origin"] != "draft")
        self.post("/api/composer/apply", {"source": {"ids": [real]}, "slots": {}}, code=400)
        self.assertEqual(self.c.delete("/api/drafts/items", json={"ids": [real]}).status_code, 400)
        self.assertEqual(self.c.patch("/api/drafts", json={"name": "接口草稿", "new_name": "接口草稿2"}).status_code, 200)
        self.assertEqual([(d["name"], d["count"], d["filled"]) for d in self.get("/api/drafts")], [("接口草稿2", 3, 3)])
        self.assertEqual(self.c.delete("/api/drafts", json={"name": "接口草稿2"}).status_code, 200)
        for _ in range(5):                               # 撤销删除、改名、写入、加条、新建，清掉免得影响别的用例
            self.post("/api/oplog/undo")
        self.assertEqual(self.get("/api/drafts"), [])

    def test_composer(self):
        ids = [it["id"] for it in self.get("/api/images?size=3&state=tagged")["items"]]
        body = {"source": {"ids": ids}, "slots": {"scene": {"mode": "replace", "text": "beach, sunset"}}}
        pv = self.post("/api/composer/preview", dict(body, size=2))
        self.assertEqual((pv["total"], len(pv["items"]), pv["changed_segments"]), (len(ids), 2, ["scene"]))
        self.assertIn("output_dir", self.get("/api/composer/config"))
        self.assertEqual(self.post("/api/composer/stats", {"source": {"ids": ids}})["total"], len(ids))
        out = os.path.join(self.tmp, "组合输出")
        ex = dict(body, name="批次", dir=out, txt=True, xlsx=False)
        r = self.post("/api/composer/export", ex)
        self.assertEqual(r["count"], len(ids))
        conflict = self.post("/api/composer/export", ex, code=409)
        self.assertEqual(len(conflict["conflicts"]), len(r["files"]))
        self.post("/api/composer/export", dict(ex, overwrite=True))
        self.post("/api/composer/preview", {"source": {}}, code=400)


if __name__ == "__main__":
    unittest.main()

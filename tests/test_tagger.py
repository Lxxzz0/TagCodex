# -*- coding: utf-8 -*-
"""
反推流程测试：推理用假模型代替（不需要 onnxruntime 和真实模型），测入库、升级、跳过、停止、失败、迁移。
真实模型与 ComfyUI 的逐词对拍见 tools/tagger_parity.py。
"""
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import app      # noqa: E402
import db       # noqa: E402
import scanner  # noqa: E402
import tagdict  # noqa: E402
import tagger   # noqa: E402

FAKE_TAGS = [("sangonomiya kokomi", 0.91), ("1girl", 0.99), ("long hair", 0.8), ("vision \\(genshin impact\\)", 0.7)]


class FakeModel:
    delay = 0.0

    def __init__(self, models_dir, name):
        self.name = name

    def predict(self, path, threshold, character_threshold, exclude_tags=""):
        if "broken" in os.path.basename(path):
            raise ValueError("坏图")
        time.sleep(self.delay)
        return list(FAKE_TAGS)


class TaggerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.saved = (dict(app.CONFIG), tagger.Model, tagger.list_models, tagger.deps_error)
        tagger.Model = FakeModel
        tagger.list_models = lambda d: ["fake-model"]
        tagger.deps_error = lambda: None

    @classmethod
    def tearDownClass(cls):
        cfg, tagger.Model, tagger.list_models, tagger.deps_error = cls.saved
        app.CONFIG.clear()
        app.CONFIG.update(cfg)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tagcodex_tagger_")
        app.CONFIG["db_path"] = os.path.join(self.tmp, "t.db")
        app.CONFIG["tag_dict_path"] = os.path.join(self.tmp, "tag_dict.json")
        app.CONFIG["thumbs_dir"] = os.path.join(self.tmp, "thumbs")
        app.CONFIG["gallery_root"] = os.path.join(self.tmp, "g")       # 测试不碰真实图库
        app.CONFIG["tagger"] = {"models_dir": self.tmp, "model": "fake-model",
                                "threshold": 0.35, "character_threshold": 0.8, "exclude_tags": ""}
        app.init()
        self.c = app.app.test_client()
        self.imgs = os.path.join(self.tmp, "set1")
        os.makedirs(self.imgs)
        for name in ("a.png", "b.jpg", "c.webp"):
            Image.new("RGB", (8, 8), (200, 100, 50)).save(os.path.join(self.imgs, name))
        FakeModel.delay = 0.0

    def tearDown(self):
        deadline = time.time() + 10
        while tagger.JOB.get("running") and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(tagdict.DEBOUNCE_SECONDS + 0.3)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_job(self, **kw):
        r = self.c.post("/api/tagger/run", json=dict({"dir": self.imgs}, **kw))
        self.assertEqual(r.status_code, 200, r.get_json())
        deadline = time.time() + 10
        while time.time() < deadline:
            job = self.c.get("/api/tagger/status").get_json()["job"]
            if not job["running"]:
                return job
            time.sleep(0.05)
        self.fail("反推任务超时")

    def q(self, sql, *p):
        conn = db.connect(app.CONFIG["db_path"])
        try:
            return conn.execute(sql, p).fetchall()
        finally:
            conn.close()

    def test_tag_and_store_scores(self):
        job = self.run_job()
        self.assertEqual((job["phase"], job["done"], job["failed"]), ("完成", 3, []))
        rows = self.q("SELECT path, origin, tagger, tagged FROM images ORDER BY stem")
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["origin"] == "tagged" and r["tagged"] and "fake-model" in r["tagger"] for r in rows))
        self.assertEqual(rows[0]["path"], os.path.join(self.imgs, "a.png"))
        scores = {r[0]: r[1] for r in self.q(
            "SELECT t.word, it.score FROM image_tags it JOIN tags t ON t.id = it.tag_id "
            "JOIN images i ON i.id = it.image_id WHERE i.stem = 'a' ORDER BY it.ord")}
        self.assertEqual(list(scores), [t for t, _ in FAKE_TAGS])            # 顺序同模型输出
        self.assertAlmostEqual(scores["1girl"], 0.99)

    def test_skip_done_and_rerun(self):
        self.run_job()
        job = self.run_job()
        self.assertEqual((job["done"], job["skipped"]), (0, 3))
        job = self.run_job(skip_done=False)
        self.assertEqual(job["done"], 3)
        self.assertEqual(len(self.q("SELECT id FROM images")), 3)          # 重跑不产生重复图

    def test_upgrade_placeholder(self):
        conn = db.connect(app.CONFIG["db_path"])
        with conn:
            conn.execute("INSERT INTO images (path, stem, set_name, origin, added) "
                         "VALUES ('import:set1/a', 'a', 'set1', 'import', ?)", (db.now(),))
        conn.close()
        job = self.run_job()
        self.assertEqual(job["report"]["images_upgraded"], 1)
        self.assertEqual(len(self.q("SELECT id FROM images")), 3)
        self.assertEqual(self.q("SELECT path FROM images WHERE stem = 'a'")[0][0], os.path.join(self.imgs, "a.png"))

    def test_file_import_reuses_tagged_image(self):
        self.run_job()
        txt = os.path.join(self.tmp, "txt", "set1")
        os.makedirs(txt)
        with open(os.path.join(txt, "a.txt"), "w", encoding="utf-8") as f:
            f.write("[人物]\n1girl, solo\n")
        r = self.c.post("/api/import/txt", json={"dir": txt}).get_json()
        self.assertEqual((r["images_new"], r["images_updated"]), (0, 1))
        self.assertEqual(len(self.q("SELECT id FROM images")), 3)

    def ids(self):
        return {r["stem"]: r["id"] for r in self.q("SELECT id, stem FROM images")}

    def test_ids_mode_records_file_info_and_thumb(self):
        Image.new("RGB", (40, 20), (1, 2, 3)).save(os.path.join(self.imgs, "a.png"))
        self.run_job()
        conn = db.connect(app.CONFIG["db_path"])
        with conn:
            pid = conn.execute("INSERT INTO images (path, stem, set_name, origin, added) "
                               "VALUES ('import:x/p', 'p', 'x', 'import', ?)", (db.now(),)).lastrowid
        conn.close()
        ids = self.ids()
        job = self.run_job(dir=None, ids=[ids["a"], pid], skip_done=False)
        self.assertEqual((job["phase"], job["total"], job["done"], job["dir"]), ("完成", 1, 1, "选中的 1 张图"))
        row = self.q("SELECT * FROM images WHERE id = ?", ids["a"])[0]
        self.assertEqual((row["w"], row["h"], len(row["sha1"])), (40, 20, 40))
        self.assertEqual(row["size"], os.path.getsize(os.path.join(self.imgs, "a.png")))
        self.assertTrue(row["touched"])
        self.assertTrue(os.path.exists(scanner.thumb_path(app.CONFIG, ids["a"])))
        r = self.c.post("/api/tagger/run", json={"ids": [pid]})
        self.assertEqual(r.status_code, 400, "仅导入记录不能反推")
        self.assertEqual(self.c.post("/api/tagger/run", json={"ids": ["x"]}).status_code, 400)

    def test_edited_images_skipped_unless_overwrite(self):
        self.run_job()
        conn = db.connect(app.CONFIG["db_path"])
        with conn:
            conn.execute("UPDATE images SET edited = '2026-10-11T12:00:00' WHERE stem = 'a'")
        conn.close()
        job = self.run_job(skip_done=False)
        self.assertEqual((job["done"], job["skipped_edited"]), (2, 1))
        self.assertEqual(job["report"]["edited_skipped"], ["set1/a"])
        self.assertEqual(self.q("SELECT edited FROM images WHERE stem = 'a'")[0][0], "2026-10-11T12:00:00")
        job = self.run_job(skip_done=False, overwrite_edited=True)
        self.assertEqual(job["done"], 3)
        self.assertEqual(job["report"]["edited_overwritten"], ["set1/a"])
        self.assertIsNone(self.q("SELECT edited FROM images WHERE stem = 'a'")[0][0])

    def test_file_import_respects_edited(self):
        self.run_job()
        txt = os.path.join(self.tmp, "txt", "set1")
        os.makedirs(txt)
        with open(os.path.join(txt, "a.txt"), "w", encoding="utf-8") as f:
            f.write("[人物]\n1girl, solo\n")
        conn = db.connect(app.CONFIG["db_path"])
        with conn:
            conn.execute("UPDATE images SET edited = '2026-10-11T12:00:00' WHERE stem = 'a'")
        conn.close()
        count = lambda: self.q("SELECT COUNT(*) FROM image_tags it JOIN images i ON i.id = it.image_id "
                               "WHERE i.stem = 'a'")[0][0]
        r = self.c.post("/api/import/txt", json={"dir": txt}).get_json()
        self.assertEqual(r["edited_skipped"], ["set1/a"])
        self.assertEqual(count(), len(FAKE_TAGS), "手工改过的图标签不动")
        r = self.c.post("/api/import/txt", json={"dir": txt, "overwrite_edited": True}).get_json()
        self.assertEqual(r["edited_overwritten"], ["set1/a"])
        self.assertEqual(count(), 2)

    def test_file_import_matches_scanned_image(self):
        conn = db.connect(app.CONFIG["db_path"])
        with conn:
            conn.execute("INSERT INTO images (path, stem, set_name, char_name, origin, added) "
                         "VALUES (?, 'a', 'set1', '角色', 'scan', ?)", (os.path.join(self.imgs, "a.png"), db.now()))
        conn.close()
        txt = os.path.join(self.tmp, "txt", "set1")
        os.makedirs(txt)
        with open(os.path.join(txt, "a.txt"), "w", encoding="utf-8") as f:
            f.write("[人物]\n1girl\n")
        r = self.c.post("/api/import/txt", json={"dir": txt}).get_json()
        self.assertEqual((r["images_new"], r["images_updated"]), (0, 1))
        self.assertEqual(self.q("SELECT COUNT(*) FROM images")[0][0], 1, "不再多出一条占位")

    def test_busy_while_scanning(self):
        scanner.JOB["running"] = True
        try:
            self.assertEqual(self.c.post("/api/tagger/run", json={"dir": self.imgs}).status_code, 409)
        finally:
            scanner.JOB["running"] = False

    def test_failed_image_does_not_stop_job(self):
        Image.new("RGB", (8, 8)).save(os.path.join(self.imgs, "broken.png"))
        job = self.run_job()
        self.assertEqual(job["done"], 3)
        self.assertEqual([f["file"] for f in job["failed"]], ["broken.png"])

    def test_stop_and_busy(self):
        FakeModel.delay = 0.3
        r = self.c.post("/api/tagger/run", json={"dir": self.imgs})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.c.post("/api/tagger/run", json={"dir": self.imgs}).status_code, 409)
        time.sleep(0.1)
        self.c.post("/api/tagger/stop")
        deadline = time.time() + 5
        while tagger.JOB.get("running") and time.time() < deadline:
            time.sleep(0.05)
        job = self.c.get("/api/tagger/status").get_json()["job"]
        self.assertEqual(job["phase"], "已停止")
        self.assertLess(job["done"], 3)

    def test_user_errors(self):
        self.assertEqual(self.c.post("/api/tagger/run", json={"dir": os.path.join(self.tmp, "nope")}).status_code, 400)
        self.assertEqual(self.c.post("/api/tagger/run", json={"dir": self.imgs, "threshold": 1.5}).status_code, 400)
        tagger.deps_error, saved = (lambda: "未安装反推依赖"), tagger.deps_error
        try:
            r = self.c.post("/api/tagger/run", json={"dir": self.imgs})
            self.assertEqual((r.status_code, r.get_json()["error"]), (400, "未安装反推依赖"))
            self.assertEqual(self.c.get("/api/tagger/status").get_json()["deps_error"], "未安装反推依赖")
        finally:
            tagger.deps_error = saved


class MigrationTest(unittest.TestCase):
    def test_v1_database_upgrades_in_place(self):
        tmp = tempfile.mkdtemp(prefix="tagcodex_mig_")
        try:
            path = os.path.join(tmp, "old.db")
            old = db.SCHEMA.replace(",\n  tagger    TEXT,\n  tagged    TEXT", "").replace("  score    REAL,\n", "")
            for col in ("size", "mtime", "missing", "touched", "edited", "category", "zone", "dir"):   # schema 3、4、6、8 的列
                old = re.sub(r",\n  %s +[A-Z][^,\n]*(?=[,\n])" % col, "", old)
            self.assertNotIn("  touched ", old)
            conn = sqlite3.connect(path)
            conn.executescript(old)
            conn.execute("INSERT INTO images (path, added) VALUES ('import:x/y', '2026-01-01')")
            conn.commit()
            conn.close()
            conn = db.connect(path)
            self.assertNotIn("score", [r["name"] for r in conn.execute("PRAGMA table_info(image_tags)")])
            db.init_db(conn)
            db.init_db(conn)
            self.assertIn("score", [r["name"] for r in conn.execute("PRAGMA table_info(image_tags)")])
            self.assertIn("tagged", [r["name"] for r in conn.execute("PRAGMA table_info(images)")])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM images").fetchone()[0], 1)
            self.assertIn("edited", [r["name"] for r in conn.execute("PRAGMA table_info(images)")])
            self.assertIn("dir", [r["name"] for r in conn.execute("PRAGMA table_info(images)")])
            self.assertIn("idx_images_dir", [r["name"] for r in conn.execute("PRAGMA index_list(images)")])
            self.assertEqual(conn.execute("SELECT touched FROM images").fetchone()[0], "2026-01-01")
            conn.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_v7_fills_dir_from_zone(self):
        """schema 7 → 8：按旧的 区 / 分级 换算 dir（收件箱、深一层的套图也对），换算不出来的留空，先备份。"""
        tmp = tempfile.mkdtemp(prefix="tagcodex_mig8_")
        try:
            path = os.path.join(tmp, "a.db")
            conn = db.connect(path)
            db.init_db(conn)
            rows = [  # (路径, 区, 分级, 期望的 dir)
                (r"D:\g\反推\nsfw\角色A\套1\a.png", "反推", "nsfw", "反推/nsfw/角色A/套1"),
                ("D:/g/反推/nsfw/角色A/套1/深一层/b.png", "反推", "nsfw", "反推/nsfw/角色A/套1/深一层"),
                (r"D:\g\生成\sfw\未反推\c.png", "生成", "sfw", "生成/sfw/未反推"),
                (r"D:\生成\sfw\g\生成\sfw\d.png", "生成", "sfw", "生成/sfw"),      # 根路径里也有同名两段，取最后一处
                (r"D:\g\别的\e.png", "反推", "nsfw", None),                            # 换算不出来
            ]
            with conn:
                for p, z, c, _ in rows:
                    conn.execute("INSERT INTO images (path, zone, category, added) VALUES (?, ?, ?, '2026')", (p, z, c))
                conn.execute("INSERT INTO images (path, set_name, added) VALUES ('import:套/x', '套', '2026')")
                conn.execute("UPDATE meta SET value = '7' WHERE key = 'schema_version'")
            db.init_db(conn)
            got = [r[0] for r in conn.execute("SELECT dir FROM images ORDER BY id")]
            self.assertEqual(got, [r[3] for r in rows] + [None])
            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0], "8")
            conn.close()
            self.assertTrue(os.path.exists(path + ".v7.bak"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

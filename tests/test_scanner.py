# -*- coding: utf-8 -*-
"""扫图库测试：临时目录里生成小图，测任意层级登记（dir）、增量、内容变化、移动、缺失、重复、回填占位、停止、互斥。"""
import os
import shutil
import sys
import tempfile
import time
import unittest

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db       # noqa: E402
import scanner  # noqa: E402
import tagger   # noqa: E402


class ScannerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tagcodex_scan_")
        self.root = os.path.join(self.tmp, "gallery")
        self.zroot = os.path.join(self.root, "反推")        # 用例默认都在「反推」这个文件夹里
        self.cfg = {"db_path": os.path.join(self.tmp, "t.db"), "thumbs_dir": os.path.join(self.tmp, "thumbs")}
        self.conn = db.connect(self.cfg["db_path"])
        db.init_db(self.conn)
        self.color = 0

    def tearDown(self):
        self.conn.close()
        scanner.JOB.clear()
        scanner.JOB["running"] = False
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------- 工具 ----------
    def img(self, rel, size=(600, 400), exif=None, base=None):
        """在「反推」文件夹下（base 给了就在 base 下）生成一张颜色各不相同的图（保证 sha1 不同）。"""
        path = os.path.join(base or self.zroot, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.color += 1
        im = Image.new("RGB", size, (self.color * 7 % 256, self.color * 13 % 256, self.color * 29 % 256))
        if exif:
            im.save(path, exif=exif)
        else:
            im.save(path)
        return path

    def scan(self):
        scanner.JOB.clear()
        scanner.JOB.update(running=True, stop=False)
        scanner._run(self.cfg, self.root)
        self.assertEqual(scanner.JOB["phase"], "完成", scanner.JOB.get("error"))
        return scanner.JOB["report"]

    def row(self, path):
        return self.conn.execute("SELECT * FROM images WHERE path = ?", (path,)).fetchone()

    def add_tag(self, image_id, word):
        unk = db.bucket_ids(self.conn)[db.UNKNOWN]
        with self.conn:
            tid = self.conn.execute("INSERT INTO tags (word, bucket_id, created) VALUES (?, ?, '2026')",
                                    (word, unk)).lastrowid
            self.conn.execute("INSERT INTO image_tags (image_id, tag_id) VALUES (?, ?)", (image_id, tid))

    def placeholder(self, set_name, stem):
        with self.conn:
            return self.conn.execute(
                "INSERT INTO images (path, stem, set_name, origin, added) VALUES (?, ?, ?, 'import', '2026')",
                ("import:%s/%s" % (set_name, stem), stem, set_name)).lastrowid

    # ---------- 用例 ----------
    def test_register_and_thumbs(self):
        a = self.img("nsfw/角色A/套1/a.png")
        b = self.img("nsfw/角色A/套1/深一层/b.jpg", size=(300, 900))
        r = self.scan()
        self.assertEqual((r["new"], r["files"]), (2, 2))
        ra, rb = self.row(a), self.row(b)
        self.assertEqual((ra["dir"], ra["set_name"], ra["stem"], ra["origin"]), ("反推/nsfw/角色A/套1", "套1", "a", "scan"))
        self.assertEqual((rb["dir"], rb["set_name"], rb["w"], rb["h"]), ("反推/nsfw/角色A/套1/深一层", "深一层", 300, 900))
        self.assertEqual(len(ra["sha1"]), 40)
        self.assertIsNone(ra["touched"])
        with Image.open(scanner.thumb_path(self.cfg, rb["id"])) as t:
            self.assertEqual(max(t.size), scanner.THUMB_SIZE)
            self.assertEqual(t.format, "WEBP")

    def test_any_level_registered(self):
        """根目录下、任意一层直接放的图都登记；dir 是相对图库根的路径，set_name 是所在文件夹名。"""
        top = self.img("top.png", base=self.root)
        one = self.img("one.png")
        deep = self.img("a/b/c/d/e/deep.png")
        inbox = self.img("nsfw/未反推/x.png")             # 原来的收件箱现在只是普通文件夹
        r = self.scan()
        self.assertEqual((r["new"], r["files"]), (4, 4))
        place = lambda p: (self.row(p)["dir"], self.row(p)["set_name"])
        self.assertEqual(place(top), ("", "gallery"), "根目录下的图 dir 为空，套图名取图库根的名字")
        self.assertEqual(place(one), ("反推", "反推"))
        self.assertEqual(place(deep), ("反推/a/b/c/d/e", "e"))
        self.assertEqual(place(inbox), ("反推/nsfw/未反推", "未反推"))
        self.assertNotIn("outside_sets", r)

    def test_move_to_other_folder_keeps_tags_and_edits(self):
        a = self.img("nsfw/未反推/a.png")
        self.scan()
        old = self.row(a)
        self.add_tag(old["id"], "1girl")
        with self.conn:
            self.conn.execute("UPDATE images SET edited = '2026-10-11T12:00:00' WHERE id = ?", (old["id"],))
        moved = os.path.join(self.zroot, "非nsfw", "角色A", "套1", "a.png")
        os.makedirs(os.path.dirname(moved))
        shutil.move(a, moved)
        r = self.scan()
        self.assertEqual((r["moved"], r["new"]), ([{"from": a, "to": moved}], 0))
        new = self.row(moved)
        self.assertEqual((new["id"], new["dir"], new["set_name"], new["edited"]),
                         (old["id"], "反推/非nsfw/角色A/套1", "套1", "2026-10-11T12:00:00"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM image_tags WHERE image_id = ?",
                                           (new["id"],)).fetchone()[0], 1)

    def scan_sub(self, sub):
        scanner.JOB.clear()
        scanner.JOB.update(running=True, stop=False)
        scanner._run(self.cfg, self.root, scanner._sub_dir(self.root, sub))
        self.assertEqual(scanner.JOB["phase"], "完成", scanner.JOB.get("error"))
        return scanner.JOB["report"]

    def test_scan_sub_dir(self):
        a = self.img("nsfw/角色A/套1/a.png")
        b = self.img("nsfw/角色B/套2/b.png")
        self.scan()
        os.remove(b)                                       # 子目录外的变化这次不判
        c = self.img("nsfw/角色A/套3/c.png")
        r = self.scan_sub("反推/nsfw/角色A")
        self.assertEqual((r["files"], r["new"], r["unchanged"], r["missing"]), (2, 1, 1, 0))
        self.assertEqual((self.row(c)["dir"], self.row(c)["set_name"]), ("反推/nsfw/角色A/套3", "套3"),
                         "dir 仍按图库根算")
        self.assertEqual(self.row(b)["missing"], 0)
        os.remove(c)
        self.assertEqual(self.scan_sub(os.path.join(self.zroot, "nsfw", "角色A"))["missing"], 1)
        self.assertEqual(self.row(a)["missing"], 0)

    def test_scan_sub_dir_picks_up_move(self):
        a = self.img("nsfw/未反推/a.png")
        self.scan()
        old = self.row(a)
        moved = os.path.join(self.zroot, "nsfw", "角色B", "套图A", "a.png")
        os.makedirs(os.path.dirname(moved))
        shutil.move(a, moved)
        r = self.scan_sub(os.path.join(self.zroot, "nsfw", "角色B"))
        self.assertEqual(r["moved"], [{"from": a, "to": moved}])
        self.assertEqual((self.row(moved)["id"], self.row(moved)["dir"]), (old["id"], "反推/nsfw/角色B/套图A"))

    def test_sub_dir_validation(self):
        os.makedirs(os.path.join(self.zroot, "nsfw", "角色A"))
        self.assertEqual(scanner._sub_dir(self.root, "反推/nsfw/角色A"), os.path.join(self.zroot, "nsfw", "角色A"))
        self.assertEqual(scanner._sub_dir(self.root, '"%s"' % os.path.join(self.zroot, "nsfw")),
                         os.path.join(self.zroot, "nsfw"))
        for same in ("", None, self.root, self.root + os.sep):
            self.assertIsNone(scanner._sub_dir(self.root, same), "留空或等于根就是扫整个图库")
        for bad in (self.tmp, os.path.join(self.root, "..", "别处"), "反推/nsfw/没有这个"):
            with self.assertRaises(scanner.importer.ImportFailed):
                scanner._sub_dir(self.root, bad)

    def test_incremental_and_changed(self):
        a = self.img("nsfw/角色A/套1/a.png")
        self.scan()
        old = self.row(a)
        os.remove(scanner.thumb_path(self.cfg, old["id"]))
        r = self.scan()
        self.assertEqual((r["new"], r["unchanged"], r["changed"]), (0, 1, []))
        self.assertTrue(os.path.exists(scanner.thumb_path(self.cfg, old["id"])), "缺的缩略图要补")

        Image.new("RGB", (64, 64), (1, 2, 3)).save(a)
        os.utime(a, (time.time() + 5, time.time() + 5))
        r = self.scan()
        self.assertEqual(r["changed"], [a])
        new = self.row(a)
        self.assertEqual(new["id"], old["id"])
        self.assertNotEqual(new["sha1"], old["sha1"])
        self.assertEqual((new["w"], new["h"]), (64, 64))

    def test_move_keeps_tags(self):
        a = self.img("nsfw/角色A/套1/a.png")
        self.scan()
        old = self.row(a)
        self.add_tag(old["id"], "1girl")
        moved = os.path.join(self.zroot, "nsfw", "角色B", "套2", "a2.png")
        os.makedirs(os.path.dirname(moved))
        shutil.move(a, moved)
        r = self.scan()
        self.assertEqual(r["moved"], [{"from": a, "to": moved}])
        self.assertEqual((r["new"], r["missing"]), (0, 0))
        new = self.row(moved)
        self.assertEqual((new["id"], new["dir"], new["set_name"], new["stem"]), (old["id"], "反推/nsfw/角色B/套2", "套2", "a2"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM image_tags WHERE image_id = ?",
                                           (new["id"],)).fetchone()[0], 1)

    def test_missing_and_returned(self):
        a = self.img("nsfw/角色A/套1/a.png")
        self.img("nsfw/角色A/套1/b.png")
        self.scan()
        keep = a + ".bak"
        os.rename(a, keep)
        r = self.scan()
        self.assertEqual(r["missing"], 1)
        self.assertEqual(self.row(a)["missing"], 1)
        os.rename(keep, a)
        r = self.scan()
        self.assertEqual((r["returned"], r["missing"]), (1, 0))
        self.assertEqual(self.row(a)["missing"], 0)

    def test_outside_root_not_marked_missing(self):
        self.img("nsfw/角色A/套1/a.png")
        with self.conn:
            self.conn.execute("INSERT INTO images (path, added) VALUES (?, '2026')",
                              (os.path.join(self.tmp, "别处", "x.png"),))
        self.assertEqual(self.scan()["missing"], 0)

    def test_duplicate_content_registers_first_only(self):
        a = self.img("nsfw/角色A/套1/a.png")
        dup = os.path.join(self.zroot, "nsfw", "角色A", "套2", "copy.png")
        os.makedirs(os.path.dirname(dup))
        shutil.copyfile(a, dup)
        r = self.scan()
        self.assertEqual(r["new"], 1)
        self.assertEqual(r["duplicates"], [{"file": dup, "same_as": a}])
        self.assertIsNone(self.row(dup))

    def test_backfill_placeholder_same_set(self):
        pid = self.placeholder("套1", "a")
        self.add_tag(pid, "bikini")
        a = self.img("nsfw/角色A/套1/a.png")
        r = self.scan()
        self.assertEqual((r["new"], r["backfilled"]), (0, [a]))
        row = self.row(a)
        self.assertEqual((row["id"], row["origin"], row["dir"]), (pid, "import", "反推/nsfw/角色A/套1"))
        self.assertTrue(os.path.exists(scanner.thumb_path(self.cfg, pid)))

    def test_backfill_by_stem_and_ambiguous(self):
        pid = self.placeholder("别的目录", "e")
        self.placeholder("x", "f")
        self.placeholder("y", "f")
        e = self.img("nsfw/角色A/套1/e.png")
        f = self.img("nsfw/角色A/套1/f.png")
        r = self.scan()
        self.assertEqual(r["backfilled"], [e])
        self.assertEqual(self.row(e)["id"], pid)
        self.assertEqual(self.row(e)["set_name"], "套1")
        self.assertEqual(r["backfill_ambiguous"], [{"file": f, "candidates": ["import:x/f", "import:y/f"]}])
        self.assertEqual(self.row(f)["origin"], "scan")
        self.assertEqual(r["new"], 1)

    def test_adopt_image_tagged_before_scan(self):
        a = self.img("nsfw/角色A/套1/a.png")
        with self.conn:
            iid = self.conn.execute("INSERT INTO images (path, stem, set_name, origin, added) "
                                    "VALUES (?, 'a', '套1', 'tagged', '2026')", (a,)).lastrowid
        r = self.scan()
        self.assertEqual((r["adopted"], r["changed"], r["new"]), (1, [], 0))
        self.assertEqual((self.row(a)["id"], self.row(a)["dir"]), (iid, "反推/nsfw/角色A/套1"))

    def test_exif_rotation_swaps_size(self):
        exif = Image.Exif()
        exif[scanner.EXIF_ORIENTATION] = 6
        a = self.img("nsfw/角色A/套1/r.jpg", size=(600, 400), exif=exif.tobytes())
        self.scan()
        self.assertEqual((self.row(a)["w"], self.row(a)["h"]), (400, 600))

    def test_broken_file_reported(self):
        bad = os.path.join(self.zroot, "nsfw", "角色A", "套1", "bad.png")
        os.makedirs(os.path.dirname(bad))
        with open(bad, "wb") as f:
            f.write(b"not an image")
        r = self.scan()
        self.assertEqual([x["file"] for x in r["failed"]], [bad])
        self.assertIsNone(self.row(bad))

    def test_stop_skips_missing_marking(self):
        a = self.img("nsfw/角色A/套1/a.png")
        b = self.img("nsfw/角色A/套1/b.png")
        self.scan()
        os.remove(a)
        for _ in range(2):               # 第一轮还剩 b（循环里停）；第二轮一个文件都没有（列完就停）
            scanner.JOB.clear()
            scanner.JOB.update(running=True, stop=True)
            scanner._run(self.cfg, self.root)
            self.assertEqual(scanner.JOB["phase"], "已停止")
            self.assertEqual(self.row(a)["missing"], 0, "没扫完不能判定文件已不在")
            if os.path.exists(b):
                os.remove(b)

    def test_unchanged_row_gets_dir(self):
        """schema 8 升级时没换算出 dir 的图：文件没动过，再扫一次补上 dir，标签和关联都不动。"""
        a = self.img("nsfw/角色A/套1/a.png")
        self.scan()
        iid = self.row(a)["id"]
        self.add_tag(iid, "1girl")
        with self.conn:
            self.conn.execute("UPDATE images SET dir = NULL WHERE id = ?", (iid,))
        r = self.scan()
        self.assertEqual((r["unchanged"], r["new"]), (1, 0))
        self.assertEqual((self.row(a)["id"], self.row(a)["dir"]), (iid, "反推/nsfw/角色A/套1"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM image_tags WHERE image_id = ?", (iid,)).fetchone()[0], 1)

    def test_start_validation_and_busy(self):
        with self.assertRaises(scanner.importer.ImportFailed):
            scanner.start(self.cfg, {"root": os.path.join(self.tmp, "没有")})
        os.makedirs(self.root)
        saved = dict(tagger.JOB)
        tagger.JOB["running"] = True
        try:
            with self.assertRaises(tagger.TaggerBusy):
                scanner.start(self.cfg, {"root": self.root})
        finally:
            tagger.JOB.clear()
            tagger.JOB.update(saved)


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""TagCodex（本地提示词管理器）：Flask 入口，路由都在这。"""
import json
import os
import shutil
import sys
import threading
import webbrowser

from flask import Flask, Response, abort, g, jsonify, request, send_file, send_from_directory

import composer
import db
import drafts
import gallery
import importer
import presets
import scanner
import tagdict
import tagger
import tagops
import themes

ROOT = os.path.dirname(os.path.abspath(__file__))


def _abs(p):
    return os.path.normpath(os.path.join(ROOT, p)) if p and not os.path.isabs(p) else p


def load_config():
    path = os.path.join(ROOT, "config.json")
    if not os.path.exists(path):                      # 首次运行：从模板生成，之后用户自己改
        shutil.copyfile(os.path.join(ROOT, "config.example.json"), path)
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # 相对路径一律相对项目根目录
    for k in ("db_path", "tag_dict_path", "thumbs_dir", "composer_output_dir", "danbooru_xlsx"):
        cfg[k] = _abs(cfg.get(k))
    if cfg.get("tagger"):
        cfg["tagger"]["models_dir"] = _abs(cfg["tagger"].get("models_dir"))
    return cfg


CONFIG = load_config()
THUMB_MAX_AGE = 365 * 24 * 3600
app = Flask(__name__, static_folder="static", static_url_path="/static")


def get_db():
    if "db" not in g:
        g.db = db.connect(CONFIG["db_path"])
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


@app.get("/")
def index():
    return send_from_directory(os.path.join(ROOT, "static"), "index.html")


@app.errorhandler(tagops.OpError)
@app.errorhandler(importer.ImportFailed)
def user_error(e):
    return jsonify({"error": str(e)}), 400


@app.after_request
def auto_export(resp):
    """词典有改动就防抖重写 tag_dict.json，不用手动点导出。"""
    if (request.method != "GET" and request.path.startswith("/api/")
            and request.path != "/api/tagdict/export" and resp.status_code < 400):
        tagdict.schedule(CONFIG["db_path"], CONFIG["tag_dict_path"])
    return resp


@app.errorhandler(presets.PresetExists)
def preset_exists(e):
    return jsonify({"error": str(e), "existing": e.existing}), 409


@app.errorhandler(presets.PresetGroupConflict)
def preset_group_conflict(e):
    return jsonify({"error": str(e), "conflicts": e.conflicts}), 409


@app.errorhandler(composer.Conflict)
def composer_conflict(e):
    return jsonify({"error": str(e), "conflicts": e.files}), 409


@app.errorhandler(tagger.TaggerBusy)
def busy_error(e):
    return jsonify({"error": str(e)}), 409


def body():
    return request.get_json(silent=True) or {}


@app.get("/api/health")
def health():
    return jsonify({"ok": True})


@app.get("/api/stats")
def api_stats():
    s = tagops.stats(get_db())
    s["tagdict"] = tagdict.LAST
    return jsonify(s)


# ---------------- 段 ----------------
@app.get("/api/buckets")
def api_buckets():
    return jsonify(tagops.list_buckets(get_db()))


@app.patch("/api/buckets")
def api_buckets_patch():
    return jsonify(tagops.patch_buckets(get_db(), body()))


# ---------------- 词 ----------------
@app.get("/api/tags")
def api_tags():
    return jsonify(tagops.list_tags(get_db(), request.args))


@app.patch("/api/tags/<int:tag_id>")
def api_tag_patch(tag_id):
    tagops.update_tag(get_db(), tag_id, body())
    return jsonify({"ok": True})


@app.get("/api/tags/<int:tag_id>/images")
def api_tag_images(tag_id):
    a = request.args
    return jsonify(tagops.tag_images(get_db(), tag_id, a.get("page", 1), a.get("size", 60)))


@app.post("/api/tags/assign")
def api_assign():
    b = body()
    return jsonify(tagops.assign(get_db(), b.get("ids"), b.get("bucket")))


@app.post("/api/tags/block")
def api_block():
    b = body()
    return jsonify(tagops.block(get_db(), b.get("ids"), b.get("blocked", True)))


@app.post("/api/tags/add")
def api_add():
    b = body()
    return jsonify(tagops.add_words(get_db(), b.get("bucket"), b.get("text"), b.get("items")))


@app.post("/api/tags/split")
def api_split():
    return jsonify(tagops.split_text(get_db(), body().get("text")))


@app.post("/api/tags/alias")
def api_alias():
    b = body()
    if b.get("remove"):
        tagops.remove_alias(get_db(), b["remove"])
        return jsonify({"ok": True})
    return jsonify(tagops.merge_alias(get_db(), b.get("main_id"), b.get("ids")))


# ---------------- 撤销 ----------------
@app.get("/api/oplog")
def api_oplog():
    return jsonify(tagops.list_ops(get_db()))


@app.post("/api/oplog/undo")
def api_undo():
    return jsonify(tagops.undo_last(get_db()))


# ---------------- 导入 ----------------
@app.post("/api/import/xlsx")
def api_import_xlsx():
    b = body()
    return jsonify(importer.import_xlsx(get_db(), (b.get("path") or "").strip().strip('"'),
                                        bool(b.get("file_wins")), bool(b.get("overwrite_edited"))))


@app.post("/api/import/txt")
def api_import_txt():
    b = body()
    return jsonify(importer.import_txt_dir(get_db(), (b.get("dir") or "").strip().strip('"'),
                                           bool(b.get("file_wins")), bool(b.get("overwrite_edited"))))


@app.post("/api/import/ref")
def api_import_ref():
    return jsonify(importer.import_ref(get_db(), CONFIG["danbooru_xlsx"]))


# ---------------- 反推 ----------------
@app.get("/api/tagger/status")
def api_tagger_status():
    return jsonify(tagger.status(CONFIG))


@app.post("/api/tagger/run")
def api_tagger_run():
    return jsonify(tagger.start(CONFIG, body()))


@app.post("/api/tagger/stop")
def api_tagger_stop():
    return jsonify({"stopping": tagger.stop()})


# ---------------- 扫图库 ----------------
@app.get("/api/scan/status")
def api_scan_status():
    return jsonify(scanner.status(CONFIG))


@app.post("/api/scan")
def api_scan():
    """{root?, dir?}：root 缺省用 config 的 gallery_root；dir 只扫图库里的这个子目录。"""
    return jsonify(scanner.start(CONFIG, body()))


@app.post("/api/scan/stop")
def api_scan_stop():
    return jsonify({"stopping": scanner.stop()})


# ---------------- 图库 ----------------
@app.get("/api/images")
def api_images():
    return jsonify(gallery.list_images(get_db(), request.args))


@app.get("/api/images/tree")
def api_images_tree():
    return jsonify(gallery.tree(get_db()))


@app.get("/api/images/stats")
def api_images_stats():
    """筛选参数同 /api/images：统计整个筛选结果每段的词频。"""
    return jsonify(gallery.stats(get_db(), request.args))


@app.get("/api/images/<int:image_id>")
def api_image(image_id):
    return jsonify(gallery.detail(get_db(), image_id, request.args))


@app.post("/api/images/<int:image_id>/tags")
def api_image_tags(image_id):
    return jsonify(gallery.edit_tags(get_db(), image_id, body()))


@app.post("/api/images/<int:image_id>/links")
def api_image_links(image_id):
    """{role: gen | src, add | remove: [ids]}：管理这张图的生成图 / 原图。"""
    return jsonify(gallery.links(get_db(), image_id, body()))


# ---------------- 主题 ----------------
@app.get("/api/themes")
def api_themes():
    return jsonify(themes.list_themes(get_db()))


@app.post("/api/themes")
def api_theme_create():
    return jsonify(themes.create(get_db(), body()))


@app.post("/api/themes/preview")
def api_theme_preview():
    return jsonify(themes.preview(get_db(), body()))


@app.patch("/api/themes/<int:theme_id>")
def api_theme_patch(theme_id):
    return jsonify(themes.update(get_db(), theme_id, body()))


@app.delete("/api/themes/<int:theme_id>")
def api_theme_delete(theme_id):
    return jsonify(themes.delete(get_db(), theme_id))


@app.post("/api/themes/<int:theme_id>/members")
def api_theme_members(theme_id):
    """{add | remove | restore: [image ids]}"""
    return jsonify(themes.members(get_db(), theme_id, body()))


# ---------------- 预设 ----------------
@app.get("/api/presets")
def api_presets():
    return jsonify(presets.list_presets(get_db(), request.args.get("bucket")))


@app.post("/api/presets")
def api_preset_create():
    return jsonify(presets.create(get_db(), body()))


@app.post("/api/presets/group")
def api_preset_group_create():
    return jsonify(presets.create_group(get_db(), body()))


@app.put("/api/presets/group")
def api_preset_group_save():
    return jsonify(presets.save_group(get_db(), body()))


@app.delete("/api/presets/group")
def api_preset_group_delete():
    return jsonify(presets.delete_group(get_db(), body().get("name")))


@app.get("/api/presets/groups")
def api_preset_groups():
    return jsonify(presets.groups(get_db()))


@app.get("/api/presets/groups/match")
def api_preset_groups_match():
    return jsonify(presets.match_counts(get_db(), presets.match_min(request.args.get("min"))))


@app.patch("/api/presets/<int:preset_id>")
def api_preset_patch(preset_id):
    return jsonify(presets.update(get_db(), preset_id, body()))


@app.delete("/api/presets/<int:preset_id>")
def api_preset_delete(preset_id):
    return jsonify(presets.delete(get_db(), preset_id))


# ---------------- 组合器 ----------------
@app.get("/api/composer/config")
def api_composer_config():
    return jsonify({"output_dir": CONFIG.get("composer_output_dir") or "", "max_source": composer.MAX_SOURCE})


@app.post("/api/composer/preview")
def api_composer_preview():
    """{source: {ids} | {filter}, slots, page?, size?}"""
    return jsonify(composer.preview(get_db(), body()))


@app.post("/api/composer/compose")
def api_composer_compose():
    """{slots} → 单条编辑的结果 {segments, prompt, all_text}，不需要来源"""
    return jsonify(composer.compose(get_db(), body()))


@app.post("/api/composer/stats")
def api_composer_stats():
    return jsonify(composer.source_stats(get_db(), body()))


@app.post("/api/composer/export")
def api_composer_export():
    """{source, slots, name, dir, txt, xlsx, overwrite}；有同名文件又没让覆盖时 409"""
    return jsonify(composer.export(get_db(), CONFIG, body()))


@app.post("/api/composer/apply")
def api_composer_apply():
    """{source, slots}：组合结果写进空白套图的条"""
    return jsonify(drafts.apply(get_db(), body()))


# ---------------- 空白套图 ----------------
@app.get("/api/drafts")
def api_drafts():
    return jsonify(drafts.list_drafts(get_db()))


@app.post("/api/drafts")
def api_draft_create():
    return jsonify(drafts.create(get_db(), body()))


@app.patch("/api/drafts")
def api_draft_rename():
    return jsonify(drafts.rename(get_db(), body()))


@app.delete("/api/drafts")
def api_draft_delete():
    return jsonify(drafts.delete(get_db(), body()))


@app.post("/api/drafts/items")
def api_draft_add_items():
    return jsonify(drafts.add_items(get_db(), body()))


@app.delete("/api/drafts/items")
def api_draft_delete_items():
    return jsonify(drafts.delete_items(get_db(), body()))


def _real_path(image_id):
    """只发库里登记过的真实文件，不接受任意路径。"""
    r = get_db().execute("SELECT path FROM images WHERE id = ?", (image_id,)).fetchone()
    if r is None or r["path"].startswith("import:") or not os.path.isfile(r["path"]):
        abort(404)
    return r["path"]


@app.get("/image/<int:image_id>")
def image_file(image_id):
    return send_file(_real_path(image_id), max_age=0)


@app.get("/thumb/<int:image_id>.webp")
def thumb_file(image_id):
    # URL 带 ?v=<sha1 前 8 位>，内容变了地址就变，所以可以长缓存
    path = scanner.thumb_path(CONFIG, image_id)
    if not os.path.exists(path):
        path = scanner.ensure_thumb(CONFIG, image_id, _real_path(image_id))
        if path is None:
            abort(404)
    return send_file(path, mimetype="image/webp", max_age=THUMB_MAX_AGE)


# ---------------- 导出 ----------------
@app.post("/api/tagdict/export")
def api_tagdict_export():
    try:
        return jsonify(tagdict.export(CONFIG["db_path"], CONFIG["tag_dict_path"]))
    except OSError:
        return jsonify({"error": tagdict.LAST["error"]}), 500


@app.get("/api/export/full")
def api_export_full():
    name, mime, data = tagdict.export_full(get_db(), request.args.get("format", "csv"))
    return Response(data, mimetype=mime,
                    headers={"Content-Disposition": "attachment; filename=%s" % name})


def init():
    importer.load_rules()
    importer.load_char_list(CONFIG.get("tagger"))
    conn = db.connect(CONFIG["db_path"])
    db.init_db(conn)
    importer.run_pending_reclassify(conn)
    conn.close()
    try:
        tagdict.export(CONFIG["db_path"], CONFIG["tag_dict_path"])
    except OSError:
        pass                             # 错误在 /api/stats 的 tagdict.error 里


if __name__ == "__main__":
    init()
    url = "http://%s:%d/" % (CONFIG["host"], CONFIG["port"])
    if "--open" in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host=CONFIG["host"], port=CONFIG["port"], debug=False)

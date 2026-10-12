# -*- coding: utf-8 -*-
"""
管理器内置反推：在 CPU 上跑 WD14 onnx 模型，结果按导入器同一套规则入库。

预处理照 comfyui-wd14-tagger 的 tag() 和 ComfyUI LoadImage 复刻，保证与 ComfyUI 里反推逐词一致。
Model 的读表和 predict 的预处理、取词改写自 pythongosssss/ComfyUI-WD14-Tagger 的 wd14tagger.py
（MIT License，Copyright (c) 2024 pythongosssss），见 THIRD_PARTY.md。
模型文件只读（install.bat 下载到 data/models，或者 config 指向 comfyui-wd14-tagger 的 models 目录），这里不下载、不改动。
onnxruntime / numpy 是可选依赖（requirements-tagger.txt），缺了只禁用反推，其他功能不受影响。
"""
import csv
import os
import threading
import time

from PIL import Image, ImageOps

import db
import importer
import tagdict

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
EXPORT_EVERY = 20            # 每反推这么多张重写一次 tag_dict.json


class TaggerBusy(Exception):
    """已有反推任务在跑。"""


def deps_error():
    try:
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
    except ImportError as e:
        return ("未安装反推依赖，在项目目录执行：.venv\\Scripts\\python.exe -m pip install -r "
                "requirements-tagger.txt（%s）" % e)
    return None


def list_models(models_dir):
    """模型目录里 onnx + csv 成对的才算可用（与 WD14 节点的判断一致）。"""
    try:
        names = os.listdir(models_dir or "")
    except OSError:
        return []
    return sorted(os.path.splitext(n)[0] for n in names
                  if n.endswith(".onnx") and os.path.exists(os.path.join(models_dir, n[:-5] + ".csv")))


# ---------------------------------------------------------------------
#  推理
# ---------------------------------------------------------------------
def load_like_comfy(path):
    """ComfyUI LoadImage：exif 校正 → RGB → float32/255；WD14 节点再 *255 截断回 uint8。
    这个往返会让个别像素差 1，不复刻的话少数词会和 ComfyUI 对不上。"""
    import numpy as np
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    arr = np.array(img).astype(np.float32) / 255.0
    return Image.fromarray(np.array(arr * 255, dtype=np.uint8))


class Model:
    def __init__(self, models_dir, name):
        import onnxruntime as ort
        self.name = name
        self.session = ort.InferenceSession(os.path.join(models_dir, name + ".onnx"),
                                            providers=["CPUExecutionProvider"])
        self.input = self.session.get_inputs()[0]
        self.output = self.session.get_outputs()[0].name
        self.size = self.input.shape[1]
        self.tags, self.general_index, self.character_index = [], None, None
        with open(os.path.join(models_dir, name + ".csv"), "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            next(reader)
            for row in reader:
                if self.general_index is None and row[2] == "0":
                    self.general_index = reader.line_num - 2
                elif self.character_index is None and row[2] == "4":
                    self.character_index = reader.line_num - 2
                self.tags.append(row[1].replace("_", " "))

    def predict(self, path, threshold, character_threshold, exclude_tags=""):
        """返回 [(标签, 置信度)]，角色词在前，括号已转义，与 WD14 节点输出一致。"""
        import numpy as np
        image = load_like_comfy(path)
        h = self.size
        ratio = float(h) / max(image.size)
        new_size = tuple(int(x * ratio) for x in image.size)
        image = image.resize(new_size, Image.LANCZOS)
        square = Image.new("RGB", (h, h), (255, 255, 255))
        square.paste(image, ((h - new_size[0]) // 2, (h - new_size[1]) // 2))
        x = np.expand_dims(np.array(square).astype(np.float32)[:, :, ::-1], 0)   # RGB → BGR
        probs = self.session.run([self.output], {self.input.name: x})[0][0]
        result = list(zip(self.tags, probs))
        general = [t for t in result[self.general_index:self.character_index] if t[1] > threshold]
        character = [t for t in result[self.character_index:] if t[1] > character_threshold]
        remove = [s.strip() for s in (exclude_tags or "").lower().split(",")]
        return [(t.replace("(", "\\(").replace(")", "\\)"), float(p))
                for t, p in character + general if t not in remove]


# ---------------------------------------------------------------------
#  后台任务：同一时间只跑一个
# ---------------------------------------------------------------------
_lock = threading.Lock()
JOB = {"running": False}


def _snapshot():
    with _lock:
        return dict(JOB)


def status(cfg):
    tcfg = cfg.get("tagger") or {}
    return {
        "deps_error": deps_error(),
        "models_dir": tcfg.get("models_dir", ""),
        "models": list_models(tcfg.get("models_dir")),
        "defaults": {k: tcfg.get(k) for k in ("model", "threshold", "character_threshold", "exclude_tags")},
        "job": _snapshot(),
    }


def _float01(v, name):
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise importer.ImportFailed("%s 必须是 0 到 1 之间的数" % name)
    if not 0 <= f <= 1:
        raise importer.ImportFailed("%s 必须是 0 到 1 之间的数" % name)
    return f


def start(cfg, opts):
    err = deps_error()
    if err:
        raise importer.ImportFailed(err)
    tcfg = cfg.get("tagger") or {}
    models_dir = tcfg.get("models_dir", "")
    model = opts.get("model") or tcfg.get("model")
    if model not in list_models(models_dir):
        raise importer.ImportFailed("模型不存在（要求 onnx 和 csv 成对）：%s ，目录：%s" % (model, models_dir))
    if opts.get("ids") is not None:
        files, label = _files_of_ids(cfg, opts["ids"])
    else:
        files, label = _files_of_dir(opts.get("dir"))
    params = {
        "model": model,
        "threshold": _float01(opts.get("threshold", tcfg.get("threshold", 0.35)), "阈值"),
        "character_threshold": _float01(opts.get("character_threshold", tcfg.get("character_threshold", 0.8)), "角色阈值"),
        "exclude_tags": opts.get("exclude_tags", tcfg.get("exclude_tags", "")) or "",
        "skip_done": bool(opts.get("skip_done", True)),
        "overwrite_edited": bool(opts.get("overwrite_edited", False)),
    }
    import scanner                       # scanner 依赖本模块，放这里避免循环导入
    if scanner.JOB.get("running"):
        raise TaggerBusy("扫描图库的任务正在跑，等它结束再反推")
    with _lock:
        if JOB.get("running"):
            raise TaggerBusy("已经有反推任务在跑：%s" % JOB.get("dir"))
        JOB.clear()
        JOB.update(running=True, stop=False, phase="加载模型", dir=label, model=model,
                   total=len(files), done=0, skipped=0, skipped_edited=0, failed=[], current="", eta=None,
                   started=db.now(), finished=None, error=None, report=None)
    t = threading.Thread(target=_run, args=(cfg, models_dir, files, params), daemon=True)
    t.start()
    return _snapshot()


def _files_of_dir(directory):
    directory = os.path.abspath((directory or "").strip().strip('"'))
    if not os.path.isdir(directory):
        raise importer.ImportFailed("目录不存在：%s" % directory)
    files = sorted(os.path.join(directory, n) for n in os.listdir(directory)
                   if n.lower().endswith(IMAGE_EXTS) and os.path.isfile(os.path.join(directory, n)))
    if not files:
        raise importer.ImportFailed("目录里没有图片（png / jpg / jpeg / webp）：%s" % directory)
    return files, directory


def _files_of_ids(cfg, ids):
    """图库里选中的图：只取有真实文件的，仅导入记录和文件已不在的跳过。"""
    try:
        ids = list(dict.fromkeys(int(i) for i in ids))
    except (TypeError, ValueError):
        raise importer.ImportFailed("ids 必须是整数列表")
    if not ids:
        raise importer.ImportFailed("没有选中任何图")
    conn = db.connect(cfg["db_path"])
    try:
        paths = []
        for chunk in range(0, len(ids), 500):            # SQLite 参数个数有上限
            part = ids[chunk:chunk + 500]
            paths += [r[0] for r in conn.execute(
                "SELECT path FROM images WHERE id IN (%s) AND path NOT LIKE 'import:%%' ORDER BY path"
                % ",".join("?" * len(part)), part)]
    finally:
        conn.close()
    files = [p for p in paths if os.path.isfile(p)]
    if not files:
        raise importer.ImportFailed("选中的图里没有能反推的（仅导入记录或文件已不在的不能反推）")
    return files, "选中的 %d 张图" % len(files)


def stop():
    with _lock:
        if not JOB.get("running"):
            return False
        JOB["stop"] = True
        return True


def _set(**kw):
    with _lock:
        JOB.update(kw)


def _run(cfg, models_dir, files, p):
    import scanner
    conn = db.connect(cfg["db_path"])
    report = importer._new_report(False, p["overwrite_edited"])
    desc = "%s t=%g c=%g" % (p["model"], p["threshold"], p["character_threshold"])
    if p["exclude_tags"].strip():
        desc += " x=%s" % p["exclude_tags"].strip()
    model = None
    try:
        model = Model(models_dir, p["model"])
        done_paths, edited_paths = set(), set()
        if p["skip_done"]:
            done_paths = {r[0] for r in conn.execute("SELECT path FROM images WHERE tagged IS NOT NULL")}
        if not p["overwrite_edited"]:
            edited_paths = {r[0] for r in conn.execute("SELECT path FROM images WHERE edited IS NOT NULL")}
        _set(phase="反推中")
        spent, n = 0.0, 0
        for i, path in enumerate(files):
            if JOB.get("stop"):
                _set(phase="已停止")
                break
            name = os.path.basename(path)
            stem = os.path.splitext(name)[0]
            set_name = os.path.basename(os.path.dirname(path))
            if path in done_paths:
                with _lock:
                    JOB["skipped"] += 1
                continue
            if path in edited_paths:         # 手工改过的图默认不覆盖，省得白跑推理
                report["edited_skipped"].append("%s/%s" % (set_name, stem))
                with _lock:
                    JOB["skipped_edited"] += 1
                continue
            _set(current=name)
            t0 = time.time()
            try:
                tags = model.predict(path, p["threshold"], p["character_threshold"], p["exclude_tags"])
                extra = {"path": path, "scores": dict(tags), "tagger": desc}
                info = scanner.file_info(path)
                with conn:
                    importer._import_records(conn, [(stem, [(None, None, [t for t, _ in tags])], extra)],
                                             set_name, path, False, report)
                    image_id, old_sha = conn.execute("SELECT id, sha1 FROM images WHERE path = ?", (path,)).fetchone()
                    # 顺手记下文件信息：以后扫图库能认出它没动过、能判重复
                    scanner.update_image(conn, image_id, info)
                if old_sha and old_sha != info["sha1"]:
                    scanner.make_thumb(path, scanner.thumb_path(cfg, image_id))
                else:
                    scanner.ensure_thumb(cfg, image_id, path)
            except Exception as e:      # 单张失败不影响其他
                with _lock:
                    JOB["failed"].append({"file": name, "error": str(e)})
                continue
            spent += time.time() - t0
            n += 1
            remaining = len(files) - i - 1
            with _lock:
                JOB["done"] += 1
                JOB["eta"] = round(spent / n * remaining)
            if n % EXPORT_EVERY == 0:
                tagdict.schedule(cfg["db_path"], cfg["tag_dict_path"])
        else:
            _set(phase="完成")
    except Exception as e:
        _set(phase="出错", error="反推失败：%s" % e)
    finally:
        conn.close()
        del model                        # 释放模型内存（eva02 约 1.3 GB）
        report["files"] = JOB.get("done", 0)
        _set(running=False, current="", eta=None, finished=db.now(), report=importer._finish(report))
        tagdict.schedule(cfg["db_path"], cfg["tag_dict_path"])

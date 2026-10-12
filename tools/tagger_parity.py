# -*- coding: utf-8 -*-
"""
对拍：用管理器内置反推跑真实模型，与 ComfyUI 反推产出的六段 txt 逐词对比。
改了 tagger.py 的预处理、换了模型版本后跑一次。只读，不写数据库。
需要另装 ComfyUI 和 comfyui-wd14-tagger 节点先产出对照 txt；只有改反推代码时才用得到。

用法：
  .venv\\Scripts\\python.exe -X utf8 tools\\tagger_parity.py <txt目录> <图片1> [图片2 ...] [--model 名称]
txt 按图片的文件名主干在 <txt目录> 里找同名 .txt。
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import importer  # noqa: E402
import tagger    # noqa: E402
from app import CONFIG  # noqa: E402


def comfy_tags(txt_path):
    words = []
    for _key, _header, raws in importer.parse_txt(txt_path, {}):
        words += raws
    return words


def main(argv):
    model_name = None
    if "--model" in argv:
        i = argv.index("--model")
        model_name = argv[i + 1]
        del argv[i:i + 2]
    if len(argv) < 2:
        print(__doc__)
        return 2
    err = tagger.deps_error()
    if err:
        print(err)
        return 2
    txt_dir, images = argv[0], argv[1:]
    tcfg = CONFIG["tagger"]
    model_name = model_name or tcfg["model"]
    t0 = time.time()
    model = tagger.Model(tcfg["models_dir"], model_name)
    print("模型 %s，加载 %.1f 秒" % (model_name, time.time() - t0))
    all_same = True
    for path in images:
        stem = os.path.splitext(os.path.basename(path))[0]
        txt = os.path.join(txt_dir, stem + ".txt")
        if not os.path.exists(txt):
            print("  %s：找不到对照 txt %s" % (stem, txt))
            all_same = False
            continue
        t1 = time.time()
        ours = {t for t, _ in model.predict(path, tcfg["threshold"], tcfg["character_threshold"], tcfg["exclude_tags"])}
        dt = time.time() - t1
        ref = set(comfy_tags(txt))
        same = ours == ref
        all_same &= same
        print("  %s  %.2f 秒  %d 词  %s" % (stem, dt, len(ours), "逐词一致" if same else "不一致"))
        if ours - ref:
            print("    管理器多出：", ", ".join(sorted(ours - ref)))
        if ref - ours:
            print("    管理器缺少：", ", ".join(sorted(ref - ours)))
    print("全部一致" if all_same else "有差异（txt 若被手工改过或当时用了排除词，差异属正常）")
    return 0 if all_same else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

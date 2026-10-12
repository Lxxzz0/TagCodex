# -*- coding: utf-8 -*-
"""
安装：建 .venv、装依赖、生成 config.json、下载 danbooru 表和反推模型。
install.bat 调用它；每一步已经做过就跳过，可以重复运行。只用标准库，用系统 Python 跑。

用法：
  python tools\\setup.py [--tagger | --no-tagger] [--model 模型名]
  --tagger / --no-tagger  要不要启用内置反推（装 onnxruntime、下载模型）；不给就问
  --model                 反推模型，默认 config.json 里的 tagger.model（SmilingWolf 的 WD14 v3 系列）
模型先从 huggingface.co 下，连不上自动换 hf-mirror.com；设了环境变量 HF_ENDPOINT 就只用它。
下载中断后 .part 留着，再运行接着下。pip 镜像用 PIP_INDEX_URL。
"""
import json
import os
import shutil
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe") if os.name == "nt" \
    else os.path.join(ROOT, ".venv", "bin", "python")
DANBOORU_URL = ("https://raw.githubusercontent.com/RafealaSilva/ComfyUI-Danbooru-Tag-Sorter-Node/"
                "main/tags_database/danbooru_tags.xlsx")
MODEL_FILES = (("model.onnx", ".onnx"), ("selected_tags.csv", ".csv"))   # 仓库里的文件名 → 本地后缀
HF_OFFICIAL = "https://huggingface.co"
HF_MIRROR = "https://hf-mirror.com"          # 国内镜像，官方连不上时自动换
TIMEOUT = 20                                 # 连接和每次读的超时（秒）


def step(msg):
    print("\n== %s" % msg, flush=True)


def run(args):
    print("   > " + " ".join(args), flush=True)
    subprocess.check_call(args)


def ask(question, default=False):
    if not sys.stdin or not sys.stdin.isatty():
        return default
    ans = input("%s [%s] " % (question, "Y/n" if default else "y/N")).strip().lower()
    return default if not ans else ans in ("y", "yes", "是")


def abs_path(p):
    return os.path.normpath(os.path.join(ROOT, p)) if p and not os.path.isabs(p) else p


class _Redirect(urllib.request.HTTPRedirectHandler):
    """Python 3.10 的 urllib 不跟 308 跳转（3.11 才支持），hf-mirror 用的就是 308。按 307 处理，Range 头会带过去。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, 307 if code == 308 else code, msg, headers, newurl)

    http_error_308 = urllib.request.HTTPRedirectHandler.http_error_302


OPENER = urllib.request.build_opener(_Redirect)


def hf_endpoints():
    """设了 HF_ENDPOINT 就只用它；没设先连官方，连不上自动换国内镜像。"""
    env = os.environ.get("HF_ENDPOINT", "").strip().rstrip("/")
    if env:
        return [env if env.startswith("http") else "https://" + env]
    return [HF_OFFICIAL, HF_MIRROR]


def _fetch(url, part):
    """下到 part；part 里已经有内容就用 Range 接着下（服务器不支持就从头下）。"""
    have = os.path.getsize(part) if os.path.exists(part) else 0
    headers = {"User-Agent": "TagCodex-setup"}
    if have:
        headers["Range"] = "bytes=%d-" % have
    req = urllib.request.Request(url, headers=headers)
    with OPENER.open(req, timeout=TIMEOUT) as resp:
        if have and resp.status == 206:
            total = int((resp.headers.get("Content-Range") or "").rpartition("/")[2] or 0)
            print("     接着上次的 %.1f MB 往下下" % (have / 1048576), flush=True)
        else:
            have = 0
            total = int(resp.headers.get("Content-Length") or 0)
        done, last = have, -1
        with open(part, "ab" if have else "wb") as f:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                pct = done * 100 // total if total else -1
                if pct != last:
                    last = pct
                    size = "%.1f / %.1f MB" % (done / 1048576, total / 1048576) if total else "%.1f MB" % (done / 1048576)
                    print("\r     %s%s" % ("%3d%%  " % pct if total else "", size), end="", flush=True)
    print()
    if total and done != total:
        raise IOError("连接中断（%d / %d 字节）" % (done, total))


def download(urls, dest):
    """
    依次试 urls 里的地址，前一个连不上或断了就换下一个。先下到 .part，下完再改名，
    中断后不会留下半个文件；.part 留着，下次运行接着下。
    """
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    print("   下载到 %s" % dest, flush=True)
    err = None
    for i, url in enumerate(urls):
        print("   %s %s" % ("从" if i == 0 else "换成", url), flush=True)
        try:
            _fetch(url, part)
            os.replace(part, dest)
            return
        except (OSError, ValueError) as e:       # URLError / 超时 / HTTPError 都是 OSError
            print("\n     失败：%s" % e, flush=True)
            err = e
    raise IOError("下载失败（%s）。网络好了再运行一次 install.bat，已经下的部分会接着下；"
                  "也可以自己下载上面的文件，改名放到 %s" % (err, dest))


def ensure_venv():
    step("Python 虚拟环境 .venv")
    if os.path.exists(VENV_PY):
        print("   已存在，跳过创建")
    else:
        run([sys.executable, "-m", "venv", os.path.join(ROOT, ".venv")])
    run([VENV_PY, "-m", "pip", "install", "-r", os.path.join(ROOT, "requirements.txt")])


def ensure_config():
    step("配置文件 config.json")
    path = os.path.join(ROOT, "config.json")
    if os.path.exists(path):
        print("   已存在，不覆盖")
    else:
        shutil.copyfile(os.path.join(ROOT, "config.example.json"), path)
        print("   已从 config.example.json 生成")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def ensure_danbooru(cfg):
    step("danbooru 参考表")
    dest = abs_path(cfg.get("danbooru_xlsx") or "data/danbooru_tags.xlsx")
    if os.path.isfile(dest):
        print("   已存在，跳过：%s" % dest)
    else:
        download([DANBOORU_URL], dest)


def ensure_tagger(cfg, model):
    step("内置反推：依赖和模型 %s" % model)
    run([VENV_PY, "-m", "pip", "install", "-r", os.path.join(ROOT, "requirements-tagger.txt")])
    models_dir = abs_path((cfg.get("tagger") or {}).get("models_dir") or "data/models")
    for remote, ext in MODEL_FILES:
        dest = os.path.join(models_dir, model + ext)
        if os.path.isfile(dest):
            print("   已存在，跳过：%s" % dest)
        else:
            download(["%s/SmilingWolf/%s/resolve/main/%s" % (hf, model, remote) for hf in hf_endpoints()], dest)


def main(argv):
    if sys.version_info < (3, 10):
        sys.exit("需要 Python 3.10 以上，当前是 %s" % sys.version.split()[0])
    model = None
    if "--model" in argv:
        model = argv[argv.index("--model") + 1]
    ensure_venv()
    cfg = ensure_config()
    ensure_danbooru(cfg)
    model = model or (cfg.get("tagger") or {}).get("model") or "wd-eva02-large-tagger-v3"
    if "--tagger" in argv:
        tagger = True
    elif "--no-tagger" in argv:
        tagger = False
    else:
        tagger = ask("\n启用内置反推吗？要装 onnxruntime 并下载约 1.3GB 的模型（%s）。以后想用再运行一次 install.bat 也行。" % model)
    if tagger:
        ensure_tagger(cfg, model)
    step("完成")
    print("   双击 run.bat 启动。第一次进去先在顶栏「反推设置」→「danbooru 参考表」里导入 danbooru 表。")
    if not tagger:
        print("   没有启用反推，其他功能都能用。")


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (subprocess.CalledProcessError, IOError, OSError) as e:
        sys.exit("\n[错误] %s" % e)

# 参与开发

欢迎提 issue 和 PR。下面是代码里的约定，改之前看一眼能少走弯路。

## 环境

```bat
install.bat                                   :: 建 .venv、装依赖
.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
node --check static/app.js
node tools/check_template.js                  :: 编译检查 index.html 里的 Vue 模板
```

- 测试用临时数据库和临时缩略图目录，不会碰 `data/`。`tests/test_api.py` 需要一份真实的反推记录：设环境变量 `TAGCODEX_TEST_DATA` 指向含 `反推记录.xlsx` 的目录，并先下载 danbooru 表；没有就整组跳过
- 改了 `tagger.py` 的预处理后，用 `tools/tagger_parity.py` 和 ComfyUI 里 WD14 节点的结果逐词对拍（需要另装 ComfyUI 和 comfyui-wd14-tagger）

## 目录

| 文件 | 内容 |
| --- | --- |
| `app.py` | Flask 路由、读配置 |
| `db.py` | 建表、升级、分页 |
| `importer.py` | 导入 xlsx / txt / danbooru 表，机器归类 |
| `tag_rules.py` | 规则表：danbooru 表没意见的词由它兜底归类 |
| `tagops.py` | 词典操作和撤销 |
| `tagdict.py` | 导出 `tag_dict.json` 和完整词典 |
| `tagger.py` | 内置反推（onnxruntime，可选依赖） |
| `scanner.py` | 扫描图库、缩略图 |
| `gallery.py` | 图库查询、词频、单图改词、图片关联 |
| `presets.py` / `composer.py` / `drafts.py` | 预设组 / 组合器 / 空白套图 |
| `themes.py` | 主题（后端保留，界面已不用） |
| `static/` | 前端：`index.html` + `app.js` + `app.css`，Vue 3 从 CDN 加载 |

## 约定

- 依赖只有 flask / pillow / openpyxl；反推的 onnxruntime / numpy 是可选的，**只能延迟 import**，缺了只禁用反推
- 前端不引入 node_modules、不加构建步骤；不用 `v-html`；颜色只用 `app.css` 里 `:root` 的变量（深色模式两处都要写）
- 前端不算归类、不拼提示词：段永远由后端按词典实时算（`gallery.segments`），拼提示词只用 `gallery.prompt_of`
- 分页接口一律返回 `{items, total, page, size}`（用 `db.paginate`）
- 会改数据的操作（批量归类、单图改词、关联、预设、空白套图……）都写 `op_log`，保证能撤销
- 扫描和反推是互斥的后台单任务；**只读图片**，不移动、不改名、不删除；组合器导出只写用户指定的目录，不能在图库根里面
- 机器意见有歧义时进分歧队列让用户定，不要用多数投票之类的办法替用户决定
- 面向用户的文案、注释用中文；Python 用 snake_case
- 改了界面或行为，同步更新 `docs/` 里对应的使用说明

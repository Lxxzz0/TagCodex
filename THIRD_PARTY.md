# 第三方资源

下面这些都**不随本仓库分发**。`install.bat`（`tools/setup.py`）运行时从原始出处下载，或者由浏览器从 CDN 加载。它们各自的许可证适用于它们本身。

| 资源 | 用途 | 出处 | 许可证 |
| --- | --- | --- | --- |
| WD14 tagger 模型（默认 `wd-eva02-large-tagger-v3`，`model.onnx` + `selected_tags.csv`） | 内置反推 | [SmilingWolf on HuggingFace](https://huggingface.co/SmilingWolf/wd-eva02-large-tagger-v3) | Apache-2.0 |
| `danbooru_tags.xlsx` | danbooru 标签参考表：中文翻译、大类 / 小类，用于机器归类 | [RafealaSilva/ComfyUI-Danbooru-Tag-Sorter-Node](https://github.com/RafealaSilva/ComfyUI-Danbooru-Tag-Sorter-Node) | MIT |
| Vue 3 | 前端界面，从 jsDelivr / cdnjs 加载 | [vuejs/core](https://github.com/vuejs/core) | MIT |

Python 依赖（flask、pillow、openpyxl，以及可选的 onnxruntime、numpy）由 pip 安装，许可证见各自的项目。

## 改写的代码

`tagger.py` 里的图片预处理、读标签表和按阈值取词改写自 [pythongosssss/ComfyUI-WD14-Tagger](https://github.com/pythongosssss/ComfyUI-WD14-Tagger)，目的是与它的结果逐词一致。原许可证：

```
MIT License

Copyright (c) 2024 pythongosssss

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

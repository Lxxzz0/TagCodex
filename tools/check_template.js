// 用与页面同版本的 Vue 编译 static/index.html 里的模板，提前发现模板语法错误。
// 运行：node tools/check_template.js
// 首次运行会把 vue.global.js 下载到 tools/.cache/（已在 .gitignore 里）。
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const VUE_URL = 'https://cdn.jsdelivr.net/npm/vue@3.4.38/dist/vue.global.js';
const root = path.resolve(__dirname, '..');
const cacheDir = path.join(__dirname, '.cache');
const vuePath = path.join(cacheDir, 'vue.global.3.4.38.js');

async function loadVue() {
  if (!fs.existsSync(vuePath)) {
    fs.mkdirSync(cacheDir, { recursive: true });
    const r = await fetch(VUE_URL);
    if (!r.ok) throw new Error('下载 Vue 失败：' + r.status);
    fs.writeFileSync(vuePath, await r.text());
  }
  const ctx = { console: { log() {}, warn() {}, info() {} } };
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(vuePath, 'utf8'), ctx);
  return ctx.Vue;
}

(async () => {
  const html = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');
  const start = html.indexOf('<div id="app"');
  const end = html.lastIndexOf('<script src="/static/app.js"');
  const block = html.slice(start, end);
  const inner = block.slice(block.indexOf('>') + 1, block.lastIndexOf('</div>'));
  // 组件模板放在 <script type="text/x-template" id="..."> 里，一并编译
  const parts = [['#app', inner]];
  const re = /<script type="text\/x-template" id="([^"]+)">([\s\S]*?)<\/script>/g;
  for (let m; (m = re.exec(html));) parts.push(['#' + m[1], m[2]]);
  const Vue = await loadVue();
  const errs = [];
  for (const [name, tpl] of parts) {
    Vue.compile(tpl, {
      decodeEntities: s => s,            // node 里没有 DOM，跳过实体解码
      onError: e => errs.push(name + ': ' + e.message),
      onWarn: e => errs.push(name + ' warn: ' + e.message),
    });
  }
  if (errs.length) {
    console.error(errs.join('\n'));
    process.exit(1);
  }
  console.log('模板编译通过：' + parts.map(([n, t]) => n + '（' + t.length + ' 字符）').join('，'));
})().catch(e => { console.error(e.message); process.exit(1); });

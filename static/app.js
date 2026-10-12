/* TagCodex · 词典 + 图库 + 组合器界面（Vue 3 CDN 全局版，无构建） */
const { createApp, reactive, ref, computed, watch, onMounted, onBeforeUnmount, provide, inject } = Vue;

const SOURCE = { manual: '手工', file: '文件', tagger: '反推模型', danbooru: 'danbooru', rule: '规则' };
const THEMES = ['auto', 'light', 'dark'];
const THEME_LABEL = { auto: '主题：跟随系统', light: '主题：浅色', dark: '主题：深色' };
const STRONG_SCORE = 0.8;        // 置信度分三档：≥ 0.80 强、0.50–0.79 中、< 0.50 弱
const LOW_SCORE = 0.5;
const SEG_LABEL_W = 88;          // 段标签槽 76px + 间距 12px（与 app.css 的 .seg-label 一致）
const MORE_W = 62;               // 给 +N 按钮留的宽度
const REPORT_LIST = 50;          // 报告里的长列表最多显示这么多条
const IDS_PAGE = 500;            // 取当前筛选全部图片 id 时每页大小（后端分页上限）
// 图库筛选里进查询串的项。预设组和目录（文件夹 dir / 套图 / scope）二选一；
// 按预设组筛选时另带命中阈值 group_min（个人设置，不算筛选项，见 gFilterArgs）
const GV_FILTER_KEYS = ['dir', 'dir_only', 'set', 'scope', 'state', 'group'];
const GROUP_MIN = 50;             // 预设组命中阈值默认 50%（同后端 presets.MATCH_MIN）
const STATS_MIN = 30;
const PE_CAND = 30;               // 预设组编辑器每段默认显示的候选词个数
const PE_SUGG_DELAY = 250;        // 编辑器搜词典的防抖（毫秒）
const PE_DICT = 200;              // 编辑器从词典取候选词的个数（按出现次数）
const WP_W = 440, WP_H = 460;     // 挑词框的宽高（与 app.css 的 .wpick 一致），用来算弹出位置
const WP_CAND = 60;               // 挑词框默认显示的候选词个数
const TABS = ['dict', 'gallery', 'composer'];
// 组合器的槽方式，同后端 composer.MODES；fill：这种方式要填内容
const CM_MODES = [
  { key: 'keep', label: '保留', fill: false }, { key: 'replace', label: '替换', fill: true },
  { key: 'prepend', label: '前加', fill: true }, { key: 'append', label: '后加', fill: true },
  { key: 'remove', label: '删词', fill: true }, { key: 'clear', label: '清空', fill: false },
];
const CM_PREVIEW_DELAY = 300;    // 改槽后防抖多久重新预览
// 读写 localStorage 的便利项，存不了也能用
function lsGet(k, d = '') { try { return localStorage.getItem(k) ?? d; } catch (e) { return d; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* 忽略 */ } }

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return; } catch (e) { /* 退回老办法 */ }
  const ta = document.createElement('textarea');
  ta.value = text;
  document.body.appendChild(ta);
  ta.select();
  document.execCommand('copy');
  ta.remove();
}

// 一张图按段显示的词：列表和图片详情共用。状态和操作都在根组件里，通过 provide('g') 拿
// list：用在列表卡片上，受「显示段」「紧凑」控制；详情里不传，永远全部展开
const ImgSegs = {
  template: '#img-segs',
  props: { item: { type: Object, required: true }, list: Boolean },
  setup(props) {
    const g = inject('g');
    const box = ref(null);
    const width = ref(0);              // 段区实测宽度，窗口缩放时跟着变
    let ro = null;
    onMounted(() => {
      if (!props.list) return;
      width.value = box.value.clientWidth;
      ro = new ResizeObserver(es => { width.value = es[0].contentRect.width; });
      ro.observe(box.value);
    });
    onBeforeUnmount(() => ro && ro.disconnect());

    // 按估算宽度累加，放不下的折成 +N；只渲染放得下的词
    const chipWidth = t => t.word.length * 7.1 + 26 + (g.showScore && t.score != null ? 30 : 0) + 6;
    const rows = computed(() => {
      const avail = width.value - SEG_LABEL_W - MORE_W;
      const out = [];
      for (const s of props.item.segments) {
        if (props.list && g.hiddenSegs.has(s.key)) continue;
        const tags = g.visibleTags(s);
        const open = props.list && g.compact && g.expanded.has(g.segKey(props.item, s));
        let shown = tags;
        if (props.list && g.compact && !open) {
          let used = 0, n = 0;
          while (n < tags.length && (used += chipWidth(tags[n])) <= avail) n++;
          shown = tags.slice(0, n);
        }
        const more = tags.length - shown.length;
        // full：这段的词全看得见，才给加词和拖动调序
        out.push({ s, shown, more, open, count: tags.length, full: !more });
      }
      return out;
    });
    return { g, box, rows };
  },
};

async function api(method, url, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    opt.headers['Content-Type'] = 'application/json';
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(url, opt);
  let data = null;
  try { data = await r.json(); } catch (e) { /* 非 JSON 响应 */ }
  if (!r.ok) {
    const e = new Error((data && data.error) || ('请求失败 ' + r.status));
    Object.assign(e, { status: r.status, data });     // 409 等需要看返回内容的情况
    throw e;
  }
  return data;
}

createApp({
  setup() {
    const stats = ref({});
    const buckets = ref([]);
    const page = ref({ items: [], total: 0, page: 1, size: 100 });
    const loading = ref(false);
    const selected = reactive(new Map());   // id -> 词对象，跨页保留
    const ops = ref([]);
    const showOps = ref(false);
    const aliasDlg = reactive({ open: false, main: null });
    // 对话框里的页签分两组：顶栏「添加 / 导入」和「反推设置」各打开一组，各自记住上次停在哪个页签
    const IMP_GROUPS = { import: ['add', 'xlsx', 'txt', 'scan'], tagger: ['tag', 'ref'] };
    const impGroupOf = tab => (IMP_GROUPS.tagger.includes(tab) ? 'tagger' : 'import');
    const impLast = { import: 'add', tagger: 'tag' };
    const imp = reactive({ open: false, group: 'import', tab: 'add', bucket: 'character', text: '', xlsx: '', dir: '',
                           fileWins: false, overwriteEdited: false, busy: false, report: null, reportOf: '' });
    try {
      imp.xlsx = localStorage.getItem('imp.xlsx') || '';
      imp.dir = localStorage.getItem('imp.dir') || '';
    } catch (e) { /* 记不住上次路径也不影响使用 */ }
    const detail = reactive({ open: false, tag: null, zh: '', note: '', images: null, saving: false });
    const showExport = ref(false);
    const tg = reactive({ status: null, dir: '', model: '', threshold: 0.35, character_threshold: 0.8,
                          exclude_tags: '', skip_done: true, overwrite_edited: false });
    try { tg.dir = localStorage.getItem('tg.dir') || ''; } catch (e) { /* 忽略 */ }
    const bEdit = reactive({ on: false, renaming: null, name: '', newKey: '', newName: '', drag: null, over: null });
    const toast = ref(null);
    const tab = ref(TABS.includes(lsGet('tab')) ? lsGet('tab') : 'dict');
    // 默认落在未归类、按频次倒序：每天要清的队列
    const view = reactive({ bucket: 'unknown', filter: '', conflict: 'any', q: '', sort: 'freq', size: 100 });

    let savedTheme = 'auto';
    try { savedTheme = localStorage.getItem('theme') || 'auto'; } catch (e) { /* 无痕模式等 */ }
    const theme = ref(THEMES.includes(savedTheme) ? savedTheme : 'auto');
    function cycleTheme() {
      theme.value = THEMES[(THEMES.indexOf(theme.value) + 1) % THEMES.length];
      if (theme.value === 'auto') delete document.documentElement.dataset.theme;
      else document.documentElement.dataset.theme = theme.value;
      try { localStorage.setItem('theme', theme.value); } catch (e) { /* 存不了就只本次生效 */ }
    }

    let toastTimer = null;
    function notify(text, kind = 'ok', undo = false) {
      toast.value = { text, kind, undo };
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => (toast.value = null), kind === 'err' ? 6000 : 2500);
    }

    async function loadStats() {
      try {
        const s = await api('GET', '/api/stats');
        stats.value = s;
        buckets.value = s.buckets;
      } catch (e) { notify(e.message, 'err'); }
    }

    async function loadOps() {
      try { ops.value = await api('GET', '/api/oplog'); } catch (e) { notify(e.message, 'err'); }
    }

    async function reload(p = page.value.page) {
      const qs = new URLSearchParams({ page: p, size: view.size, sort: view.sort });
      if (view.q.trim()) qs.set('q', view.q.trim());
      if (view.filter === 'conflict') qs.set('conflict', view.conflict);
      else if (view.filter === 'blocked') qs.set('blocked', '1');
      else if (view.filter !== 'all') qs.set('bucket', view.bucket);
      loading.value = true;
      try {
        let res = await api('GET', '/api/tags?' + qs);
        // 归位后当前页可能被清空，退回上一页
        if (!res.items.length && res.page > 1 && res.total > 0) {
          qs.set('page', Math.ceil(res.total / res.size));
          res = await api('GET', '/api/tags?' + qs);
        }
        page.value = res;
      } catch (e) { notify(e.message, 'err'); }
      loading.value = false;
    }

    // 改了数据之后：统计、操作日志、当前页（词典或图库）、打开着的图片详情都重新拉
    async function refresh() {
      const jobs = [loadStats(), loadOps()];
      if (tab.value === 'dict') jobs.push(reload(), loadPresets());
      else if (tab.value === 'gallery') jobs.push(loadGallery(), loadTree(), loadPresets(), loadDrafts());
      else jobs.push(loadPresets().then(() => cmPreview()), loadTree(), loadDrafts());   // 先清掉槽里已删的预设再预览
      if (st.open) jobs.push(loadStatsDrawer());
      if (gd.open && gd.item) jobs.push(openImage(gd.item.id, true));
      await Promise.all(jobs);
    }
    function switchTab(t) {
      if (tab.value === t) return;
      tab.value = t;
      lsSet('tab', t);
      refresh();
    }

    // ---------- 批量操作 ----------
    async function run(method, url, body, okText) {
      try {
        const r = await api(method, url, body);
        selected.clear();
        await refresh();
        notify(okText(r), 'ok', true);
        return r;
      } catch (e) { notify(e.message, 'err'); }
    }
    const selIds = () => [...selected.keys()];

    function assignTo(key) {
      if (!selected.size) return;
      const b = buckets.value.find(x => x.key === key);
      run('POST', '/api/tags/assign', { ids: selIds(), bucket: key },
          r => r.changed ? `${r.changed} 个词归到「${b.name}」` : '都已经在这个段了');
    }
    function setBlocked(flag) {
      run('POST', '/api/tags/block', { ids: selIds(), blocked: flag },
          r => `${r.changed} 个词${flag ? '加入' : '移出'}黑名单`);
    }
    const anyBlocked = computed(() => [...selected.values()].some(t => t.blocked));
    const anyActive = computed(() => [...selected.values()].some(t => !t.blocked));

    function openAlias() {
      // 默认主词取频次最高的
      const list = [...selected.values()].sort((a, b) => b.freq - a.freq);
      aliasDlg.main = list[0].id;
      aliasDlg.open = true;
    }
    async function doAlias() {
      const ids = selIds().filter(id => id !== aliasDlg.main);
      const main = selected.get(aliasDlg.main);
      aliasDlg.open = false;
      await run('POST', '/api/tags/alias', { main_id: aliasDlg.main, ids },
                r => `${r.merged} 个词并入「${main.word}」作为别名`);
    }

    async function undo() {
      try {
        const r = await api('POST', '/api/oplog/undo');
        selected.clear();
        await refresh();
        notify('已撤销：' + r.summary);
      } catch (e) { notify(e.message, 'err'); }
    }

    // ---------- 添加 / 导入 ----------
    // tab 给了就开到那个页签（所在的组跟着变）；没给就开 group 那一组上次停的页签
    function openImport(tab, group) {
      imp.group = tab ? impGroupOf(tab) : (group || 'import');
      imp.tab = tab || impLast[imp.group];
      if (view.bucket && view.bucket !== 'unknown' && !view.filter) imp.bucket = view.bucket;
      imp.report = null;
      imp.open = true;
      if (imp.tab === 'tag') loadTagger();
      if (imp.tab === 'scan') loadScan();
    }
    watch(() => imp.tab, t => { impLast[impGroupOf(t)] = t; });
    const pasteWords = computed(() => {
      const seen = new Set();
      return imp.text.split(/[,\n]/).map(w => w.trim().toLowerCase().replace(/_/g, ' ').replace(/ +/g, ' '))
        .filter(w => w && !seen.has(w) && seen.add(w));
    });
    async function doAdd() {
      if (!pasteWords.value.length) return;
      imp.busy = true;
      const b = buckets.value.find(x => x.key === imp.bucket);
      const r = await run('POST', '/api/tags/add', { bucket: imp.bucket, text: imp.text },
        r => `添加到「${b.name}」：新增 ${r.created}，改段 ${r.moved}，原本就在 ${r.unchanged}`);
      imp.busy = false;
      if (r) imp.text = '';
    }
    async function doImport(kind) {
      const opts = { file_wins: imp.fileWins, overwrite_edited: imp.overwriteEdited };
      const body = kind === 'xlsx' ? { path: imp.xlsx, ...opts }
                 : kind === 'txt' ? { dir: imp.dir, ...opts } : {};
      try {
        if (kind === 'xlsx') localStorage.setItem('imp.xlsx', imp.xlsx);
        if (kind === 'txt') localStorage.setItem('imp.dir', imp.dir);
      } catch (e) { /* 忽略 */ }
      imp.busy = true; imp.report = null;
      try {
        imp.report = await api('POST', '/api/import/' + kind, body);
        imp.reportOf = kind;
        await refresh();
      } catch (e) { notify(e.message, 'err'); }
      imp.busy = false;
    }

    // ---------- 词详情 ----------
    async function openDetail(t) {
      detail.tag = t; detail.zh = t.zh || ''; detail.note = t.note || '';
      detail.images = null; detail.open = true;
      loadImages(1);
    }
    async function loadImages(p) {
      if (!detail.tag) return;
      const id = detail.tag.id;
      try {
        const r = await api('GET', `/api/tags/${id}/images?page=${p}&size=30`);
        if (detail.tag && detail.tag.id === id) detail.images = r;   // 快速切换时丢弃旧响应
      } catch (e) { notify(e.message, 'err'); }
    }
    const detailDirty = computed(() => detail.tag &&
      (detail.zh.trim() !== (detail.tag.zh || '') || detail.note.trim() !== (detail.tag.note || '')));
    async function saveDetail() {
      if (!detailDirty.value) return;
      detail.saving = true;
      try {
        await api('PATCH', `/api/tags/${detail.tag.id}`, { zh: detail.zh, note: detail.note });
        detail.tag.zh = detail.zh.trim() || null;
        detail.tag.note = detail.note.trim() || null;
        notify('已保存');
        reload();
      } catch (e) { notify(e.message, 'err'); }
      detail.saving = false;
    }
    async function removeAlias(alias) {
      if (!confirm(`移除别名「${alias}」？\n它以后会被当成一个独立的新词，出现次数不会自动拆回来。`)) return;
      try {
        await api('POST', '/api/tags/alias', { remove: alias });
        detail.tag.aliases = (detail.tag.aliases || '').split(', ').filter(a => a !== alias).join(', ') || null;
        notify('已移除别名');
        refresh();
      } catch (e) { notify(e.message, 'err'); }
    }
    function closeDetail() {
      if (detailDirty.value && !confirm('中文/备注还没保存，确定关闭？')) return;
      detail.open = false; detail.tag = null;
    }
    const detailOpinions = computed(() => {
      if (!detail.tag) return [];
      const name = k => (buckets.value.find(b => b.key === k) || {}).name || k;
      return (detail.tag.opinions || []).map(o => ({
        src: SOURCE[o.source] || o.source, bucket: name(o.bucket_key),
        origin: o.origin ? o.origin.split(/[\\/]/).pop() : '',
      }));
    });

    // ---------- 导出 ----------
    async function exportNow() {
      showExport.value = false;
      try {
        const r = await api('POST', '/api/tagdict/export');
        await loadStats();
        notify(`已写入 tag_dict.json：${r.counts.tags} 个词，${r.counts.aliases} 个别名，${r.counts.blocked} 个黑名单`);
      } catch (e) { notify(e.message, 'err'); }
    }

    // ---------- 反推 ----------
    const tgJob = computed(() => (tg.status && tg.status.job) || {});
    const tgRunning = computed(() => !!tgJob.value.running);
    const tgPercent = computed(() => {
      const j = tgJob.value;
      return j.total ? Math.round((j.done + j.skipped) / j.total * 100) : 0;
    });
    let tgTimer = null;
    let tgDefaultsSet = false;
    async function loadTagger() {
      try {
        const s = await api('GET', '/api/tagger/status');
        const wasRunning = tgRunning.value;
        tg.status = s;
        if (!tgDefaultsSet && s.defaults) {
          // 默认值只在第一次读到时填，之后保留用户改过的
          tg.model = s.models.includes(s.defaults.model) ? s.defaults.model : (s.models[0] || '');
          tg.threshold = s.defaults.threshold;
          tg.character_threshold = s.defaults.character_threshold;
          tg.exclude_tags = s.defaults.exclude_tags || '';
          tgDefaultsSet = true;
        }
        if (s.job.running) {
          clearTimeout(tgTimer);
          tgTimer = setTimeout(loadTagger, 1000);
        } else if (wasRunning) {
          await refresh();
          notify(`反推${s.job.phase}：${s.job.done} 张入库` + (s.job.failed.length ? `，${s.job.failed.length} 张失败` : '')
                 + (s.job.skipped_edited ? `，跳过 ${s.job.skipped_edited} 张手工改过的图` : ''),
                 s.job.error || s.job.failed.length ? 'err' : 'ok');
        }
      } catch (e) { notify(e.message, 'err'); }
    }
    async function startTagger() {
      try { localStorage.setItem('tg.dir', tg.dir); } catch (e) { /* 忽略 */ }
      try {
        await api('POST', '/api/tagger/run', {
          dir: tg.dir, model: tg.model, threshold: tg.threshold,
          character_threshold: tg.character_threshold, exclude_tags: tg.exclude_tags, skip_done: tg.skip_done,
          overwrite_edited: tg.overwrite_edited,
        });
        await loadTagger();
      } catch (e) { notify(e.message, 'err'); }
    }
    async function stopTagger() {
      try { await api('POST', '/api/tagger/stop'); await loadTagger(); } catch (e) { notify(e.message, 'err'); }
    }
    function fmtSec(s) {
      if (s < 60) return s + ' 秒';
      return Math.floor(s / 60) + ' 分 ' + (s % 60) + ' 秒';
    }
    onMounted(loadTagger);   // 刷新页面时如果反推还在跑，接着显示进度

    // ---------- 段管理 ----------
    async function patchBuckets(body, okText) {
      try {
        await api('PATCH', '/api/buckets', body);
        await refresh();
        if (okText) notify(okText);
        return true;
      } catch (e) { notify(e.message, 'err'); return false; }
    }
    function startRename(b) { bEdit.renaming = b.key; bEdit.name = b.name; }
    async function saveRename(b) {
      if (bEdit.renaming !== b.key) return;   // 回车后输入框移除还会触发一次 blur
      const name = bEdit.name.trim();
      bEdit.renaming = null;
      if (name && name !== b.name) await patchBuckets({ rename: { key: b.key, name } }, `已改名为「${name}」`);
    }
    function toggleJoins(b) {
      patchBuckets({ joins: { key: b.key, joins: !b.joins } },
                   `「${b.name}」${b.joins ? '不再' : '重新'}参与拼接`);
    }
    async function addBucket() {
      const key = bEdit.newKey.trim(), name = bEdit.newName.trim();
      if (!key || !name) { notify('key 和中文名都要填', 'err'); return; }
      if (await patchBuckets({ add: { key, name } }, `已新增段「${name}」`)) {
        bEdit.newKey = ''; bEdit.newName = '';
      }
    }
    async function deleteBucket(b) {
      const msg = `删除段「${b.name}」？
它下面的 ${b.count} 个词会移到「未归类」。可以撤销。`;
      if (!confirm(msg)) return;
      try {
        await api('PATCH', '/api/buckets', { delete: b.key });
        if (view.bucket === b.key) view.bucket = 'unknown';
        await refresh();
        notify(`已删除段「${b.name}」`, 'ok', true);
      } catch (e) { notify(e.message, 'err'); }
    }
    function dragStart(b, e) { bEdit.drag = b.key; e.dataTransfer.effectAllowed = 'move'; }
    function dragOver(b) { if (bEdit.drag) bEdit.over = b.key; }
    function dragEnd() { bEdit.drag = null; bEdit.over = null; }
    async function dropOn(b) {
      const from = bEdit.drag;
      dragEnd();
      if (!from || from === b.key) return;
      const keys = buckets.value.map(x => x.key).filter(k => k !== from);
      keys.splice(keys.indexOf(b.key) + (isBelow(from, b.key) ? 1 : 0), 0, from);
      await patchBuckets({ order: keys });
    }
    // 往下拖放到目标之后，往上拖放到目标之前
    function isBelow(from, to) {
      const ks = buckets.value.map(x => x.key);
      return ks.indexOf(from) < ks.indexOf(to);
    }

    // ---------- 图库：筛选与列表 ----------
    const gv = reactive({ dir: '', dir_only: '', set: '', scope: '', state: '', group: '',
                          group_min: Number(lsGet('g.groupMin', String(GROUP_MIN))) || GROUP_MIN,
                          q: '', sort: 'touched',
                          mode: lsGet('g.mode', 'list') === 'grid' ? 'grid' : 'list', size: 60 });
    const gpage = ref({ items: [], total: 0, page: 1, size: 60 });
    const gtree = ref({ dirs: [], root_own: 0, total: 0, gallery: 0, outside: 0, placeholder: 0, untagged: 0, missing: 0 });
    const gloading = ref(false);
    const gsel = reactive(new Set());              // 选中的图片 id
    // 树里展开的文件夹（路径）；默认都收起，只显示第一层。记在 localStorage
    const openDirs = reactive(new Set(lsGet('g.openDirs').split('|').filter(Boolean)));
    const g = reactive({
      showBlocked: lsGet('g.blocked') === '1',
      // 列表卡片的纯视图状态，不进撤销栈
      compact: lsGet('g.compact', '1') !== '0',
      showScore: lsGet('g.score') === '1',
      hiddenSegs: new Set(lsGet('g.hideSegs').split(',').filter(Boolean)),   // 记隐藏的，新加的段默认显示
      expanded: new Set(),                                                  // 紧凑模式下展开的「图/段」
      drag: { img: null, seg: null, tag: null, over: null },
    });
    provide('g', g);

    // 当前筛选（不含排序和搜词）：选了预设组时带上命中阈值
    function gFilterArgs() {
      const f = {};
      for (const k of GV_FILTER_KEYS) if (gv[k]) f[k] = gv[k];
      if (gv.group) f.group_min = gv.group_min;
      return f;
    }
    function gQuery() {
      const qs = new URLSearchParams({ sort: gv.sort, ...gFilterArgs() });
      if (gv.q.trim()) qs.set('q', gv.q.trim());
      if (pk.id) qs.set('link_of', pk.id);          // 挑图模式：标出已经关联过的图
      return qs;
    }
    let gReq = 0;
    async function loadGallery(p = gpage.value.page) {
      const qs = gQuery();
      qs.set('page', p); qs.set('size', gv.size);
      const my = ++gReq;
      gloading.value = true;
      try {
        let res = await api('GET', '/api/images?' + qs);
        if (!res.items.length && res.page > 1 && res.total > 0) {
          qs.set('page', Math.ceil(res.total / res.size));
          res = await api('GET', '/api/images?' + qs);
        }
        if (my === gReq) gpage.value = res;          // 快速切换筛选时丢弃旧响应
      } catch (e) { notify(e.message, 'err'); }
      if (my === gReq) gloading.value = false;
    }
    async function loadTree() {
      try { gtree.value = await api('GET', '/api/images/tree'); } catch (e) { notify(e.message, 'err'); }
    }
    // 目录筛选和预设组二选一：选目录时清掉预设组，setGroup 时清掉目录
    const DIR_RESET = { dir: '', dir_only: '', set: '', scope: '' };
    function gFilter(patch) {
      leaveGroup();
      Object.assign(gv, DIR_RESET, patch);
      if (pk.id) savePickFilter(); else gsel.clear();   // 挑图时换筛选不清掉已勾的
      loadGallery(1);
    }
    function pickState(st) {
      if (tab.value !== 'gallery') switchTab('gallery');
      gv.state = st;
      if (pk.id) savePickFilter(); else gsel.clear();
      loadGallery(1);
    }
    function toggleDir(path) {
      openDirs.has(path) ? openDirs.delete(path) : openDirs.add(path);
      lsSet('g.openDirs', [...openDirs].join('|'));
    }
    // 点文件夹：筛这个文件夹（含子文件夹），顺手展开
    function openDir(path) {
      gFilter({ dir: path });
      if (!openDirs.has(path)) toggleDir(path);
    }
    // 树拍平成要显示的行：父文件夹展开了才显示子文件夹
    const dirRows = computed(() => {
      const out = [];
      const walk = (nodes, depth) => {
        for (const n of nodes) {
          out.push({ n, depth });
          if (n.children.length && openDirs.has(n.path)) walk(n.children, depth + 1);
        }
      };
      walk(gtree.value.dirs, 0);
      return out;
    });
    const hasFilter = computed(() => !!(gv.dir || gv.dir_only || gv.set || gv.scope));
    function setMode(m) { gv.mode = m; lsSet('g.mode', m); }
    function toggleShowBlocked() { g.showBlocked = !g.showBlocked; lsSet('g.blocked', g.showBlocked ? '1' : '0'); }
    function toggleCompact() { g.compact = !g.compact; lsSet('g.compact', g.compact ? '1' : '0'); }
    function toggleShowScore() { g.showScore = !g.showScore; lsSet('g.score', g.showScore ? '1' : '0'); }
    function toggleSegShown(key) {
      g.hiddenSegs.has(key) ? g.hiddenSegs.delete(key) : g.hiddenSegs.add(key);
      lsSet('g.hideSegs', [...g.hiddenSegs].join(','));
    }
    g.segKey = (item, seg) => item.id + '/' + seg.key;
    g.toggleSeg = (item, seg) => {
      const k = g.segKey(item, seg);
      g.expanded.has(k) ? g.expanded.delete(k) : g.expanded.add(k);
    };
    let gSearchTimer = null;
    function gSearchLater() {
      clearTimeout(gSearchTimer);
      gSearchTimer = setTimeout(() => { gsel.clear(); loadGallery(1); }, 250);
    }
    const gPageCount = computed(() => Math.max(1, Math.ceil(gpage.value.total / gpage.value.size)));
    const gTitle = computed(() => {
      if (gv.scope === 'draft') return gv.set ? '空白套图：' + gv.set : '空白套图';
      if (gv.dir_only && !gv.dir) return '（根目录）';
      if (gv.dir) return gv.dir.split('/').join(' / ');
      if (gv.set) return '套图：' + gv.set;
      return { outside: '图库目录外', placeholder: '仅导入记录' }[gv.scope] || '全部图片';
    });
    // 选了预设组时标题就是预设组（预设组和目录不叠加）
    const gFullTitle = computed(() => (gv.group ? `预设组：${gv.group}` : gTitle.value));
    // 图在哪：图库里的显示文件夹路径，图库外的显示套图（所在目录名 / 导入时的套图名）
    const placeText = it => (it.dir != null ? (it.dir ? it.dir.split('/').join(' / ') : '（根目录）') : (it.set_name || ''));
    // 从导入 / 反推报告跳过来看「刚处理的那一批」
    function goBatch(setName) {
      imp.open = false;
      Object.assign(gv, { dir: '', dir_only: '', set: setName || '', scope: '', state: '', group: '',
                          q: '', sort: 'touched' });
      gsel.clear();
      if (tab.value !== 'gallery') { tab.value = 'gallery'; lsSet('tab', 'gallery'); }
      refresh();
    }

    const baseName = p => p.split(/[\\/]/).pop();
    const thumbUrl = it => `/thumb/${it.id}.webp?v=${it.thumb}`;
    const canTag = it => !it.placeholder && !it.missing;
    const hasTags = it => it.segments.some(s => s.tags.length);
    g.visibleTags = s => (g.showBlocked ? s.tags : s.tags.filter(t => !t.blocked));
    // 没有置信度（导入 / 手工加的）按强显示
    g.tier = t => (t.score == null || t.score >= STRONG_SCORE ? 's' : t.score >= LOW_SCORE ? 'm' : 'w');

    // 平时都能勾（反推时后端跳过仅导入记录和文件已不在的）；挑图模式下除了这张图自己和已关联的，都能勾
    const checkable = it => (pk.id ? it.id !== pk.id && !it.linked : true);
    function toggleImg(it) { gsel.has(it.id) ? gsel.delete(it.id) : gsel.add(it.id); }
    const gAllChecked = computed(() => {
      const items = gpage.value.items.filter(checkable);
      return items.length > 0 && items.every(it => gsel.has(it.id));
    });
    function gToggleAll() {
      const on = !gAllChecked.value;
      gpage.value.items.filter(checkable).forEach(it => (on ? gsel.add(it.id) : gsel.delete(it.id)));
    }

    async function copyPrompt(it) {
      if (!it.prompt) { notify('这张图没有可拼接的提示词', 'err'); return; }
      try { await copyText(it.prompt); notify('已复制提示词（' + it.prompt.length + ' 字符）'); }
      catch (e) { notify('复制失败：' + e.message, 'err'); }
    }

    // ---------- 图库：按预设组筛选 ----------
    // 图上含组里词的比例 ≥ 阈值就算这个组的图（后端实时算）；左栏图数跟阈值走。选组时排序切到「命中比例」，离开时换回来
    const gg = ref([]);                              // [{name, total, count}]
    const curGroup = computed(() => gg.value.find(x => x.name === gv.group) || null);
    let ggReq = 0, sortBeforeGroup = 'touched';
    async function loadGroupMatch() {
      const my = ++ggReq;
      try {
        const r = await api('GET', '/api/presets/groups/match?min=' + gv.group_min);
        if (my === ggReq) gg.value = r;
      } catch (e) { notify(e.message, 'err'); }
    }
    function leaveGroup() {
      if (!gv.group) return;
      gv.group = '';
      if (gv.sort === 'hit') gv.sort = sortBeforeGroup;
    }
    function setGroup(name) {
      if (!name) leaveGroup();
      else {
        if (gv.sort !== 'hit') { sortBeforeGroup = gv.sort; gv.sort = 'hit'; }
        Object.assign(gv, DIR_RESET, { group: name });
      }
      if (pk.id) savePickFilter(); else gsel.clear();
      loadGallery(1);
    }
    function setGroupMin() {
      lsSet('g.groupMin', String(gv.group_min));
      loadGroupMatch();
      loadGallery(1);
    }
    function goGroup(name) {
      closeImage();
      setGroup(name);
    }

    // ---------- 图库：词频与存预设 ----------
    // 统计当前筛选的全部结果（不只这一页）；勾选一段的词存成这一段的预设
    const st = reactive({ open: false, data: null, min: STATS_MIN, more: new Set(), checked: new Set(),
                          saving: null });
    let stReq = 0;
    async function loadStatsDrawer() {
      const my = ++stReq;
      const qs = gQuery();
      qs.delete('link_of');
      try {
        const d = await api('GET', '/api/images/stats?' + qs);
        if (my === stReq) st.data = d;
      } catch (e) { notify(e.message, 'err'); }
    }
    function openStats() {
      Object.assign(st, { open: true, data: null, saving: null });
      st.more.clear();
      st.checked.clear();
      loadStatsDrawer();
    }
    function closeStats() { st.open = false; st.saving = null; }
    const stTags = s => s.tags.filter(t => g.showBlocked || !t.blocked);
    const stShown = s => stTags(s).filter(t => st.more.has(s.key) || t.ratio * 100 >= st.min);
    const stKey = (s, t) => s.key + '/' + t.id;
    const stChecked = s => stTags(s).filter(t => st.checked.has(stKey(s, t)));
    function stToggle(s, t) { flip(st.checked, stKey(s, t)); }
    function stStartSave(s) {
      st.saving = { key: s.key, name: `${gv.group || gTitle.value}·${s.name}` };
    }
    async function stSave(s, overwrite = false) {
      const words = stChecked(s).map(t => t.word), name = st.saving.name.trim();
      try {
        await api('POST', '/api/presets', { name, bucket: s.key, content: words.join(', '), overwrite });
      } catch (e) {
        if (e.status === 409 && !overwrite) {
          if (confirm(`「${s.name}」段里已经有叫「${name}」的预设：\n${e.data.existing.content}\n\n覆盖它吗？（可以撤销）`)) {
            await stSave(s, true);
          }
          return;
        }
        notify(e.message, 'err');
        return;
      }
      st.saving = null;
      stChecked(s).forEach(t => st.checked.delete(stKey(s, t)));
      await loadOps();
      notify(`已把 ${words.length} 个词存成「${s.name}」段的预设「${name}」`, 'ok', true);
    }

    // ---------- 图库：预设组 ----------
    // 都打开预设组编辑器，只是带来的词和候选来源不同：
    // 词频抽屉带上勾选的词（标「新加」），候选 = 当前筛选的词频；图片详情不带词，候选 = 这张图的词；
    // 工具栏「预设组…」候选 = 当前筛选（要现算）
    const normWord = w => w.trim().toLowerCase().replace(/_/g, ' ').replace(/ +/g, ' ');
    function gFilterSource() {
      const filter = { sort: gv.sort, ...gFilterArgs() };
      if (gv.q.trim()) filter.q = gv.q.trim();
      // 整个空白套图送过去时记上名字，组合器里才有加条、写入这些操作
      const draft = gv.scope === 'draft' && gv.set && !gv.q.trim() && !gv.state ? { draft: gv.set } : {};
      return { filter, label: gFullTitle.value + (gv.q.trim() ? `（含「${gv.q.trim()}」）` : ''), ...draft };
    }
    const gGroupName = () => gv.group || gTitle.value;
    function groupFromStats() {
      const incoming = {};
      for (const sg of st.data.segments) {
        const ws = stChecked(sg).map(t => t.word);
        if (ws.length) incoming[sg.key] = ws;
      }
      openGroupEditor('', { withTarget: true, incoming, name: gGroupName(),
                            extra: { label: '当前筛选的词频：' + gFullTitle.value, stats: st.data },
                            after: () => st.checked.clear() });
    }
    function groupFromImage() {
      const it = gd.item;
      const stats = { total: 1, segments: it.segments.map(sg => ({ key: sg.key, name: sg.name,
        tags: sg.tags.map(t => ({ word: t.word, zh: t.zh, blocked: t.blocked, ratio: 1 })) })) };
      openGroupEditor('', { withTarget: true, extra: { label: '这张图的词：' + (it.stem || it.path), stats } });
    }
    function groupFromGallery() {
      const src = gFilterSource();
      openGroupEditor('', { withTarget: true, name: gGroupName(), extra: { label: '当前筛选：' + src.label, source: src } });
    }

    // ---------- 图库：图片详情 ----------
    // back：从关联的图跳过来时记下来路，抽屉顶部可以返回；翻页、从列表打开都清空
    const gd = reactive({ open: false, item: null, back: [] });
    let gdReq = 0;
    async function openImage(id, keepBack = false) {
      const my = ++gdReq;
      gd.open = true;
      if (!keepBack) gd.back = [];
      try {
        const item = await api('GET', `/api/images/${id}?` + gQuery());
        if (my === gdReq && gd.open) gd.item = item;
      } catch (e) { notify(e.message, 'err'); if (!gd.item) gd.open = false; }
    }
    function closeImage() { gd.open = false; gd.item = null; gd.back = []; }
    function openLinked(x) {
      gd.back.push({ id: gd.item.id, stem: gd.item.stem });
      openImage(x.id, true);
    }
    function goBack() {
      const b = gd.back.pop();
      if (b) openImage(b.id, true);
    }
    function unlink(role, x) {
      const it = gd.item;
      grun('POST', `/api/images/${it.id}/links`, { role, remove: [x.id] },
           () => `已取消「${it.stem}」和「${x.stem}」的关联`);
    }

    // ---------- 图库：挑图模式（给一张图关联生成图 / 原图）----------
    // 复用图库页挑图：筛选、搜词、翻页照常；勾选后「确定」一次建好，退出时恢复进入前的筛选并回到那张图的详情
    const pk = reactive({ id: null, role: '', stem: '', saved: null });
    function savePickFilter() {
      const f = {};
      for (const k of GV_FILTER_KEYS) f[k] = gv[k];
      lsSet('g.pick', JSON.stringify(f));
    }
    function startPick(role) {
      const it = gd.item;
      const saved = { q: gv.q };
      for (const k of GV_FILTER_KEYS) saved[k] = gv[k];
      Object.assign(pk, { id: it.id, role, stem: it.stem, saved });
      closeImage();
      gsel.clear();
      let f = null;
      try { f = JSON.parse(lsGet('g.pick') || 'null'); } catch (e) { f = null; }
      if (f) for (const k of GV_FILTER_KEYS) gv[k] = f[k] || '';
      gv.q = '';
      loadGallery(1);
    }
    function endPick() {
      const id = pk.id;
      Object.assign(gv, pk.saved);
      Object.assign(pk, { id: null, role: '', stem: '', saved: null });
      gsel.clear();
      loadGallery(1);
      openImage(id);
    }
    async function confirmPick() {
      if (!gsel.size) { notify('还没有勾选图片', 'err'); return; }
      savePickFilter();                            // 下次挑图从这里开始
      const what = pk.role === 'gen' ? '生成图' : '原图', stem = pk.stem;
      const r = await grun('POST', `/api/images/${pk.id}/links`, { role: pk.role, add: [...gsel] },
                           r => `给「${stem}」关联了 ${r.changed} 张${what}` + (r.skipped ? `，${r.skipped} 张已经关联过` : ''));
      if (r) endPick();
    }
    function jumpToImage(id) {
      closeDetail();
      if (detail.open) return;                     // 词详情没保存、用户取消了关闭
      switchTab('gallery');
      openImage(id);
    }

    // ---------- 图库：在图上改词 ----------
    // 词典级操作（归段、拉黑）与单图操作（增删词、调序）都可撤销
    async function grun(method, url, body, okText) {
      try {
        const r = await api(method, url, body);
        await refresh();
        notify(okText(r), 'ok', true);
        return r;
      } catch (e) { notify(e.message, 'err'); }
    }
    const wmenu = reactive({ open: false, x: 0, y: 0, img: null, seg: null, tag: null });
    g.openWordMenu = (item, seg, tag, ev) => {
      Object.assign(wmenu, {
        open: true, img: item, seg, tag,
        x: Math.max(8, Math.min(ev.clientX, window.innerWidth - 320)),
        y: Math.max(8, Math.min(ev.clientY + 8, window.innerHeight - 300)),
      });
    };
    function wordAssign(key) {
      const t = wmenu.tag, b = buckets.value.find(x => x.key === key);
      wmenu.open = false;
      grun('POST', '/api/tags/assign', { ids: [t.id], bucket: key },
           r => r.changed ? `「${t.word}」归到「${b.name}」，${t.freq} 张图一起改了` : '已经在这个段了');
    }
    function wordBlock() {
      const t = wmenu.tag, flag = !t.blocked;
      wmenu.open = false;
      grun('POST', '/api/tags/block', { ids: [t.id], blocked: flag },
           () => `「${t.word}」${flag ? '加入' : '移出'}黑名单`);
    }
    function wordRemove() {
      const t = wmenu.tag, it = wmenu.img;
      wmenu.open = false;
      grun('POST', `/api/images/${it.id}/tags`, { remove: [t.id] }, () => `已从「${it.stem}」删掉「${t.word}」`);
    }
    async function wordInDict() {
      const word = wmenu.tag.word;
      wmenu.open = false;
      closeImage();
      Object.assign(view, { filter: 'all', q: word });
      switchTab('dict');
      await reload(1);
      const t = page.value.items.find(x => x.word === word);
      if (t) openDetail(t);
    }

    g.startAdd = (item, seg, ev) => openPicker(ev, {
      title: `给「${item.stem}」加词`, bucket: seg.key,
      have: item.segments.flatMap(x => x.tags.map(t => t.word)),
      done: words => grun('POST', `/api/images/${item.id}/tags`, { add: words.join(', '), bucket: seg.key }, r => {
        const parts = [r.changed ? `给「${item.stem}」加了 ${r.changed} 个词` : '没有加新词'];
        if (r.already.length) parts.push(`已经有的：${r.already.join(', ')}`);
        if (r.elsewhere.length) parts.push('按词典落在别的段：' +
          r.elsewhere.map(e => `${e.word} → ${e.bucket_name}`).join('，'));
        return parts.join('；');
      }),
    });

    // ---------- 挑词框：图上的 ＋、组合器槽的「挑词…」共用 ----------
    // 按段从词典挑（按出现次数），可以换段、搜词、多选；搜索框回车把输入的词原样加进已选，空着回车就确定。
    // have：已经有的词（按规范化写法比较），列出来但不能选；done(words)：确定时调用
    const wp = reactive({ open: false, x: 0, y: 0, title: '', tab: '', q: '', items: [], loading: false, err: '',
                          more: false, picked: [], have: new Set(), done: null });
    function openPicker(ev, opts) {
      const r = ev.currentTarget.getBoundingClientRect();
      const below = r.bottom + 4 + WP_H <= window.innerHeight;
      Object.assign(wp, {
        open: true, title: opts.title, tab: opts.bucket, q: '', items: [], err: '', more: false, picked: [],
        have: new Set((opts.have || []).map(normWord)), done: opts.done,
        x: Math.max(8, Math.min(r.left, window.innerWidth - WP_W - 8)),
        y: below ? r.bottom + 4 : Math.max(8, r.top - WP_H - 4),
      });
      wpLoad();
    }
    let wpReq = 0, wpTimer = null;
    async function wpLoad() {
      const my = ++wpReq;
      wp.loading = true;
      try {
        const qs = new URLSearchParams({ bucket: wp.tab, sort: 'freq', size: PE_DICT, blocked: '0' });
        if (wp.q.trim()) qs.set('q', wp.q.trim());
        const r = await api('GET', '/api/tags?' + qs);
        if (my === wpReq) Object.assign(wp, { items: r.items, err: '' });
      } catch (e) { if (my === wpReq) wp.err = '候选词没取到：' + e.message; }
      finally { if (my === wpReq) wp.loading = false; }
    }
    function wpSetTab(key) { wp.tab = key; wp.more = false; wpLoad(); }
    function wpSearch() {
      wp.more = false;
      clearTimeout(wpTimer);
      wpTimer = setTimeout(wpLoad, PE_SUGG_DELAY);
    }
    const wpShown = computed(() => (wp.more ? wp.items : wp.items.slice(0, WP_CAND)));
    const wpHad = w => wp.have.has(normWord(w));
    const wpOn = w => wp.picked.some(x => normWord(x) === normWord(w));
    function wpToggle(w) {
      if (wpHad(w)) return;
      if (wpOn(w)) wp.picked = wp.picked.filter(x => normWord(x) !== normWord(w));
      else wp.picked.push(w);
    }
    function wpEnter() {
      if (!wp.q.trim()) { wpDone(); return; }
      for (const w of splitWords(wp.q)) if (!wpOn(w) && !wpHad(w)) wp.picked.push(w);
      wp.q = '';
      wpSearch();
    }
    function wpDone() {
      const words = wp.picked.slice(), done = wp.done;
      wp.open = false;
      if (words.length) done(words);
    }

    // 段内拖动调整词序（只调这张图）
    g.tagDragStart = (item, seg, tag, ev) => {
      Object.assign(g.drag, { img: item.id, seg: seg.key, tag: tag.id, over: null });
      ev.dataTransfer.effectAllowed = 'move';
    };
    g.tagDragOver = (item, seg, tag) => {
      if (g.drag.img === item.id && g.drag.seg === seg.key) g.drag.over = tag.id;
    };
    g.tagDragEnd = () => Object.assign(g.drag, { img: null, seg: null, tag: null, over: null });
    g.tagDrop = (item, seg, tag) => {
      const from = g.drag.tag, same = g.drag.img === item.id && g.drag.seg === seg.key;
      g.tagDragEnd();
      if (!same || from === tag.id) return;
      const ids = seg.tags.map(t => t.id);
      const below = ids.indexOf(from) < ids.indexOf(tag.id);   // 往后拖放到目标之后，往前拖放到目标之前
      const order = ids.filter(i => i !== from);
      order.splice(order.indexOf(tag.id) + (below ? 1 : 0), 0, from);
      grun('POST', `/api/images/${item.id}/tags`, { order }, () => `已调整「${item.stem}」的「${seg.name}」词序`);
    };

    // ---------- 图库：反推选中的图 ----------
    const tgDlg = reactive({ open: false, ids: [], label: '', skip_done: false, overwrite_edited: false, busy: false });
    async function openRetag(ids, label) {
      if (!ids.length) { notify('没有能反推的图（仅导入记录或文件已不在的不能反推）', 'err'); return; }
      Object.assign(tgDlg, { open: true, ids, label, skip_done: false, overwrite_edited: false, busy: false });
      if (!tg.status) await loadTagger();
    }
    async function retagSelected() {
      const ids = gpage.value.items.filter(it => gsel.has(it.id) && canTag(it)).map(it => it.id);
      const other = [...gsel].filter(id => !gpage.value.items.some(it => it.id === id));
      openRetag(ids.concat(other), `选中的 ${gsel.size} 张图`);
    }
    // 当前筛选（文件夹 / 套图 / 状态 / 搜词）下的全部图，分页取 id；出错返回 null
    async function filterIds(pred) {
      const ids = [];
      try {
        for (let p = 1; ; p++) {
          const qs = gQuery();
          qs.set('page', p); qs.set('size', IDS_PAGE);
          const r = await api('GET', '/api/images?' + qs);
          r.items.filter(pred).forEach(it => ids.push(it.id));
          if (p * r.size >= r.total) break;
        }
      } catch (e) { notify(e.message, 'err'); return null; }
      return ids;
    }
    async function retagFilter() {
      const ids = await filterIds(canTag);
      if (ids) openRetag(ids, `「${gTitle.value}」下的 ${ids.length} 张图`);
    }
    async function doRetag() {
      tgDlg.busy = true;
      try {
        await api('POST', '/api/tagger/run', {
          ids: tgDlg.ids, model: tg.model, threshold: tg.threshold, character_threshold: tg.character_threshold,
          exclude_tags: tg.exclude_tags, skip_done: tgDlg.skip_done, overwrite_edited: tgDlg.overwrite_edited,
        });
        tgDlg.open = false;
        gsel.clear();
        notify('反推已开始，顶部可以看进度');
        await loadTagger();
      } catch (e) { notify(e.message, 'err'); }
      tgDlg.busy = false;
    }

    // ---------- 扫描图库 ----------
    const sc = reactive({ status: null, root: lsGet('scan.root'), dir: lsGet('scan.dir') });
    const scJob = computed(() => (sc.status && sc.status.job) || {});
    const scRunning = computed(() => !!scJob.value.running);
    const scPercent = computed(() => {
      const j = scJob.value;
      return j.total ? Math.round(j.done / j.total * 100) : 0;
    });
    let scTimer = null;
    async function loadScan() {
      try {
        const s = await api('GET', '/api/scan/status');
        const wasRunning = scRunning.value;
        sc.status = s;
        if (!sc.root) sc.root = s.root || '';
        if (s.job.running) {
          clearTimeout(scTimer);
          scTimer = setTimeout(loadScan, 1000);
        } else if (wasRunning) {
          await refresh();
          const r = s.job.report || {};
          notify(`扫描${s.job.phase}：新登记 ${r.new || 0} 张，回填 ${(r.backfilled || []).length} 张`,
                 s.job.error ? 'err' : 'ok');
        }
      } catch (e) { notify(e.message, 'err'); }
    }
    async function startScan() {
      lsSet('scan.root', sc.root);
      lsSet('scan.dir', sc.dir);
      try { await api('POST', '/api/scan', { root: sc.root, dir: sc.dir }); await loadScan(); }
      catch (e) { notify(e.message, 'err'); }
    }
    async function stopScan() {
      try { await api('POST', '/api/scan/stop'); await loadScan(); } catch (e) { notify(e.message, 'err'); }
    }
    onMounted(loadScan);     // 刷新页面时如果扫描还在跑，接着显示进度
    // 当前筛选对应的图库子目录（相对图库根）
    const scanDirOfFilter = computed(() => gv.dir || '');
    function scanFilterDir() {
      sc.dir = scanDirOfFilter.value;
      openImport('scan');
    }
    const shortList = list => (list || []).slice(0, REPORT_LIST);
    const moreCount = list => Math.max(0, (list || []).length - REPORT_LIST);

    // ---------- 组合器 ----------
    // 来源图按段的词 → 每段一个槽批量替换 → 预览 → 导出 txt 目录 / xlsx。计算和拼接都在后端（composer.py）
    // 槽设置、来源、输出目录记在 localStorage（个人便利项），正式记录是导出时写的 _组合记录.json
    const cm = reactive({
      source: null,                  // {ids} 或 {filter}，加 label
      slots: {},                     // {段 key: {mode, presets: [id], text}}
      page: null, loading: false, error: '',
      config: { output_dir: '', max_source: 1000 },
    });
    try { cm.source = JSON.parse(lsGet('cm.source') || 'null'); } catch (e) { cm.source = null; }
    // 旧版按 区 / 分级 / 角色 筛的来源后端已经不认，留着会变成全部图片，清掉
    if (cm.source && cm.source.filter && ['zone', 'category', 'char'].some(k => k in cm.source.filter)) cm.source = null;
    try { cm.slots = JSON.parse(lsGet('cm.slots') || '{}') || {}; } catch (e) { cm.slots = {}; }
    // 每段一个槽：段有增删时补齐（不在渲染里补，避免渲染时改状态）
    function ensureSlots() {
      for (const b of buckets.value) {
        const s = cm.slots[b.key];
        if (!s || !CM_MODES.some(m => m.key === s.mode)) cm.slots[b.key] = { mode: 'keep', presets: [], text: '', exclude: [] };
        else if (!Array.isArray(s.exclude)) s.exclude = [];
      }
    }
    watch(buckets, ensureSlots, { immediate: true });
    const cmSlot = key => cm.slots[key] || { mode: 'keep', presets: [], text: '', exclude: [] };
    const cmFill = key => CM_MODES.find(m => m.key === cmSlot(key).mode).fill;
    const cmChanged = computed(() => buckets.value.filter(b => cm.slots[b.key] && cm.slots[b.key].mode !== 'keep'));

    let cmTimer = null, cmReq = 0;
    function cmPreviewLater() { clearTimeout(cmTimer); cmTimer = setTimeout(() => cmPreview(1), CM_PREVIEW_DELAY); }
    async function cmPreview(p = (cm.page && cm.page.page) || 1) {
      if (!cm.source) { cm.page = null; return; }
      const my = ++cmReq;
      cm.loading = true;
      try {
        const r = await api('POST', '/api/composer/preview', { source: cm.source, slots: cm.slots, page: p, size: 60 });
        if (my === cmReq) Object.assign(cm, { page: r, error: '' });
      } catch (e) { if (my === cmReq) Object.assign(cm, { page: null, error: e.message }); }
      if (my === cmReq) cm.loading = false;
    }
    watch(() => cm.slots, () => { lsSet('cm.slots', JSON.stringify(cm.slots)); cmPreviewLater(); }, { deep: true });
    function setSource(src) {
      cm.source = src;
      lsSet('cm.source', JSON.stringify(src));
      cmPreview(1);
    }
    // 图库「送到组合器」：有勾选送勾选的图，没有就送当前筛选的全部结果（按当前排序）
    function sendToComposer() {
      let src;
      if (gsel.size) src = { ids: [...gsel], label: `图库里勾选的 ${gsel.size} 张` };
      else src = gFilterSource();
      gsel.clear();
      switchTab('composer');
      setSource(src);
    }
    // 组合器里直接选：直接放了图的文件夹（只取这一层，不含子文件夹）
    const cmSetOptions = computed(() => {
      const out = [];
      if (gtree.value.root_own) {
        out.push({ label: '（根目录）', count: gtree.value.root_own, filter: { dir: '', dir_only: '1', sort: 'name' } });
      }
      const walk = nodes => {
        for (const n of nodes) {
          if (n.own) out.push({ label: n.path.split('/').join(' / '), count: n.own, filter: { dir: n.path, dir_only: '1', sort: 'name' } });
          walk(n.children);
        }
      };
      walk(gtree.value.dirs);
      return out;
    });
    function onPickSource(ev) {
      const v = ev.target.value;
      ev.target.value = '';
      if (!v) return;
      const [kind, i] = v.split(':');
      if (kind === 'draft') setSource(draftSource(dr.items[Number(i)].name));
      else if (kind === 'group') {
        const x = gg.value[Number(i)];
        setSource({ filter: { group: x.name, group_min: gv.group_min, sort: 'hit' }, label: `预设组「${x.name}」` });
      } else {
        const o = cmSetOptions.value[Number(i)];
        setSource({ filter: o.filter, label: `套图「${o.label}」` });
      }
    }
    function resetSlots() { cm.slots = {}; ensureSlots(); }

    // ---------- 组合器：空白套图 ----------
    // 从 0 创建的套图：每条是一份空白提示词（后端存成没有文件的图，见 drafts.py）。来源设成它以后，
    // 可以加条、删条、改名，「写入套图」把槽的组合结果写进各条，「编辑」打开图片详情逐条手改。都可撤销
    const dr = reactive({ items: [], sel: new Set(), dlg: false, name: '', count: 10, addN: 5, busy: false });
    async function loadDrafts() {
      try { dr.items = await api('GET', '/api/drafts'); } catch (e) { notify(e.message, 'err'); }
    }
    const draftSource = name => ({ filter: { scope: 'draft', set: name, sort: 'name' }, label: `空白套图「${name}」`, draft: name });
    const cmDraft = computed(() => (cm.source && cm.source.draft) || '');
    watch(cmDraft, () => dr.sel.clear());
    // 改了条以后：组合器预览、图库、操作日志都要跟着变
    async function draftCall(method, url, body, okText) {
      dr.busy = true;
      try {
        const r = await api(method, url, body);
        await refresh();
        notify(okText(r), 'ok', true);
        return r;
      } catch (e) { notify(e.message, 'err'); return null; }
      finally { dr.busy = false; }
    }
    function openNewDraft() {
      const d = new Date(), pad = n => String(n).padStart(2, '0');
      Object.assign(dr, { dlg: true, name: `套图-${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}`, count: 10 });
    }
    async function createDraft() {
      const name = dr.name.trim();
      const r = await draftCall('POST', '/api/drafts', { name, count: dr.count }, r => `已新建空白套图「${r.name}」（${r.ids.length} 条）`);
      if (!r) return;
      dr.dlg = false;
      setSource(draftSource(r.name));
    }
    function addDraftItems(n) {
      draftCall('POST', '/api/drafts/items', { name: cmDraft.value, count: n }, r => `加了 ${r.ids.length} 条`);
    }
    async function deleteDraftItems() {
      const ids = [...dr.sel];
      if (!ids.length || !confirm(`删掉选中的 ${ids.length} 条（连同它们的词）？可以撤销。`)) return;
      if (await draftCall('DELETE', '/api/drafts/items', { ids }, r => `删掉了 ${r.count} 条`)) dr.sel.clear();
    }
    async function renameDraft() {
      const old = cmDraft.value, name = (prompt('新的套图名', old) || '').trim();
      if (!name || name === old) return;
      const r = await draftCall('PATCH', '/api/drafts', { name: old, new_name: name }, r => `已改名为「${r.name}」`);
      if (r) setSource(draftSource(r.name));
    }
    async function deleteDraft() {
      const name = cmDraft.value, d = dr.items.find(x => x.name === name);
      if (!confirm(`删除空白套图「${name}」${d ? `（${d.count} 条）` : ''}？可以撤销。`)) return;
      if (await draftCall('DELETE', '/api/drafts', { name }, () => `已删除空白套图「${name}」`)) setSource(null);
    }
    // 有勾选只写勾选的条（没勾的不动），没勾选写全部
    async function applyToDraft() {
      const ids = [...dr.sel];
      const what = ids.length ? `选中的 ${ids.length} 条` : `「${cmDraft.value}」的每一条`;
      if (!confirm(`把现在预览里的结果写进${what}？这些条原来的词会被整份换掉，可以撤销。`)) return;
      await draftCall('POST', '/api/composer/apply', { source: ids.length ? { ids } : cm.source, slots: cm.slots },
                      r => `已写入 ${r.count} 条` + (r.lora ? '（LoRA 不写进去，导出时在槽里放上带 LoRA 的预设）' : ''));
    }
    function toggleDraftRow(id) { dr.sel.has(id) ? dr.sel.delete(id) : dr.sel.add(id); }
    const draftPageAll = computed(() => !!cm.page && cm.page.items.length > 0 && cm.page.items.every(r => dr.sel.has(r.image_id)));
    function toggleDraftPage() {
      const on = !draftPageAll.value;
      for (const r of cm.page.items) on ? dr.sel.add(r.image_id) : dr.sel.delete(r.image_id);
    }
    function cmAddPreset(key, id) {
      const s = cmSlot(key);
      if (!cmFill(key)) s.mode = 'replace';
      if (id && !s.presets.includes(Number(id))) s.presets.push(Number(id));
    }
    // 槽里挑词：接在手写框后面；已经手写过的词不能再选
    function cmPickWords(b, ev) {
      const s = cmSlot(b.key);
      openPicker(ev, {
        title: `「${b.name}」槽${s.mode === 'remove' ? '要删的词' : '手写的词'}`, bucket: b.key, have: splitWords(s.text),
        done: words => {                                   // 确定时再取槽，期间槽对象可能被换掉
          const cur = cmSlot(b.key);
          cur.text = [...splitWords(cur.text), ...words].join(', ');
        },
      });
    }
    function cmRemovePreset(key, id) {
      const s = cmSlot(key);
      s.presets = s.presets.filter(x => x !== id);
      // 只留还在槽里的预设用得到的去掉标记，免得以后再放进来的预设莫名少词
      const left = new Set(s.presets.map(presetById).filter(Boolean).flatMap(p => splitWords(p.content)).map(normWord));
      s.exclude = s.exclude.filter(w => left.has(normWord(w)));
    }

    // ---------- 槽里微调 ----------
    // 槽里每个预设的词列成 chip，点一下只在这个槽里去掉（exclude，划掉显示），再点恢复，不改预设。
    // 「存回预设」把去掉的词从各自的预设里删掉、手写的词加到第一个预设末尾，然后清空 exclude 和手写框
    const cmExcluded = (key, w) => cmSlot(key).exclude.some(x => normWord(x) === normWord(w));
    function cmToggleWord(key, w) {
      const s = cmSlot(key);
      if (cmExcluded(key, w)) s.exclude = s.exclude.filter(x => normWord(x) !== normWord(w));
      else s.exclude.push(w);
    }
    // 槽里的预设（已删的跳过），每个带上按 exclude 去掉以后的新内容
    function cmSlotEdits(key) {
      const s = cmSlot(key);
      const ps = s.presets.map(presetById).filter(Boolean);
      const out = ps.map(p => {
        const words = splitWords(p.content);
        return { p, words: words.filter(w => !cmExcluded(key, w)), changed: words.some(w => cmExcluded(key, w)) };
      });
      const typed = splitWords(s.text);
      if (out.length && typed.length) {
        const have = new Set(out[0].words.map(normWord));
        for (const w of typed) if (!have.has(normWord(w))) { out[0].words.push(w); have.add(normWord(w)); out[0].changed = true; }
      }
      return out.filter(x => x.changed);
    }
    const cmSlotDirty = key => cmFill(key) && cmSlotEdits(key).length > 0;
    async function saveSlotToPresets(key) {
      const edits = cmSlotEdits(key);
      if (!edits.length || pr.busy) return;
      pr.busy = true;
      let done = 0;
      try {
        for (const x of edits) {
          await api('PATCH', '/api/presets/' + x.p.id, { content: x.words.join(', ') });
          done++;
        }
        const s = cmSlot(key);
        s.exclude = [];
        s.text = '';
        notify(`已存回预设：${edits.map(x => x.p.name).join('、')}` + (done > 1 ? `（撤销要按 ${done} 次）` : ''), 'ok', true);
      } catch (e) { notify((done ? `存了 ${done} 个后出错：` : '') + e.message, 'err'); }
      finally {
        pr.busy = false;
        await Promise.all([loadPresets(), loadOps()]);
        cmPreviewLater();
      }
    }
    // 整组套用：组里每个预设按「替换」填进它那段的槽
    function applyGroup(ev) {
      const name = ev.target.value;
      ev.target.value = '';
      if (name) applyGroupByName(name);
    }
    function applyGroupByName(name) {
      const items = pr.items.filter(p => p.name === name);
      if (!items.length) return;
      for (const p of items) cm.slots[p.bucket] = { mode: 'replace', presets: [p.id], text: '', exclude: [] };
      notify(`已套用预设组「${name}」：${items.map(p => p.bucket_name).join('、')}`);
    }
    // 从左栏把预设拖进槽
    // 从左栏拖预设组卡片（group 是组名），放到任何一个槽上都是整组套用；拖动时组里各段的槽一起高亮
    const cmDrag = reactive({ group: null, over: null });
    function groupDragStart(name, ev) { cmDrag.group = name; ev.dataTransfer.effectAllowed = 'copy'; }
    function groupDragEnd() { cmDrag.group = null; cmDrag.over = null; }
    function slotDrop() {
      const grp = cmDrag.group;
      groupDragEnd();
      if (grp) applyGroupByName(grp);
    }
    const slotHot = key => !!cmDrag.group && (cmDrag.over !== null) && groupBuckets(cmDrag.group).includes(key);
    const presetById = id => pr.items.find(p => p.id === id);
    const cmPageCount = computed(() => (cm.page ? Math.max(1, Math.ceil(cm.page.total / cm.page.size)) : 1));
    const bucketName = key => (buckets.value.find(b => b.key === key) || {}).name || key;

    // 预设库（左栏）：按预设组列，同名的预设是一组（只有一段的也算一组）。增删改都可撤销
    const pr = reactive({ items: [], groups: [], q: '', busy: false });
    async function loadPresets() {
      try {
        const [items, groups] = await Promise.all([api('GET', '/api/presets'), api('GET', '/api/presets/groups'),
                                                   loadGroupMatch()]);
        Object.assign(pr, { items, groups });
        // 删除或撤销新建后，槽里不能再引用已经不在的预设，否则预览会报错；
        // 删掉的预设 id 可能被别的段的新预设重用，所以还要核对段
        const bucketOf = new Map(items.map(p => [p.id, p.bucket]));
        for (const [key, s] of Object.entries(cm.slots)) {
          const ok = id => bucketOf.get(id) === key;
          if (s.presets.some(id => !ok(id))) s.presets = s.presets.filter(ok);
        }
      } catch (e) { notify(e.message, 'err'); }
    }
    const prOf = key => pr.items.filter(p => p.bucket === key);
    const bOrd = key => buckets.value.findIndex(b => b.key === key);
    // 每组一张卡片，组里有星标的排前面；搜索看名字、内容、LoRA
    const prGroupList = computed(() => {
      const q = pr.q.trim().toLowerCase();
      const by = new Map();
      for (const p of pr.items) {
        if (!by.has(p.name)) by.set(p.name, []);
        by.get(p.name).push(p);
      }
      const hit = ps => !q || ps.some(p => p.name.toLowerCase().includes(q) || p.content.toLowerCase().includes(q)
        || (p.lora || '').toLowerCase().includes(q));
      return [...by].filter(([, ps]) => hit(ps))
        .map(([name, ps]) => ({ name, starred: ps.some(p => p.starred),
                                presets: ps.slice().sort((a, b) => bOrd(a.bucket) - bOrd(b.bucket)) }))
        .sort((a, b) => (b.starred - a.starred) || a.name.localeCompare(b.name, 'zh'));
    });
    const groupBuckets = name => pr.items.filter(p => p.name === name).map(p => p.bucket);
    async function savePresetGroupCall(body, okText) {
      pr.busy = true;
      try {
        await api('PUT', '/api/presets/group', body);
        if (body.new_name && body.name && body.new_name !== body.name && gv.group === body.name) gv.group = body.new_name;
        await refresh();                           // 槽里用着的预设、按组筛选的图都可能变了
        notify(okText, 'ok', true);
        return true;
      } catch (e) { notify(e.message, 'err'); return false; }
      finally { pr.busy = false; }
    }
    function toggleGroupStar(grp) {
      savePresetGroupCall({ name: grp.name, starred: !grp.starred,
                            items: grp.presets.map(p => ({ bucket: p.bucket, content: p.content, lora: p.lora })) },
                          `「${grp.name}」${grp.starred ? '取消' : '加上'}星标`);
    }
    const splitWords = text => (text || '').split(/[,\n]/).map(w => w.trim()).filter(Boolean);

    // 词典：选中的词按各自的段带进编辑器，候选 = 词典
    function groupFromDict() {
      const incoming = {};
      for (const t of [...selected.values()].sort((a, b) => b.freq - a.freq)) (incoming[t.bucket] ||= []).push(t.word);
      openGroupEditor('', { withTarget: true, incoming, src: 'dict', after: () => selected.clear() });
    }
    // 词详情：这个词在哪些预设组里（按规范化写法比，任何段）
    const detailGroups = computed(() => {
      if (!detail.tag) return [];
      const w = normWord(detail.tag.word);
      return [...new Set(pr.items.filter(p => splitWords(p.content).some(x => normWord(x) === w)).map(p => p.name))];
    });

    // ---------- 预设组编辑器 ----------
    // 各入口共用：openGroupEditor(组名，新建给空)。左边是组里每段已选的词（× 去掉、同段拖动排序、LoRA），
    // 右边挑词：选候选来源（组合器来源 / 词典 / 主题 / 套图）和段，点词加入或去掉。段不用先加，有词的段就进组。
    // 保存是整组 PUT（新建、改名、加段、去段一次写完），整组一次撤销
    // 从图库、词典打开时（withTarget）可以选「新建」还是「加到已有组」；带来的词（incoming）加进去并标「新加」（fresh），
    // 都能去掉。extra 是这个入口自带的候选来源（{label, stats} 现成的统计，或 {label, source} 现算）
    const pe = reactive({ open: false, orig: '', name: '', segs: {}, tab: '', src: '', cand: null, dict: [], q: '',
                          more: false, loading: false, err: '', busy: false, drag: null,
                          withTarget: false, incoming: {}, fresh: [], suggest: '', extra: null, after: null });
    const peSources = computed(() => [
      ...(pe.extra ? [{ v: 'extra', label: pe.extra.label }] : []),
      ...(cm.source ? [{ v: 'composer', label: '组合器来源：' + cm.source.label }] : []),
      { v: 'dict', label: '词典（按出现次数）' },
      ...gg.value.map(x => ({ v: 'group:' + x.name, label: `预设组「${x.name}」的图（${x.count}）` })),
      ...cmSetOptions.value.map((o, i) => ({ v: 'set:' + i, label: '套图：' + o.label })),
    ]);
    // 按组名（新建给空）把组里的词摆到左边，再把带来的词接在后面（已有的不重复加），记下哪些是新加的
    function peBuild(name, incoming) {
      const items = name ? pr.items.filter(p => p.name === name) : [];
      const segs = {}, fresh = [];
      for (const b of buckets.value) {
        const p = items.find(x => x.bucket === b.key);
        const words = p ? splitWords(p.content) : [];
        for (const w of incoming[b.key] || []) {
          if (words.some(x => normWord(x) === normWord(w))) continue;
          words.push(w);
          fresh.push(b.key + '|' + normWord(w));
        }
        segs[b.key] = { words, lora: p ? (p.lora || '').split('\n').filter(Boolean).join(', ') : '' };
      }
      Object.assign(pe, { orig: name, name: name || pe.suggest, segs, fresh });
    }
    // opts：{withTarget, incoming: {段: [词]}, name: 新建时建议的名字, extra, after: 保存成功后调}
    function openGroupEditor(name = '', opts = {}) {
      Object.assign(pe, { open: true, withTarget: !!opts.withTarget, incoming: opts.incoming || {}, suggest: opts.name || '',
                          extra: opts.extra || null, after: opts.after || null, src: '', cand: null, dict: [], q: '',
                          more: false, busy: false, drag: null, err: '' });
      peBuild(name, pe.incoming);
      const first = buckets.value.find(b => pe.segs[b.key].words.length) || buckets.value[0];
      pe.tab = first ? first.key : '';
      const last = lsGet('cm.peSrc');
      const src = opts.extra ? 'extra' : opts.src
        || (peSources.value.some(x => x.v === last) ? last : (cm.source ? 'composer' : 'dict'));
      peSetSrc(src);
    }
    const peFresh = (key, w) => pe.fresh.includes(key + '|' + normWord(w));
    // 换目标：新建或某个已有组，带来的词重新接上（左边手动改的会丢掉）
    function peSetTarget(name) { peBuild(name, pe.incoming); }
    // 新建时撞名：把左边现在的词都当作带来的词，加到那个组里
    function peMergeInto(name) {
      const incoming = {};
      for (const b of buckets.value) if (pe.segs[b.key].words.length) incoming[b.key] = pe.segs[b.key].words.slice();
      const lora = Object.fromEntries(buckets.value.map(b => [b.key, pe.segs[b.key].lora]));
      pe.incoming = incoming;
      pe.withTarget = true;
      peBuild(name, incoming);
      for (const b of buckets.value) if (!pe.segs[b.key].lora.trim() && lora[b.key].trim()) pe.segs[b.key].lora = lora[b.key];
    }
    const peGroupNames = computed(() => [...new Set(pr.items.map(p => p.name))].sort((a, b) => a.localeCompare(b, 'zh')));
    const peNameTaken = computed(() => {
      const n = pe.name.trim();
      return !!n && n !== pe.orig && pr.items.some(p => p.name === n);
    });
    // 候选来源：词典按段现查；其余是一批图，整批统计一次（同图库「词频」）
    async function peSetSrc(v) {
      pe.src = v;
      pe.cand = null;
      pe.more = false;
      pe.err = '';
      if (v !== 'extra') lsSet('cm.peSrc', v);              // 入口自带的来源不记
      if (v === 'dict') { peLoadDict(); return; }
      if (v === 'extra' && pe.extra.stats) { pe.cand = pe.extra.stats; return; }
      let source = null;
      if (v === 'extra') source = pe.extra.source;
      else if (v === 'composer') source = cm.source;
      else if (v.startsWith('group:')) source = { filter: { group: v.slice(6), group_min: gv.group_min, sort: 'hit' } };
      else if (v.startsWith('set:')) source = { filter: cmSetOptions.value[Number(v.slice(4))].filter };
      if (!source) return;
      pe.loading = true;
      try {
        const r = await api('POST', '/api/composer/stats', { source });
        if (pe.src === v) pe.cand = r;
      } catch (e) { if (pe.src === v) pe.err = '候选词没取到：' + e.message; }
      finally { pe.loading = false; }
    }
    let peDictReq = 0, peDictTimer = null;
    async function peLoadDict() {
      if (pe.src !== 'dict' || !pe.tab) return;
      const my = ++peDictReq;
      pe.loading = true;
      try {
        const qs = new URLSearchParams({ bucket: pe.tab, sort: 'freq', size: PE_DICT, blocked: '0' });
        if (pe.q.trim()) qs.set('q', pe.q.trim());
        const r = await api('GET', '/api/tags?' + qs);
        if (my === peDictReq) Object.assign(pe, { dict: r.items, err: '' });
      } catch (e) { if (my === peDictReq) pe.err = '候选词没取到：' + e.message; }
      finally { if (my === peDictReq) pe.loading = false; }
    }
    function peSetTab(key) { pe.tab = key; pe.more = false; peLoadDict(); }
    function peSearch() {
      pe.more = false;
      if (pe.src !== 'dict') return;
      clearTimeout(peDictTimer);
      peDictTimer = setTimeout(peLoadDict, PE_SUGG_DELAY);
    }
    // 当前段的候选：{w, zh, n}，已选的也列（点一下去掉）；图的来源在本地按搜索框过滤，词典在后端搜
    const peCandAll = computed(() => {
      if (pe.src === 'dict') return pe.dict.map(t => ({ w: t.word, zh: t.zh, n: t.freq + ' 次' }));
      const seg = pe.cand && pe.cand.segments.find(x => x.key === pe.tab);
      const q = normWord(pe.q);
      return (seg ? seg.tags : []).filter(t => !t.blocked)
        .filter(t => !q || normWord(t.word).includes(q) || (t.zh || '').includes(pe.q.trim()))
        .map(t => ({ w: t.word, zh: t.zh, n: pe.cand.total > 1 ? Math.round(t.ratio * 100) + '%' : '' }));
    });
    const peCandShown = computed(() => (pe.more ? peCandAll.value : peCandAll.value.slice(0, PE_CAND)));
    const peHas = (key, w) => pe.segs[key].words.some(x => normWord(x) === normWord(w));
    function peToggle(w) {
      const s = pe.segs[pe.tab];
      if (peHas(pe.tab, w)) s.words = s.words.filter(x => normWord(x) !== normWord(w));
      else s.words.push(w);
    }
    // 搜索框回车：把输入的词（逗号分隔）直接加进当前段，词典里没有的也行
    function peAddTyped() {
      const s = pe.segs[pe.tab];
      for (const w of splitWords(pe.q)) if (!peHas(pe.tab, w)) s.words.push(w);
      pe.q = '';
      peSearch();
    }
    // 左边显示的段：有词或 LoRA 的段，加上右边正在挑的段
    const peSegList = computed(() => buckets.value.filter(b => pe.segs[b.key]
      && (pe.segs[b.key].words.length || pe.segs[b.key].lora.trim() || b.key === pe.tab)));
    const peUsed = computed(() => buckets.value.filter(b => pe.segs[b.key]
      && (pe.segs[b.key].words.length || pe.segs[b.key].lora.trim())));
    function peDrop(key, i) {
      const d = pe.drag;
      pe.drag = null;
      if (!d || d.key !== key || d.i === i) return;
      const words = pe.segs[key].words;
      const [w] = words.splice(d.i, 1);
      words.splice(i, 0, w);
    }
    const peReady = computed(() => pe.name.trim() && peUsed.value.length && !peNameTaken.value);
    async function saveGroupEditor() {
      const name = pe.name.trim();
      pe.busy = true;
      const ok = await savePresetGroupCall({ name: pe.orig, new_name: name,
        items: peUsed.value.map(b => ({ bucket: b.key, content: pe.segs[b.key].words.join(', '), lora: pe.segs[b.key].lora })) },
        pe.orig ? `已保存预设组「${name}」` : `已新建预设组「${name}」`);
      pe.busy = false;
      if (ok) {
        pe.open = false;
        if (pe.after) pe.after();
      }
    }
    async function deleteGroupEditor() {
      if (!confirm(`删除预设组「${pe.orig}」（${groupBuckets(pe.orig).length} 段）？可以撤销。`)) return;
      pe.busy = true;
      try {
        await api('DELETE', '/api/presets/group', { name: pe.orig });
        pe.open = false;
        if (gv.group === pe.orig) leaveGroup();
        await refresh();
        notify(`已删除预设组「${pe.orig}」`, 'ok', true);
      } catch (e) { notify(e.message, 'err'); }
      finally { pe.busy = false; }
    }

    // 导出：目录 = 输出根目录 / 批次名；有同名文件先列出来，确认后覆盖
    const ex = reactive({ open: false, name: '', base: '', txt: true, xlsx: true, busy: false, conflicts: null, result: null });
    const exDir = computed(() => (ex.base.replace(/[\\/]+$/, '') + '/' + ex.name.trim()));
    async function openExport() {
      try { cm.config = await api('GET', '/api/composer/config'); } catch (e) { notify(e.message, 'err'); }
      const d = new Date(), pad = n => String(n).padStart(2, '0');
      const label = (cm.source.label || '组合').replace(/^(预设组|主题|套图)「(.*)」$/, '$2').split(' / ').pop();
      Object.assign(ex, { open: true, busy: false, conflicts: null, result: null,
                          name: `${label}-${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}`.replace(/[\\/:*?"<>|]+/g, '_'),
                          base: lsGet('cm.dir') || cm.config.output_dir });
    }
    async function doExport(overwrite = false) {
      if (!ex.base.trim() || !ex.name.trim()) { notify('要填输出目录和批次名', 'err'); return; }
      lsSet('cm.dir', ex.base);
      ex.busy = true;
      try {
        ex.result = await api('POST', '/api/composer/export', {
          source: cm.source, slots: cm.slots, name: ex.name, dir: exDir.value, txt: ex.txt, xlsx: ex.xlsx, overwrite });
        ex.conflicts = null;
      } catch (e) {
        if (e.status === 409) ex.conflicts = e.data.conflicts;
        else notify(e.message, 'err');
      }
      ex.busy = false;
    }
    async function copyDir(d) {
      try { await copyText(d); notify('已复制路径'); } catch (e) { notify('复制失败：' + e.message, 'err'); }
    }

    // 快捷键：1-9 归到第 N 个段，B 拉黑，Ctrl+Z 撤销，Esc 清空选择
    // 图库：Esc 关闭词菜单 / 图片详情，← → 翻图
    function onKey(e) {
      const tag = (e.target.tagName || '').toLowerCase();
      const typing = tag === 'input' || tag === 'textarea' || tag === 'select';
      if (e.key === 'Escape' && wmenu.open) { wmenu.open = false; return; }
      if (e.key === 'Escape' && wp.open) { wp.open = false; return; }
      if (e.key === 'Escape' && pe.open && !pe.busy) { pe.open = false; return; }
      if (e.key === 'Escape' && ex.open && !ex.busy) { ex.open = false; return; }
      if (e.key === 'Escape' && dr.dlg && !dr.busy) { dr.dlg = false; return; }
      if (e.key === 'Escape' && !typing && gd.open) { closeImage(); return; }
      if (e.key === 'Escape' && !typing && st.open) { closeStats(); return; }
      if (e.key === 'Escape' && detail.open && !typing) { closeDetail(); return; }
      if (e.key === 'Escape' && !typing && pk.id && !imp.open && !tgDlg.open) { endPick(); return; }
      if (typing || aliasDlg.open || imp.open || tgDlg.open || ex.open || pe.open || dr.dlg || wp.open) return;
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') { e.preventDefault(); undo(); return; }
      if (tab.value === 'composer') return;
      if (tab.value === 'gallery') {
        if (gd.open && gd.item && !e.ctrlKey && !e.altKey) {
          const to = e.key === 'ArrowLeft' ? gd.item.nav.prev : e.key === 'ArrowRight' ? gd.item.nav.next : null;
          if (to) { e.preventDefault(); openImage(to); }
        }
        return;
      }
      if (bEdit.on) return;   // 管理段时不响应归位快捷键
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.key === 'Escape') { selected.clear(); showOps.value = false; showExport.value = false; return; }
      if (!selected.size) return;
      if (/^[1-9]$/.test(e.key)) {
        const b = buckets.value[Number(e.key) - 1];
        if (b) { e.preventDefault(); assignTo(b.key); }
      } else if (e.key.toLowerCase() === 'b') {
        e.preventDefault(); setBlocked(anyActive.value);   // 全是黑名单词时为移出
      }
    }
    onMounted(() => window.addEventListener('keydown', onKey));
    onBeforeUnmount(() => window.removeEventListener('keydown', onKey));

    function pickBucket(key) {
      view.bucket = key; view.filter = ''; selected.clear(); reload(1);
    }
    function pickFilter(f) {
      view.filter = f; selected.clear(); reload(1);
    }

    let searchTimer = null;
    function searchLater() {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => reload(1), 250);
    }

    // 多选：单击切换，Shift 连选
    let lastIdx = null;
    function toggle(id, ev) {
      const items = page.value.items;
      const idx = items.findIndex(t => t.id === id);
      if (ev && ev.shiftKey && lastIdx !== null) {
        const [a, b] = idx < lastIdx ? [idx, lastIdx] : [lastIdx, idx];
        for (let i = a; i <= b; i++) selected.set(items[i].id, items[i]);
      } else if (selected.has(id)) {
        selected.delete(id);
      } else {
        selected.set(id, items[idx]);
      }
      lastIdx = idx;
    }
    const allChecked = computed(() =>
      page.value.items.length > 0 && page.value.items.every(t => selected.has(t.id)));
    function toggleAll() {
      const on = !allChecked.value;
      page.value.items.forEach(t => (on ? selected.set(t.id, t) : selected.delete(t.id)));
    }

    const pageCount = computed(() => Math.max(1, Math.ceil(page.value.total / page.value.size)));
    const conflictTotal = computed(() =>
      (stats.value.file_conflicts || 0) + (stats.value.machine_conflicts || 0));
    const viewTitle = computed(() => {
      if (view.filter === 'all') return '全部词';
      if (view.filter === 'conflict') return '有分歧的词';
      if (view.filter === 'blocked') return '黑名单';
      const b = buckets.value.find(x => x.key === view.bucket);
      return b ? b.name : view.bucket;
    });

    function opinionText(t, only) {
      const name = k => (buckets.value.find(b => b.key === k) || {}).name || k;
      return (t.opinions || [])
        .filter(o => !only || o.source === only)
        .map(o => `${SOURCE[o.source] || o.source}: ${name(o.bucket_key)}${o.origin ? '  ← ' + o.origin.split(/[\\/]/).pop() : ''}`)
        .join('\n');
    }

    onMounted(refresh);

    return {
      tab, switchTab,
      gv, gpage, gtree, gloading, gsel, openDirs, toggleDir, openDir, dirRows,
      hasFilter, placeText,
      g, loadGallery, gFilter, pickState, setMode, toggleCompact, toggleShowScore, toggleSegShown,
      toggleShowBlocked, gSearchLater, gPageCount, gTitle, goBatch, thumbUrl, canTag, hasTags, toggleImg, checkable,
      gAllChecked, gToggleAll, copyPrompt, gd, openImage, closeImage, jumpToImage, openLinked, goBack, unlink,
      pk, startPick, endPick, confirmPick,
      gFullTitle, gg, curGroup, setGroup, setGroupMin, goGroup,
      CM_MODES, cm, cmSlot, cmFill, cmChanged, cmPreview, sendToComposer, cmSetOptions, onPickSource, resetSlots,
      dr, cmDraft, openNewDraft, createDraft, addDraftItems, deleteDraftItems, renameDraft, deleteDraft, applyToDraft,
      toggleDraftRow, draftPageAll, toggleDraftPage, cmAddPreset, cmRemovePreset, cmExcluded, cmToggleWord, cmSlotDirty, saveSlotToPresets, splitWords, applyGroup, cmDrag, groupDragStart, groupDragEnd, slotDrop, slotHot, presetById,
      cmPageCount, bucketName,
      pr, prOf, prGroupList, groupBuckets, toggleGroupStar, applyGroupByName,
      pe, peSources, openGroupEditor, peFresh, peSetTarget, peMergeInto, peGroupNames, peNameTaken, peSetSrc, peSetTab, peSearch, peCandAll, peCandShown, peHas, peToggle,
      peAddTyped, peSegList, peUsed, peDrop, peReady, saveGroupEditor, deleteGroupEditor,
      ex, exDir, openExport, doExport, copyDir,
      st, openStats, closeStats, stTags, stShown, stChecked, stKey, stToggle, stStartSave, stSave,
      groupFromStats, groupFromImage, groupFromGallery, groupFromDict, detailGroups,
      wmenu, wordAssign, wordBlock, wordRemove, wordInDict,
      wp, wpSetTab, wpSearch, wpShown, wpHad, wpOn, wpToggle, wpEnter, wpDone, cmPickWords,
      tgDlg, retagSelected, retagFilter, openRetag, doRetag,
      sc, scJob, scRunning, scPercent, loadScan, startScan, stopScan, shortList, moreCount,
      scanDirOfFilter, scanFilterDir,
      SOURCE, THEME_LABEL, theme, cycleTheme, stats, buckets, page, loading, selected, toast, view,
      reload, refresh, pickBucket, pickFilter, searchLater, toggle, toggleAll, allChecked,
      pageCount, conflictTotal, viewTitle, opinionText, notify,
      detail, openDetail, loadImages, detailDirty, saveDetail, removeAlias, closeDetail, detailOpinions,
      showExport, exportNow,
      tg, tgJob, tgRunning, tgPercent, loadTagger, startTagger, stopTagger, fmtSec,
      imp, openImport, pasteWords, doAdd, doImport, baseName,
      bEdit, startRename, saveRename, toggleJoins, addBucket, deleteBucket,
      dragStart, dragOver, dragEnd, dropOn,
      ops, showOps, aliasDlg, assignTo, setBlocked, anyBlocked, anyActive, openAlias, doAlias, undo,
    };
  },
})
  .component('img-segs', ImgSegs)
  .directive('focus', { mounted: el => { el.focus(); el.select && el.select(); } })
  .mount('#app');

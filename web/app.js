/* Yun2Face 控制台前端（原生 JS，无构建步骤） */
'use strict';

const S = {
  students: [],
  student: null,
  images: [],
  cfg: null,
  edit: null,        // {name, stem, ann, size, scale, imgW, imgH}
};

const $ = (id) => document.getElementById(id);

/* 后端时间戳是 Unix 秒，这里统一格式化为本地 YYYY-MM-DD HH:MM:SS。 */
function fmtTs(ts) {
  if (ts === null || ts === undefined || ts === '') return '—';
  const d = new Date(Number(ts) * 1000);
  if (isNaN(d.getTime())) return '—';
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} `
       + `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/* 按指定时区偏移（分钟，东八区=480）格式化。
   定时任务固定按后端声明的时区解释/显示，避免服务器是 UTC 时“下次”看起来差 8 小时。 */
function fmtTsIn(ts, offsetMin) {
  if (ts === null || ts === undefined || ts === '') return '—';
  const d = new Date(Number(ts) * 1000 + (Number(offsetMin) || 0) * 60000);
  if (isNaN(d.getTime())) return '—';
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} `
       + `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
}

function log(msg, cls) {
  const el = $('log');
  const t = new Date().toLocaleTimeString();
  el.textContent += `[${t}] ${msg}\n`;
  el.scrollTop = el.scrollHeight;
}

async function api(path, body) {
  const opt = body
    ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
    : {};
  const r = await fetch(path, opt);
  const j = await r.json().catch(() => ({ ok: false, error: 'HTTP ' + r.status }));
  if (!j.ok && j.error) throw new Error(j.error);
  return j;
}

/* ---------------------------------------------------------------- 初始化 */
async function boot() {
  try {
    S.cfg = await api('/api/config');
    $('paths').textContent = `照片：${S.cfg.photos}　|　标注：${S.cfg.annos}`;
    const d = S.cfg.detect_available;
    $('detect').textContent = d[0] ? '自动检测：可用' : '自动检测：不可用';
    $('detect').className = 'badge ' + (d[0] ? 'b-ok' : 'b-warn');
    if (!d[0]) log('自动检测不可用：' + d[1], 'warn');
    await loadStudents();
    log(`就绪：${S.students.length} 个学号目录，共 `
        + `${S.students.reduce((a, s) => a + s.photos, 0)} 张照片、`
        + `${S.students.reduce((a, s) => a + s.annos, 0)} 份标注。`
        + '左侧选学号看照片，顶部「跑步任务」看任务表。');

    // 深链接：?student=JW250246&open=L1.jpg 直接进到某个学号、可选自动打开编辑
    //          ?tab=task 直接切到跑步任务页
    const q = new URLSearchParams(location.search);
    if (q.get('tab') === 'task') {
      switchTab('task');
      const tf = q.get('open_task');
      if (tf) { await loadTasks(); await openTask(tf); }
    }
    const stu = q.get('student');
    if (stu && S.students.some(x => x.student === stu)) {
      switchTab('face');
      await selectStudent(stu);
      const open = q.get('open');
      if (open) await openEdit(open);
    }
  } catch (e) {
    log('初始化失败：' + e.message);
  }
}

async function loadStudents() {
  const j = await api('/api/students');
  S.students = j.students;
  const box = $('stus');
  box.innerHTML = '';
  if (!S.students.length) box.innerHTML = '<div class="hint" style="padding:6px">还没有任何学号目录</div>';
  for (const s of S.students) {
    const div = document.createElement('div');
    div.className = 'stu' + (s.student === S.student ? ' active' : '');
    div.innerHTML = `<span>${s.student}</span><small>${s.photos}/${s.annos}</small>`;
    div.onclick = () => selectStudent(s.student);
    box.appendChild(div);
  }
}

async function selectStudent(name) {
  S.student = name;
  $('title').textContent = name;
  await loadStudents();
  await loadImages();
  if (!$('pane-config').hidden) await loadControlConfig();
  if (!$('pane-stats').hidden) await loadStatistics();
  if (!$('pane-history').hidden) await loadHistoryTerms();
  // 定时运行页跟随左栏学号：切换学号即切换该学号的定时任务列表（正在修改的项一并取消）
  if (!$('pane-schedule').hidden) { SCH.editing = null; await loadSchedules(); }
}

/* ---------------------------------------------------------------- 列表 */
const ANN_BADGE = {
  missing: ['b-dim', '无标注'],
  ok: ['b-ok', '已绑定'],
  stale_path: ['b-warn', '路径过期'],
  mismatch: ['b-bad', '内容不符'],
  unbound: ['b-bad', '缺哈希'],
  broken: ['b-bad', 'JSON 损坏'],
};

async function loadImages() {
  if (!S.student) return;
  const j = await api('/api/images?student=' + encodeURIComponent(S.student));
  S.images = j.images;
  const tb = $('rows');
  tb.innerHTML = '';
  if (!S.images.length) {
    tb.innerHTML = '<tr><td colspan="6" class="hint" style="padding:18px">该目录下没有图片，可点右上角"添加照片"</td></tr>';
    return;
  }
  for (const im of S.images) {
    const tr = document.createElement('tr');
    const [cls, label] = ANN_BADGE[im.annotation.state] || ['b-dim', im.annotation.state];
    tr.innerHTML = `
      <td><img class="tn" src="/thumb/${encodeURIComponent(S.student)}/${encodeURIComponent(im.name)}"
               onclick="zoom(this.src)"></td>
      <td><div>${im.name}</div>
          <div class="mono" style="color:#9aa3b5">${im.sha256 ? im.sha256.slice(0, 16) + '…' : '（无法读取）'}</div>
          ${im.error ? `<div class="detail" style="color:#ff5c5c">${im.error}</div>` : ''}</td>
      <td>${im.size ? im.size[0] + '×' + im.size[1] : '—'}<br><span class="hint">${(im.bytes / 1024 / 1024).toFixed(2)} MB</span></td>
      <td><span class="badge ${cls}">${label}</span>
          ${im.annotation.detail ? `<div class="detail">${im.annotation.detail}</div>` : ''}</td>
      <td id="res-${im.stem}"><span class="hint">未检查</span></td>
      <td class="ops">
        <button onclick="check('${im.name}')">预检</button>
        <button onclick="openEdit('${im.name}')">标注</button>
        <button onclick="autodetect('${im.name}')">重检测</button>
        <button onclick="preview('${im.name}')">参考线</button>
        <button class="danger" onclick="delPhoto('${im.name}')">删除</button>
      </td>`;
    tb.appendChild(tr);
  }
  log(`已加载 ${S.student}：${S.images.length} 张`);
}

async function loadStatistics() {
  const message = $('statsMessage');
  const grid = $('statsGrid');
  if (!S.student) {
    $('statsStudent').textContent = '请选择左侧学号';
    message.textContent = '选择学号后点击“刷新统计”。';
    grid.innerHTML = '';
    return;
  }
  $('statsStudent').textContent = S.student;
  message.textContent = '正在读取当前学期记录…';
  grid.innerHTML = '';
  try {
    const d = await api('/api/statistics?student=' + encodeURIComponent(S.student));
    $('statsSemester').textContent = d.semester || '当前学期';
    message.textContent = `数据来源：${S.student} · 服务端历史记录`;
    grid.innerHTML = [
      ['本学期共跑', d.total],
      ['早晨跑步', d.morning],
      ['晚上跑步', d.evening],
      ['合格次数', d.qualified],
      ['不合格次数', d.unqualified],
    ].map(([label, value]) => `<div class="stat-card"><b>${label}</b><strong>${value}</strong></div>`).join('');
  } catch (e) {
    message.textContent = e.message;
    grid.innerHTML = '<div class="badge b-bad">统计读取失败</div>';
  }
}

const HISTORY = { terms: [], runs: [], record: null };
async function loadHistoryTerms() {
  if (!S.student) { $('historyStudent').textContent = '请选择左侧学号'; return; }
  $('historyStudent').textContent = S.student;
  $('historyMessage').textContent = '正在读取学期…';
  try {
    const j = await api('/api/history/terms?student=' + encodeURIComponent(S.student));
    HISTORY.terms = j.terms || [];
    $('historyTerm').innerHTML = HISTORY.terms.map((t, i) => `<option value="${i}">${t.key || t.sjd}</option>`).join('');
    $('historyMessage').textContent = `${HISTORY.terms.length} 个学期`;
    await loadHistoryRuns();
  } catch (e) { $('historyMessage').textContent = e.message; }
}
async function loadHistoryRuns() {
  const term = HISTORY.terms[Number($('historyTerm').value)];
  if (!term) return;
  try {
    const j = await api(`/api/history/runs?student=${encodeURIComponent(S.student)}&table=${encodeURIComponent(term.value || '')}`);
    HISTORY.runs = j.runs || [];
    $('historyRuns').innerHTML = HISTORY.runs.length ? `<table><thead><tr><th>时间</th><th>里程</th><th>状态</th><th>操作</th></tr></thead><tbody>` +
      HISTORY.runs.map((r, i) => `<tr><td>${r.recordEndTime || r.endTime || '—'}</td><td>${r.recordMileage ?? '—'} km</td><td>${r.isQualified === '1' ? '合格' : '不合格'}</td><td><button onclick="previewHistory(${i})">预览</button></td></tr>`).join('') + '</tbody></table>' : '<div class="hint">该学期没有历史记录。</div>';
  } catch (e) { $('historyRuns').innerHTML = `<span class="badge b-bad">${e.message}</span>`; }
}
async function previewHistory(index) {
  const term = HISTORY.terms[Number($('historyTerm').value)], row = HISTORY.runs[index];
  try {
    const j = await api(`/api/history/detail?student=${encodeURIComponent(S.student)}&table=${encodeURIComponent(term.value || '')}&id=${encodeURIComponent(row.id)}`);
    HISTORY.record = j;
    const data = j.data || {}, points = data.pointsList || [];
    $('historyPreviewTitle').textContent = `${data.recordEndTime || row.recordEndTime || ''} · ${data.recordMileage || row.recordMileage || '—'} km`;
    $('historyJson').textContent = JSON.stringify({ ...data, pointsList: points.slice(0, 8), _points: points.length }, null, 2);
    $('historySave').disabled = !points.length;
    try { await drawAmapHistory(points); }
    catch (mapError) {
      showHistoryCanvas(points);
      $('historyPreviewTitle').textContent += ` · 离线地图（${mapError.message}）`;
    }
  } catch (e) { $('historyJson').textContent = e.message; $('historySave').disabled = true; }
}
async function saveHistoryPreview() {
  if (!HISTORY.record) return;
  try { const r = await api('/api/history/save', { student: S.student, record: HISTORY.record }); log('已保存历史记录：' + r.filename); alert('保存成功：' + r.path); }
  catch (e) { alert('保存失败：' + e.message); }
}

function setResult(stem, html, cls) {
  const el = $('res-' + stem);
  if (el) el.innerHTML = `<span class="badge ${cls}">${html}</span>`;
}

/* ---------------------------------------------------------------- 单张动作 */
async function check(name, silent) {
  const stem = name.replace(/\.[^.]+$/, '');
  if (!silent) setResult(stem, '检查中…', 'b-dim');
  try {
    const r = await api('/api/check', { student: S.student, name });
    if (r.ok) {
      setResult(stem, `通过 ${r.upload_bytes}B`, 'b-ok');
      if (!silent) log(`✅ ${name} 通过（上传 ${r.upload_bytes} B / ${r.upload_size.join('×')}）`);
    } else {
      const reason = (r.errors && r.errors[0]) || '未知原因';
      setResult(stem, '不通过', 'b-bad');
      if (!silent) log(`❌ ${name} 不通过：${reason}`);
    }
    return r;
  } catch (e) {
    setResult(stem, '出错', 'b-bad');
    if (!silent) log(`⚠ ${name} 检查出错：${e.message}`);
    return { ok: false, errors: [e.message] };
  }
}

async function autodetect(name, silent) {
  const stem = name.replace(/\.[^.]+$/, '');
  try {
    const r = await api('/api/autodetect', { student: S.student, name, save: true });
    if (r.ok) {
      log(`🔍 ${name} 已重新检测并写入标注`);
      await loadImages();
      await check(name, true);
    } else {
      log(`⚠ ${name} 检测失败：${(r.errors || []).join('；')}`);
      setResult(stem, '未检出', 'b-warn');
    }
  } catch (e) { log(`⚠ ${name} 检测出错：${e.message}`); }
}

async function preview(name) {
  const w = window.open('', '_blank');
  w.document.write('<p style="font:14px sans-serif;color:#ccc;background:#111;padding:10px">生成中…</p>');
  try {
    const r = await fetch(`/preview/${encodeURIComponent(S.student)}/${encodeURIComponent(name)}`);
    const b = await r.blob();
    w.location = URL.createObjectURL(b);
  } catch (e) { w.close(); log('预览失败：' + e.message); }
}

async function delPhoto(name) {
  if (!confirm(`删除照片 ${name} 及其标注？此操作不可撤销。`)) return;
  const r = await api('/api/photo', { action: 'delete', student: S.student, name });
  log(`🗑 已删除 ${name}${r.annotation_removed ? '（含标注）' : ''}`);
  await loadImages();
}

async function batch(kind) {
  if (!S.student) return;
  const names = S.images.map(i => i.name);
  if (!names.length) return;
  const what = kind === 'autodetect' ? '重新检测并覆盖这 ' + names.length + ' 张的标注'
                                     : '预检这 ' + names.length + ' 张';
  if (!confirm(`确定${what}？`)) return;
  log(`▶ 批量${kind === 'autodetect' ? '生成标注' : '预检'}：${names.length} 张…`);
  const r = await api('/api/batch', {
    kind, student: S.student, images: names.map(n => ({ name: n })),
  });
  let ok = 0;
  for (const it of r.results) {
    const stem = it.name.replace(/\.[^.]+$/, '');
    if (it.ok) { ok++; setResult(stem, kind === 'check' ? `通过 ${it.upload_bytes}B` : '已生成', 'b-ok'); }
    else setResult(stem, kind === 'check' ? '不通过' : '未检出', 'b-warn');
  }
  log(`✔ 批量完成：${ok}/${r.results.length} 成功`);
  if (kind === 'autodetect') await loadImages();
}

async function uploadPicked(input) {
  if (!S.student) { alert('请先选择左侧学号目录'); return; }
  const files = [...input.files];
  input.value = '';
  for (const f of files) {
    const data = await new Promise(res => {
      const fr = new FileReader();
      fr.onload = () => res(fr.result);
      fr.readAsDataURL(f);
    });
    try {
      await api('/api/upload', { student: S.student, name: f.name, data, overwrite: false });
      log(`⬆ 已添加 ${f.name}`);
    } catch (e) {
      if (/已存在/.test(e.message) && confirm(`${f.name} 已存在，覆盖？`)) {
        await api('/api/upload', { student: S.student, name: f.name, data, overwrite: true });
        log(`⬆ 已覆盖 ${f.name}`);
      } else log(`⚠ ${f.name} 未添加：${e.message}`);
    }
  }
  await loadImages();
}

/* ---------------------------------------------------------------- 学号目录 */
async function newStudent() {
  const d = prompt('新学号 / 目录名（字母数字下划线中文）：');
  if (!d) return;
  try {
    await api('/api/students', { action: 'create', student: d });
    log('已创建目录 ' + d);
    await selectStudent(d);
  } catch (e) { alert(e.message); }
}

async function renameStudent() {
  if (!S.student) return;
  const d = prompt('新名称：', S.student);
  if (!d || d === S.student) return;
  try {
    await api('/api/students', { action: 'rename', student: S.student, new_student: d });
    log(`已改名 ${S.student} → ${d}`);
    await selectStudent(d);
  } catch (e) { alert(e.message); }
}

async function deleteStudent() {
  if (!S.student) return;
  if (!confirm(`删除整个目录 ${S.student}（照片与标注全部删除）？不可撤销。`)) return;
  await api('/api/students', { action: 'delete', student: S.student });
  log('🗑 已删除目录 ' + S.student);
  S.student = null;
  $('title').textContent = '请选择左侧学号';
  $('rows').innerHTML = '';
  await loadStudents();
}

function zoom(src) {
  const im = document.createElement('img');
  im.src = src; im.className = 'zoom';
  im.onclick = () => im.remove();
  document.body.appendChild(im);
}

/* ---------------------------------------------------------------- 标注编辑器 */
async function openEdit(name) {
  const stem = name.replace(/\.[^.]+$/, '');
  const j = await api(`/api/annotation?student=${encodeURIComponent(S.student)}&stem=${encodeURIComponent(stem)}`);
  let ann = j.annotation, size = j.photo_size;
  if (!j.exists) {
    if (!confirm('该照片还没有标注，现在用自动检测生成一份？')) return;
    const r = await api('/api/autodetect', { student: S.student, name, save: true });
    if (!r.ok) { alert('自动检测失败：' + (r.errors || []).join('；')); return; }
    ann = r.annotation;
    if (!size) { const im = new Image(); }
    await loadImages();
    const j2 = await api(`/api/annotation?student=${encodeURIComponent(S.student)}&stem=${encodeURIComponent(stem)}`);
    ann = j2.annotation; size = j2.photo_size;
  }
  if (!ann || !ann.box) { alert('标注格式不支持（需要 box + points 的单标注格式）'); return; }
  if (!size) { alert('无法读取照片尺寸'); return; }

  // 显示宽度：原图很大，缩到 1100 以内再画，坐标按 scale 反算
  const maxW = Math.min(1100, window.innerWidth * 0.88);
  const scale = Math.min(1, maxW / size[0]);
  S.edit = { name, stem, ann: JSON.parse(JSON.stringify(ann)), size, scale };

  const img = $('editImg');
  img.src = `/file/${encodeURIComponent(S.student)}/${encodeURIComponent(name)}?t=${Date.now()}`;
  await new Promise(r => { img.onload = r; img.onerror = r; });
  $('editTitle').textContent = `编辑标注 — ${name}`;
  $('editScale').textContent = `原图 ${size[0]}×${size[1]}　显示比例 ${(scale * 100).toFixed(1)}%`;
  $('editScore').value = ann.score != null ? ann.score : 1;
  drawOverlay();
  $('editGate').innerHTML = '<span class="hint">点"重新计算门限"查看当前框是否达标</span>';
  $('edit').showModal();
  reevaluate();
}

function closeEdit() { $('edit').close(); S.edit = null; }

function drawOverlay() {
  const e = S.edit; if (!e) return;
  const ov = $('ov');
  const img = $('editImg');
  const W = img.clientWidth, H = img.clientHeight;
  ov.style.width = W + 'px'; ov.style.height = H + 'px';
  // 用"显示尺寸 / 原图尺寸"换算，避免 CSS 缩放导致错位
  const kx = W / e.size[0], ky = H / e.size[1];
  const [x1, y1, x2, y2] = e.ann.box;
  const px = (v, k) => v * k;

  ov.innerHTML = '';
  for (const frac of [0.20, 0.80]) {
    const d = document.createElement('div');
    d.className = 'gline'; d.style.top = (H * frac) + 'px';
    ov.appendChild(d);
  }
  for (const frac of [0.10, 0.90]) {
    const d = document.createElement('div');
    d.className = 'gv'; d.style.left = (W * frac) + 'px';
    ov.appendChild(d);
  }
  const box = document.createElement('div');
  box.className = 'box';
  box.style.left = px(x1, kx) + 'px'; box.style.top = px(y1, ky) + 'px';
  box.style.width = px(x2 - x1, kx) + 'px'; box.style.height = px(y2 - y1, ky) + 'px';
  ov.appendChild(box);
  e.ann.points.forEach((p, i) => {
    const d = document.createElement('div');
    d.className = 'pt'; d.dataset.i = i;
    d.style.left = px(p[0], kx) + 'px'; d.style.top = px(p[1], ky) + 'px';
    ov.appendChild(d);
  });
  attachDrag(box, kx, ky, 'box');
  ov.querySelectorAll('.pt').forEach(el => attachDrag(el, kx, ky, Number(el.dataset.i)));
}

function attachDrag(el, kx, ky, what) {
  let sx = 0, sy = 0, orig = null;
  el.addEventListener('pointerdown', ev => {
    ev.preventDefault(); ev.stopPropagation();
    el.setPointerCapture(ev.pointerId);
    sx = ev.clientX; sy = ev.clientY;
    orig = what === 'box' ? [...S.edit.ann.box] : [...S.edit.ann.points[what]];
    const move = (e2) => {
      const dx = (e2.clientX - sx) / kx, dy = (e2.clientY - sy) / ky;
      if (what === 'box') {
        S.edit.ann.box = [orig[0] + dx, orig[1] + dy, orig[2] + dx, orig[3] + dy];
      } else {
        S.edit.ann.points[what] = [orig[0] + dx, orig[1] + dy];
      }
      drawOverlay();
    };
    const up = () => {
      el.removeEventListener('pointermove', move);
      el.removeEventListener('pointerup', up);
      reevaluate();
    };
    el.addEventListener('pointermove', move);
    el.addEventListener('pointerup', up);
  });
}

function currentAnn() {
  const e = S.edit;
  const score = parseFloat($('editScore').value);
  const ann = e.ann;
  ann.score = isNaN(score) ? ann.score : score;
  ann.space = ann.space || 'image';
  return ann;
}

/* 本地复算门限（与后端 core/check_framing 同一组阈值；仅用于即时反馈） */
function gateOf(ann, W, H) {
  const [x1, y1, x2, y2] = ann.box;
  const [p0, p1, p2, p3, p4] = ann.points;
  const w = x2 - x1;
  const jd = (a, b) => (b === 0 ? (a === 0 ? NaN : Math.sign(a) * Infinity) : a / b);
  const yaw = Math.abs(jd(p2[0] - (p0[0] + p1[0]) / 2, p1[0] - p0[0]));
  const eyeMid = (p0[1] + p1[1]) / 2;
  const pitch = jd(p2[1] - eyeMid, (p3[1] + p4[1]) / 2 - eyeMid);
  const roll = Math.abs(Math.atan2(p1[1] - p0[1], p1[0] - p0[0]) * 180 / Math.PI);
  const ratio = w / W;
  const rows = [
    ['框宽占比', ratio.toFixed(3), '0.400 ~ 0.650', ratio >= 0.40 && ratio <= 0.65],
    ['框 top', (y1 / H).toFixed(3), '≥ 0.200', y1 / H >= 0.20],
    ['框 bottom', (y2 / H).toFixed(3), '≤ 0.800', y2 / H <= 0.80],
    ['框 left', (x1 / W).toFixed(3), '≥ 0.100', x1 / W >= 0.10],
    ['框 right', (x2 / W).toFixed(3), '≤ 0.900', x2 / W <= 0.90],
    ['偏航 yaw', yaw.toFixed(3), '≤ 0.150', yaw <= 0.15],
    ['俯仰 pitch', pitch.toFixed(3), '0.300 ~ 0.800', pitch >= 0.30 && pitch <= 0.80],
    ['翻滚 roll', roll.toFixed(2) + '°', '≤ 15°', roll <= 15],
    ['检测分数', Number(ann.score).toFixed(3), '≥ 0.600', Number(ann.score) >= 0.60],
    ['完整在图内', '', 'x1≥0 且 x2≤W 且 y1≥0 且 y2≤H',
      x1 >= 0 && y1 >= 0 && x2 <= W && y2 <= H],
  ];
  return { rows, ok: rows.every(r => r[3]) };
}

function reevaluate() {
  const e = S.edit; if (!e) return;
  const ann = currentAnn();
  const g = gateOf(ann, e.size[0], e.size[1]);
  $('editKv').innerHTML = Object.entries({
    '框 (原图像素)': ann.box.map(v => Math.round(v)).join(', '),
    '五点顺序': '左眼 / 右眼 / 鼻尖 / 左嘴角 / 右嘴角',
  }).map(([k, v]) => `<b>${k}</b><span class="mono">${v}</span>`).join('');
  $('editGate').innerHTML =
    `<div style="margin-bottom:6px" class="badge ${g.ok ? 'b-ok' : 'b-bad'}">` +
    `${g.ok ? '当前框通过取景门' : '当前框不通过'}</div>` +
    '<table>' + g.rows.map(([n, v, req, ok]) =>
      `<tr><td>${n}</td><td class="mono">${v}</td><td class="hint">${req}</td>` +
      `<td>${ok ? '✔' : '✘'}</td></tr>`).join('') + '</table>';
}

async function resetFromDetect() {
  if (!S.edit || !confirm('重新自动检测会覆盖当前编辑内容，继续？')) return;
  const r = await api('/api/autodetect', { student: S.student, name: S.edit.name, save: true });
  if (!r.ok) { alert('检测失败：' + (r.errors || []).join('；')); return; }
  S.edit.ann = r.annotation;
  $('editScore').value = r.annotation.score;
  drawOverlay(); reevaluate();
  log('已用自动检测结果替换当前标注（未保存，点"保存并预检"生效）');
}

async function dropSourcePath() {
  if (!S.edit) return;
  const r = await api('/api/annotation',
    { action: 'drop_source_path', student: S.student, stem: S.edit.stem });
  if (r.removed) { delete S.edit.ann.source_path; log('已删除过期路径字段：' + r.removed); }
  else log('该标注没有 source_path 字段');
}

async function saveAnnotation() {
  const e = S.edit; if (!e) return;
  const ann = currentAnn();
  try {
    await api('/api/annotation',
      { action: 'save', student: S.student, name: e.name, stem: e.stem, annotation: ann });
    log('💾 已保存标注 ' + e.stem + '.json');
    await loadImages();
    await check(e.name, true);
    const r = await check(e.name, true);
    if (r.ok) log(`✅ ${e.name} 保存后预检通过`);
    else log(`❌ ${e.name} 保存后预检不通过：${(r.errors || [])[0]}`);
    closeEdit();
    await loadImages();
  } catch (err) { alert('保存失败：' + err.message); }
}

window.addEventListener('resize', () => { if (S.edit) drawOverlay(); });
boot();

/* ================================================================ 跑步任务 */
const T = { tasks: [], cur: null, split: 10 };

function switchTab(which) {
  for (const [k, id] of [['face', 'pane-face'], ['stats', 'pane-stats'], ['history', 'pane-history'], ['task', 'pane-task'], ['run', 'pane-run'], ['schedule', 'pane-schedule'],
                           ['route', 'pane-route'], ['config', 'pane-config']]) {
    $(id).hidden = (k !== which);
    $('tab-' + k).classList.toggle('active', k === which);
  }
  if (which === 'task') loadTasks();
  if (which === 'stats') loadStatistics();
  if (which === 'history') loadHistoryTerms();
  if (which === 'run') { loadRunOptions(); loadRuns(); }
  if (which === 'schedule') loadSchedules();
  if (which === 'route') { loadRouteMeta(); }
  if (which === 'config') { loadControlConfig(); }
}

/* ================================================================ 运行控制 */
const R = { runs: [], cfg: null, cur: null, since: 0, timer: null,
            configs: [], projects: [], photos: [],
            project: null, folder: '', taskFile: '' };

async function loadRunOptions() {
  try {
    const [cfg, pj] = await Promise.all([api('/api/configs'), api('/api/projects')]);
    R.cfg = cfg;
    R.configs = cfg.configs;
    R.projects = pj.projects;
    $('runPrj').textContent = `项目扫描：${pj.root}　共 ${R.projects.length} 个账号项目　python：${cfg.python}`;

    const ps = $('rProject');
    ps.innerHTML = R.projects.map((p, i) =>
      `<option value="${i}">${p.student || p.name}` +
      `${p.token ? '' : '（token 空）'}` +
      `${p.school ? ' · ' + p.school : ''}</option>`).join('');
    if (!R.projects.length) {
      $('rHint').innerHTML = '<span class="badge b-bad">没发现任何账号项目</span>' +
        ` 请确认 --projects-root（当前 ${pj.root}）下有 yunForNewVersion-&lt;学号&gt; 目录`;
      return;
    }
    // 人脸源（所有学号目录下的照片）
    const all = [];
    for (const s of S.students) {
      const j = await api('/api/images?student=' + encodeURIComponent(s.student));
      for (const im of j.images) {
        all.push({ student: s.student, name: im.name, stem: im.stem,
                   ann: im.annotation.state !== 'missing' });
      }
    }
    R.photos = all;
    $('rPhoto').innerHTML = '<option value="">（不提供，遇到人脸任务会停止）</option>' +
      all.map((p, i) => `<option value="${i}">${p.student}/${p.name}` +
        `${p.ann ? '' : ' · 无标注'}</option>`).join('');
    renderDetectOptions();
    await onProjectChange();
  } catch (e) { log('加载运行选项失败：' + e.message); }
}

/* 定时任务：列表 + 修改。
   学号固定取左侧学号栏的选中项：切换学号 → 表单/列表一起切换，列表只显示该学号的任务。
   SCH.editing 非空时，上方表单进入“修改”模式。 */
const SCH = { items: [], editing: null, tz: null, photos: [] };

/* 左栏选中学号对应的账号项目；schedules 的 options.project 由它生成。 */
function scheduleProject() {
  if (!S.student) return null;
  return R.projects.find(p => (p.student || '') === S.student) || null;
}

/* 表单顶部显示当前学号与账号项目状态；返回该学号的账号项目（可能为 null）。 */
function renderScheduleStudent() {
  const el = $('schStudent'), hint = $('schStudentHint'), list = $('schListStudent');
  if (list) list.textContent = S.student ? `　·　学号 ${S.student}` : '';
  const p = scheduleProject();
  if (!S.student) {
    el.textContent = '请选择左侧学号';
    el.style.color = '#ffb020';
    hint.textContent = '先在左侧学号栏选中一个学号；这里只显示该学号的定时任务。';
    hint.style.color = '#ffb020';
  } else if (!p) {
    el.textContent = S.student;
    el.style.color = '#ff8a8a';
    hint.textContent = `学号 ${S.student} 没有账号项目（缺 config.ini），无法创建定时任务。`;
    hint.style.color = '#ff8a8a';
  } else {
    el.textContent = S.student;
    el.style.color = '';
    hint.textContent = `账号项目：${p.path}`;
    hint.style.color = '';
  }
  $('schSave').disabled = !p;
  return p;
}

/* 显示后端使用的定时时区；若服务器本地时区不同（例如 UTC），明确提示。 */
function renderScheduleTz() {
  const el = $('schTZ');
  if (!el) return;
  const tz = SCH.tz || {};
  const label = tz.label || 'UTC+08:00';
  if (tz.matches_server === false) {
    el.innerHTML = `定时按 <b>${label}</b> 解释；服务器本地时区是 <b>${tz.server_label || '?'}</b>`
      + '（已按定时时区计算，不用改服务器时间）';
    el.style.color = '#ffb020';
  } else {
    el.textContent = `定时按 ${label} 解释`;
    el.style.color = '';
  }
}

async function loadSchedules() {
  try {
    if (!R.projects.length) await loadRunOptions();
    renderScheduleStudent();
    await fillScheduleFolders();
    await loadSchedulePhotos();
    renderScheduleDetectOptions();
    const j = await api('/api/schedules');
    SCH.tz = j.time_zone || null;
    const all = j.schedules || [];
    SCH.items = S.student ? all.filter(s => (s.options || {}).student === S.student) : [];
    renderScheduleTz();
    renderScheduleList();
    renderScheduleOrphans(all);
    if (SCH.editing && !SCH.items.some(s => s.id === SCH.editing)) SCH.editing = null;
    if (SCH.editing) {
      const item = SCH.items.find(s => s.id === SCH.editing);
      if (item) await applyScheduleForm(item);
      else { SCH.editing = null; updateScheduleFormMode(); }
    } else {
      updateScheduleFormMode();
    }
  } catch (e) { log('读取定时任务失败：' + e.message); }
}

/* 人脸素材只取当前学号的（每次进入/切换都重读，避免沿用「运行控制」页的旧缓存）。 */
async function loadSchedulePhotos() {
  SCH.photos = [];
  if (S.student) {
    try {
      const j = await api('/api/images?student=' + encodeURIComponent(S.student));
      SCH.photos = (j.images || []).map(im => ({
        student: S.student, name: im.name, stem: im.stem,
        ann: (im.annotation || {}).state !== 'missing',
      }));
    } catch (e) { log('读取该学号人脸素材失败：' + e.message); }
  }
  $('schPhoto').innerHTML = '<option value="">（不使用人脸）</option>' +
    SCH.photos.map((x, i) => `<option value="${i}">${x.name}${x.ann ? '' : ' · 无 JSON'}</option>`).join('');
}

/* 其它学号的定时任务不显示，但给出数量，避免改名/换机器后“看不见”被误以为数据丢了。 */
function renderScheduleOrphans(all) {
  const el = $('schOrphans');
  if (!el) return;
  if (!S.student) { el.textContent = ''; return; }
  const others = (all || []).filter(s => (s.options || {}).student !== S.student);
  if (!others.length) { el.textContent = ''; return; }
  const names = [...new Set(others.map(s => (s.options || {}).student || s.name))];
  el.innerHTML = `另有 ${others.length} 条定时任务属于其他学号（${names.join('、')}），`
    + '在左侧选中对应学号后可查看、修改。';
}

function renderScheduleList() {
  const box = $('scheduleList');
  if (!S.student) {
    box.innerHTML = '<div class="hint">先在左侧学号栏选中一个学号。</div>';
    return;
  }
  if (!SCH.items.length) {
    box.innerHTML = `<div class="hint">学号 ${S.student} 还没有定时任务。</div>`;
    return;
  }
  const off = (SCH.tz && SCH.tz.offset_minutes) || 0;
  box.innerHTML = SCH.items.map(s => {
    const o = s.options || {};
    const stu = o.student || S.student;
    const next = s.next_run ? fmtTsIn(s.next_run, off) : '—';
    const last = s.last_run ? `${s.last_run}（${fmtTsIn(s.last_started, off)}）` : '—';
    // 服务停机/任务停用期间错过的运行会被跳过并顺延，这里明确显示，避免误以为没跑。
    const skipped = s.skipped_count ? ` · <span style="color:#ffb020">已跳过 ${s.skipped_count} 次过期运行${s.last_skipped ? '（最近 ' + fmtTsIn(s.last_skipped, off) + '）' : ''}</span>` : '';
    const label = s.name && s.name !== stu ? ` <span class="hint">${s.name}</span>` : '';
    return `<div class="runitem ${s.id === SCH.editing ? 'active' : ''}">
      <span class="st ${s.enabled ? 'st-running' : 'st-stopped'}">${s.enabled ? '启用' : '停用'}</span>
      <div style="flex:1"><div><b>${stu}</b>${label}</div>
        <div class="meta">每天 ${s.time_of_day || '07:30'} · 下次 ${next} · 上次 ${last}${skipped}</div>
        <div class="meta">${o.mode === 'dry' ? '离线演练' : '真实运行'} · ${o.task_folder || '?'}/${o.task_file || '随机'}${o.face_photo ? ' · 人脸 ' + o.face_photo : ''}</div>
        ${s.last_error ? `<div class="detail" style="color:#ff8a8a">${s.last_error}</div>` : ''}</div>
      <button onclick="editSchedule('${s.id}')">修改</button>
      <button onclick="toggleSchedule('${s.id}',${!s.enabled})">${s.enabled ? '停用' : '启用'}</button>
      <button onclick="runScheduleNow('${s.id}')">立即运行</button>
      <button class="danger" onclick="deleteSchedule('${s.id}')">删除</button></div>`;
  }).join('');
}

function updateScheduleFormMode() {
  const item = SCH.editing ? SCH.items.find(s => s.id === SCH.editing) : null;
  $('schFormTitle').textContent = item ? '修改定时任务' : '新建定时任务';
  $('schSave').textContent = item ? '保存修改' : '保存定时任务';
  $('schCancel').hidden = !item;
  $('schEditHint').textContent = item ? `正在修改 ${item.name}（${item.id}）` : '';
}

/* 把某条已保存任务的配置回填到表单（目录/表/人脸逐级异步加载后再选中）。 */
async function applyScheduleForm(item) {
  const o = item.options || {};
  $('schName').value = (item.name && item.name !== (o.student || '')) ? item.name : '';
  $('schTime').value = item.time_of_day || '07:30';
  $('schMode').value = o.mode === 'real' ? 'real' : 'dry';
  await fillScheduleFolders();
  if (o.task_folder && [...$('schFolder').options].some(x => x.value === o.task_folder)) {
    $('schFolder').value = o.task_folder;
    await fillScheduleTasks();
  }
  if (o.task_file && [...$('schTask').options].some(x => x.value === o.task_file)) {
    $('schTask').value = o.task_file;
  }
  const ph = SCH.photos.findIndex(x => x.name === o.face_photo);
  $('schPhoto').value = ph >= 0 ? String(ph) : '';
  if (o.face_photo && ph < 0) {
    log(`定时任务的人脸照片不在当前学号素材里：${o.face_photo}（保存会清空该任务的人脸设置）`, 'warn');
  }
  renderScheduleDetectOptions();
  if (ph >= 0 && o.face_stem) {
    const want = (o.face_student || S.student) + '/' + o.face_stem + '.json';
    if ([...$('schDetect').options].some(x => x.value === want)) $('schDetect').value = want;
  }
  updateScheduleFormMode();
}

async function editSchedule(id) {
  SCH.editing = id;
  await loadSchedules();
  const item = SCH.items.find(s => s.id === id);
  if (!item) return;
  log(`正在修改定时任务 ${item.name}（${item.id}）`);
  $('pane-schedule').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function cancelScheduleEdit() {
  SCH.editing = null;
  updateScheduleFormMode();
  log('已取消修改定时任务');
  loadSchedules();
}

async function fillScheduleFolders() {
  const p = scheduleProject();
  const folders = p ? (p.task_dirs || []) : [];
  $('schFolder').innerHTML = folders.map(f => `<option value="${f.name}">${f.name}（${f.files} 个表）</option>`).join('');
  await fillScheduleTasks();
}
async function fillScheduleTasks() {
  const p = scheduleProject(), folder = $('schFolder').value;
  if (!p || !folder) { $('schTask').innerHTML = ''; return; }
  const j = await api(`/api/tasks?project=${encodeURIComponent(p.path)}&folder=${encodeURIComponent(folder)}`);
  $('schTask').innerHTML = j.tasks.filter(t => !t.error).map(t => `<option value="${t.file}">${t.file} · ${t.mileage}km</option>`).join('');
}
function renderScheduleDetectOptions() {
  const i = $('schPhoto').value;
  if (i === '') { $('schDetect').innerHTML = '<option value="">—</option>'; return; }
  const p = SCH.photos[Number(i)];
  if (!p) { $('schDetect').innerHTML = '<option value="">—</option>'; return; }
  $('schDetect').innerHTML = `<option value="${p.student}/${p.stem}.json">${p.stem}.json${p.ann ? '' : '（不存在）'}</option>`;
}
async function saveSchedule() {
  if (!S.student) { alert('请先在左侧学号栏选择学号'); return; }
  const p = scheduleProject();
  if (!p) { alert(`学号 ${S.student} 没有账号项目（缺 config.ini），无法创建定时任务`); return; }
  if (!$('schFolder').value || !$('schTask').value) {
    alert('该学号没有可用任务表：请先在「跑步任务」页导入/选择任务表');
    return;
  }
  const photo = $('schPhoto').value === '' ? null : SCH.photos[Number($('schPhoto').value)];
  const options = { mode: $('schMode').value, project: p.path, student: p.student || S.student,
    config: p.config, task_folder: $('schFolder').value, task_file: $('schTask').value,
    face_student: photo ? S.student : null, face_photo: photo ? photo.name : null,
    face_stem: photo ? photo.stem : null };
  const name = ($('schName').value || '').trim() || (p.student || S.student);
  const time_of_day = $('schTime').value || '07:30';
  try {
    if (SCH.editing) {
      await api('/api/schedules', { action: 'update', id: SCH.editing,
        name, time_of_day, options });
      log('已保存定时任务修改：' + name);
      SCH.editing = null;
    } else {
      await api('/api/schedules', { action: 'create', name, time_of_day, options });
      log('已保存定时任务');
      $('schName').value = '';   // 新建成功后清空名称，避免下一条沿用
    }
    updateScheduleFormMode();
    await loadSchedules();
  } catch (e) { alert('保存定时任务失败：' + e.message); }
}

/* 旧入口保留，等价于 saveSchedule（新建/修改共用同一套表单） */
async function createSchedule() { return saveSchedule(); }

async function toggleSchedule(id, enabled) {
  try { await api('/api/schedules', { action: 'toggle', id, enabled }); } catch (e) { alert(e.message); }
  await loadSchedules();
}

async function deleteSchedule(id) {
  if (!confirm('删除这个定时任务？')) return;
  if (SCH.editing === id) SCH.editing = null;
  await api('/api/schedules', { action: 'delete', id });
  await loadSchedules();
}

async function runScheduleNow(id) {
  const r = await api('/api/schedules', { action: 'run_now', id });
  R.cur = r.id; R.since = 0;
  $('rLog').textContent = '';
  switchTab('run');
  await loadRuns();
  setOutputTitle(r.id);
  startPolling();
}

/* 选项目 → 带出它的配置与自己的 tasks_* 目录 */
async function onProjectChange() {
  const i = $('rProject').value;
  R.project = R.projects[Number(i)];
  if (!R.project) return;
  const fs = $('rFolder');
  const dirs = R.project.task_dirs || [];
  fs.innerHTML = dirs.map(d =>
    `<option value="${d.name}">${d.name}（${d.files} 个表）</option>`).join('') ||
    '<option value="">（该项目下没有 tasks_* 目录）</option>';
  // 默认优先 tasks_else（各学号自己放表的地方）
  const prefer = dirs.find(d => d.name === 'tasks_else') || dirs[0];
  if (prefer) fs.value = prefer.name;
  await onFolderChange();
}

/* 选目录 → 列出该目录里的表 */
async function onFolderChange() {
  R.folder = $('rFolder').value;
  const ts = $('rTask');
  if (!R.project || !R.folder) {
    ts.innerHTML = '<option value="">（无）</option>';
    R.taskFile = '';
    renderConfigHint();
    previewCmd();
    return;
  }
  try {
    const j = await api(`/api/tasks?project=${encodeURIComponent(R.project.path)}` +
                        `&folder=${encodeURIComponent(R.folder)}`);
    ts.innerHTML = j.tasks.filter(t => !t.error).map(t =>
      `<option value="${t.file}">${t.file} · ${t.mileage}km · ${t.duration}s · 配速${t.pace}</option>`
    ).join('') || '<option value="">（该目录下没有表）</option>';
    R.taskFile = ts.value || '';
    if (j.tasks.length > 1) {
      R.multiTask = true;
    } else {
      R.multiTask = false;
    }
  } catch (e) { log('读取任务表失败：' + e.message); }
  renderConfigHint();
  previewCmd();
}

/* 项目自带配置就是它 config.ini：不再有"另选配置"，避免串号 */
function projectConfigPath() {
  return R.project ? (R.project.config || (R.project.path + '\\config.ini')) : '';
}

function renderConfigHint() {
  const p = R.project;
  if (!p) return;
  const td = (p.task_dirs || []).map(d => `${d.name}(${d.files})`).join('、') || '无';
  $('rHint').innerHTML =
    (p.token ? '<span class="badge b-ok">token 已就绪</span>'
             : '<span class="badge b-warn">token 为空</span> 真实运行前需点『登录』；离线演练不需要') +
    `<div class="hint" style="margin-top:6px">
       项目：${p.path}<br>
       配置：${p.config || '（缺 config.ini）'}<br>
       账号：${p.student || '—'}　学校：${p.school || '—'}　设备：${p.device || '—'}<br>
       任务表目录：${td}${R.multiTask ? '<br>⚠ 该目录有多个表，项目将<b>随机</b>挑一个' : ''}
     </div>`;
}

function renderDetectOptions() {
  const i = $('rPhoto').value;
  const sel = $('rDetect');
  if (i === '') { sel.innerHTML = '<option value="">—</option>'; return; }
  const p = R.photos[Number(i)];
  sel.innerHTML = `<option value="${p.student}/${p.stem}.json">${p.stem}.json` +
    `${p.ann ? '' : '（不存在，需先生成）'}</option>`;
}

function onModeChange() {
  const real = $('rMode').value === 'real';
  $('rHint').style.opacity = 1;
  renderConfigHint();
  previewCmd();
  if (real) log('已切到真实运行模式：会创建服务端记录，且使手机 App 会话失效', 'warn');
}

function runOptions() {
  const i = $('rPhoto').value;
  const p = i === '' ? null : R.photos[Number(i)];
  return {
    mode: $('rMode').value,
    project: R.project ? R.project.path : '',
    student: R.project ? R.project.student : '',
    config: projectConfigPath(),          // 用项目自己的 config.ini，避免串号
    task_folder: R.folder || null,        // -t 指向该学号自己的 tasks_* 目录
    task_file: $('rTask').value || null,
    drift: $('rDrift').checked,
    face_student: p ? p.student : null,
    face_photo: p ? p.name : null,
    face_stem: p ? p.stem : null,
    save_token: $('rSaveToken').checked,
  };
}

/* 前端只做"预览"用途的命令行拼接；真正执行由后端 _main_argv 生成，避免两处逻辑分叉 */
function previewCmd() {
  const o = runOptions();
  if (!R.project) { $('rCmd').textContent = ''; return; }
  const p = $('rPhoto').value === '' ? null : R.photos[Number($('rPhoto').value)];
  const parts = ['python main.py', '-f config.ini'];
  if (o.mode === 'dry') parts.push('--dry-run --dry-home <fixture>');
  else parts.push('-a');
  if (o.task_folder) parts.push(`-t ${o.task_folder}/   # ${o.task_file || '该目录'}`);
  if (o.drift) parts.push('-d');
  if (p) parts.push(`--face-photo <照片根>/${p.student}/${p.name}`);
  if (p) parts.push(`--face-detection <标注根>/${p.student}/${p.stem}.json`);
  if (o.mode === 'real') parts.push('--login-retries 2 --login-retry-delay 3 --reauth-save-token');
  $('rCmd').textContent = `cd ${R.project.name}  &&  ` + parts.join(' ');
}

async function startRun() {
  const o = runOptions();
  if (!R.project) { alert('先选账号项目'); return; }
  if (!R.folder) { alert('该项目下没有 tasks_* 任务表目录'); return; }
  if (o.mode === 'real' && !confirm(
      `真实运行（账号 ${R.project.student || R.project.name}）会：\n` +
      '· 在服务端创建跑步记录\n· 上传轨迹与（可能）人脸比对\n' +
      '· 使该账号手机 App 的登录会话失效\n\n确定启动？')) return;
  try {
    const r = await api('/api/runs', { action: 'start', ...o });
    log('▶ 已启动 ' + r.id + (r.note ? '（' + r.note + '）' : ''));
    R.cur = r.id; R.since = 0;
    $('rLog').textContent = '';
    $('rOutTitle').textContent = '（' + r.id + '）';
    await loadRuns();
    setOutputTitle(r.id);
    startPolling();
  } catch (e) { alert('启动失败：' + e.message); }
}

async function loadRuns() {
  const j = await api('/api/runs');
  R.runs = j.runs;
  const box = $('rList');
  if (!R.runs.length) {
    box.innerHTML = '<div class="hint">还没有运行过。左边选好配置与任务表，点『启动』。</div>';
    return;
  }
  box.innerHTML = R.runs.map(r => `
    <div class="runitem ${r.id === R.cur ? 'active' : ''}" onclick="viewRun('${r.id}')">
      <span class="st st-${r.status}">${statusLabel(r)}</span>
      <div style="flex:1">
        <div>${r.id}　<span class="meta">${r.mode === 'dry' ? '离线演练' : '真实运行'}</span></div>
        <div class="meta">${r.project || ''} · ${r.task_folder || '?'}/${r.task_file || '随机'}
          · ${r.elapsed}s${r.returncode !== null ? ' · 退出码 ' + r.returncode : ''}</div>
        <div class="meta">开始 ${fmtTs(r.started)}${r.ended ? ' · 结束 ' + fmtTs(r.ended) : ''}</div>
      </div>
      ${r.status === 'running'
        ? `<button class="danger" onclick="event.stopPropagation();stopRun('${r.id}')">停止</button>`
        : ''}
    </div>`).join('');
  if (R.cur) {
    const cur = R.runs.find(r => r.id === R.cur);
    if (cur && cur.status !== 'running') stopPolling();
  }
}

function statusLabel(r) {
  const base = { running: '运行中', done: '已完成', failed: '失败', stopped: '已停止' }[r.status] || r.status;
  return r.verdict ? base + '·' + r.verdict : base;
}

/* 输出面板标题带上该实例的运行开始时间 */
function setOutputTitle(id) {
  const run = R.runs.find(r => r.id === id);
  $('rOutTitle').textContent = '（' + id
    + (run && run.started ? ' · 开始 ' + fmtTs(run.started) : '') + '）';
}

function viewRun(id) {
  R.cur = id; R.since = 0;
  $('rLog').textContent = '';
  setOutputTitle(id);
  loadRuns();
  pollRun(true);
  startPolling();
}

async function pollRun(reset) {
  if (!R.cur) return;
  try {
    const j = await api(`/api/run?id=${R.cur}&since=${R.since}`);
    if (j.lines && j.lines.length) {
      const el = $('rLog');
      const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
      for (const line of j.lines) {
        const d = document.createElement('div');
        if (/✘|错误|失败|Traceback|Error|业务拒绝|停止/.test(line)) d.className = 'err';
        else if (/✅|通过|成功|完成/.test(line)) d.className = 'ok';
        else if (/⚠|warning|提示/.test(line)) d.className = 'warn';
        d.textContent = line;
        el.appendChild(d);
      }
      R.since = j.total_lines;
      if (atBottom) el.scrollTop = el.scrollHeight;
    }
    if (j.status !== 'running') {
      stopPolling();
      await loadRuns();
      log(`运行 ${R.cur} 结束：${statusLabel(j)}` + (j.returncode !== null ? `（退出码 ${j.returncode}）` : ''));
    }
  } catch (e) { stopPolling(); log('读取运行输出失败：' + e.message); }
}

function startPolling() { stopPolling(); R.timer = setInterval(() => pollRun(), 1200); }
function stopPolling() { if (R.timer) { clearInterval(R.timer); R.timer = null; } }

async function stopRun(id) {
  if (!confirm(`停止 ${id}？\n\n注意：半途停止可能留下未完成的服务端记录（需在 App 里删除）。`)) return;
  await api('/api/runs', { action: 'stop', id });
  log('⏹ 已请求停止 ' + id, 'warn');
  await loadRuns();
}

async function clearRuns() {
  await api('/api/runs', { action: 'clear' });
  log('已清理结束的实例记录');
  await loadRuns();
}

async function doLogin() {
  const c = projectConfigPath();
  if (!c) { $('rHint').innerHTML = '<span class="badge b-warn">请先选择账号项目</span>'; return; }
  $('rHint').innerHTML = '<span class="badge b-dim">准备登录…</span>';
  if (!confirm('登录会让手机 App 的会话失效（同账号多客户端的固有代价）。继续？')) return;
  $('rHint').innerHTML = '<span class="badge b-dim">登录中…</span>';
  try {
    const r = await api('/api/runs', { action: 'login', config: c,
      save_token: $('rSaveToken').checked });
    $('rHint').innerHTML = r.ok
      ? `<span class="badge b-ok">登录成功，token 已就绪${r.saved ? '，已写回配置' : ''}</span>`
      : `<span class="badge b-bad">登录失败</span> ${r.error || ''}`;
    log(r.ok ? '✅ 登录成功' : '❌ 登录失败：' + (r.error || ''));
    await loadRunOptions();
  } catch (e) {
    $('rHint').innerHTML = `<span class="badge b-bad">登录请求失败</span> ${e.message}`;
  }
}

async function loadTasks() {
  try {
    const j = await api('/api/tasks');
    T.tasks = j.tasks;
    T.split = j.split_count;
    $('taskDir').textContent = `目录：${j.dir}　（每批 ${j.split_count} 点）`;
    const tb = $('trows');
    tb.innerHTML = '';
    if (!T.tasks.length) {
      tb.innerHTML = `<tr><td colspan="7" class="hint" style="padding:18px">
        该目录下没有任务表。可点右上角"导入任务表"，或把 --tasks 指向项目的 tasks_fch</td></tr>`;
      return;
    }
    for (const t of T.tasks) {
      const tr = document.createElement('tr');
      if (t.error) {
        tr.innerHTML = `<td>${t.file}</td><td colspan="5" class="detail" style="color:#ff5c5c">${t.error}</td>
          <td><button class="danger" onclick="deleteTask('${t.file}')">删除</button></td>`;
        tb.appendChild(tr);
        continue;
      }
      tr.innerHTML = `
        <td><div>${t.file}</div>
            <div class="mono" style="color:#9aa3b5">${t.raType || ''} · raId=${t.raId ?? '—'}
              · ${t.studentId || '—'}</div></td>
        <td>${t.mileage ?? '—'} km<br><span class="hint">${t.duration ?? '—'} s
              ${t.avg_speed_kmh ? '· ' + t.avg_speed_kmh + ' km/h' : ''}</span></td>
        <td>${t.pace ?? '—'}<br><span class="hint">${t.cadence ?? '—'} 步/分</span></td>
        <td>${t.points}<br><span class="hint">${(t.bytes / 1024).toFixed(0)} KB</span></td>
        <td>${t.fence}</td>
        <td id="tv-${t.file}"><span class="hint">未校验</span></td>
        <td>
          <button onclick="openTask('${t.file}')">查看 / 派生</button>
          <button onclick="validateTask('${t.file}')">校验</button>
          <button class="danger" onclick="deleteTask('${t.file}')">删除</button>
        </td>`;
      tb.appendChild(tr);
    }
    log(`已加载任务表：${T.tasks.length} 个`);
  } catch (e) { log('加载任务表失败：' + e.message); }
}

function setTaskVal(file, html, cls) {
  const el = $('tv-' + file);
  if (el) el.innerHTML = `<span class="badge ${cls}">${html}</span>`;
}

async function validateTask(file) {
  const name = file || (T.cur && T.cur.summary.file);
  if (!name) return;
  setTaskVal(name, '校验中…', 'b-dim');
  try {
    const r = await api('/api/task', { action: 'validate', name });
    const cls = r.ok ? (r.warnings.length ? 'b-warn' : 'b-ok') : 'b-bad';
    const label = r.ok ? (r.warnings.length ? `通过(${r.warnings.length}提示)` : '通过') : '不通过';
    setTaskVal(name, label, cls);
    if (T.cur && T.cur.summary.file === name) renderValidation(r);
    return r;
  } catch (e) { setTaskVal(name, '出错', 'b-bad'); log('校验失败：' + e.message); }
}

async function batchValidateTasks() {
  const ok = T.tasks.filter(t => !t.error).map(t => t.file);
  if (!ok.length) return;
  log(`▶ 批量校验 ${ok.length} 个任务表…`);
  let pass = 0, warn = 0, fail = 0;
  for (const f of ok) {
    const r = await validateTask(f);
    if (!r) continue;
    if (!r.ok) fail++; else if (r.warnings.length) warn++; else pass++;
  }
  log(`✔ 校验完成：通过 ${pass} / 有提示 ${warn} / 不通过 ${fail}`);
}

async function openTask(file) {
  const r = await api('/api/task?name=' + encodeURIComponent(file));
  T.cur = r;
  $('taskTitle').textContent = file;
  const s = r.summary;
  $('taskMeta').textContent =
    `${s.points} 点 · ${s.mileage} km · ${s.duration}s · 配速 ${s.pace} · 步频 ${s.cadence} · 围栏 ${s.fence}`;
  $('genName').value = file.replace(/\.json$/i, '') + '_new.json';
  $('genPace').value = '';
  $('genDrift').value = '';
  $('genSeed').value = '';
  $('genOut').innerHTML = '';
  drawTaskChart(r.curves);
  renderValidation(r.validation);
  $('taskDlg').showModal();
}

function renderValidation(v) {
  if (!v) { $('taskVal').innerHTML = ''; return; }
  const badge = v.ok ? (v.warnings.length ? 'b-warn' : 'b-ok') : 'b-bad';
  const label = v.ok ? (v.warnings.length ? `通过（${v.warnings.length} 条提示）` : '通过') : '不通过';
  let html = `<div class="badge ${badge}">${label}</div>`;
  if (v.errors.length) {
    html += '<ul style="margin:8px 0 0;padding-left:18px;color:#ff5c5c">'
      + v.errors.map(e => `<li>${e}</li>`).join('') + '</ul>';
  }
  if (v.warnings.length) {
    html += '<ul style="margin:8px 0 0;padding-left:18px;color:#ffb020">'
      + v.warnings.map(e => `<li>${e}</li>`).join('') + '</ul>';
  }
  const st = v.stats || {};
  const rows = Object.entries(st).filter(([, val]) =>
    val !== null && !Array.isArray(val)).map(([k, val]) => `<b>${k}</b><span class="mono">${val}</span>`);
  if (rows.length) html += `<div class="kv" style="margin-top:10px">${rows.join('')}</div>`;
  if (st.runMileage_range) {
    html += `<div class="hint">里程 ${st.runMileage_range[0]} → ${st.runMileage_range[1]} m；
      时间 ${st.runTime_range[0]} → ${st.runTime_range[1]} s；
      步数 ${st.runStep_range[0]} → ${st.runStep_range[1]}</div>`;
  }
  $('taskVal').innerHTML = html;
}

function drawTaskChart(c) {
  const cv = $('taskChart');
  const g = cv.getContext('2d');
  const W = cv.width, H = cv.height, PAD = 34;
  g.clearRect(0, 0, W, H);
  g.fillStyle = '#0b0d13'; g.fillRect(0, 0, W, H);
  const n = c.t.length;
  if (!n) return;
  const series = [
    { data: c.speed, color: '#4c8dff', label: '速度' },
    { data: c.mileage, color: '#39d98a', label: '里程' },
    { data: c.step, color: '#ffb020', label: '步数' },
  ];
  for (const s of series) {
    const vals = s.data.filter(v => typeof v === 'number');
    if (!vals.length) continue;
    const lo = Math.min(...vals), hi = Math.max(...vals), span = (hi - lo) || 1;
    g.beginPath();
    s.data.forEach((v, i) => {
      if (typeof v !== 'number') return;
      const x = PAD + (W - PAD - 8) * (n === 1 ? 0 : i / (n - 1));
      const y = H - PAD - (H - PAD - 12) * ((v - lo) / span);
      i ? g.lineTo(x, y) : g.moveTo(x, y);
    });
    g.strokeStyle = s.color; g.lineWidth = 1.6; g.stroke();
  }
  g.fillStyle = '#9aa3b5'; g.font = '11px Consolas';
  g.fillText(`0`, PAD - 8, H - PAD + 14);
  g.fillText(`${c.points_used}/${c.points_total} 点（降采样）`, PAD, H - 8);
}

async function generateTask() {
  if (!T.cur) return;
  const body = {
    action: 'generate', name: T.cur.summary.file,
    new_name: $('genName').value.trim(),
    target_pace: $('genPace').value ? parseFloat($('genPace').value) : null,
    drift_deg: $('genDrift').value ? parseFloat($('genDrift').value) : null,
    seed: $('genSeed').value ? parseInt($('genSeed').value, 10) : null,
    rebuild_mileage: $('genRebuild').checked,
  };
  if (!body.new_name) { alert('请填新文件名'); return; }
  try {
    const r = await api('/api/task', body);
    $('genOut').innerHTML = `<div class="badge b-ok">已生成 ${r.summary.file}</div>`
      + '<ul style="margin:8px 0 0;padding-left:18px">'
      + (r.notes || []).map(x => `<li>${x}</li>`).join('') + '</ul>'
      + `<div class="hint" style="margin-top:6px">新表：${r.summary.mileage} km · `
      + `${r.summary.duration}s · 配速 ${r.summary.pace} · 步频 ${r.summary.cadence}</div>`;
    renderValidation(r.validation);
    log(`🧬 已生成任务表 ${r.summary.file}`);
    await loadTasks();
  } catch (e) {
    $('genOut').innerHTML = `<div class="badge b-bad">生成失败</div>
      <div class="detail" style="color:#ff5c5c;margin-top:6px">${e.message}</div>`;
  }
}

async function copyTask() {
  if (!T.cur) return;
  const n = prompt('复制为：', T.cur.summary.file.replace(/\.json$/i, '_copy.json'));
  if (!n) return;
  try {
    await api('/api/task', { action: 'copy', name: T.cur.summary.file, new_name: n });
    log('已复制为 ' + n); $('taskDlg').close(); await loadTasks();
  } catch (e) { alert(e.message); }
}

async function renameTask() {
  if (!T.cur) return;
  const n = prompt('改名为：', T.cur.summary.file);
  if (!n || n === T.cur.summary.file) return;
  try {
    await api('/api/task', { action: 'rename', name: T.cur.summary.file, new_name: n });
    log(`已改名 ${T.cur.summary.file} → ${n}`); $('taskDlg').close(); await loadTasks();
  } catch (e) { alert(e.message); }
}

async function deleteTask(file) {
  const name = file || (T.cur && T.cur.summary.file);
  if (!name) return;
  if (!confirm(`删除任务表 ${name}？不可撤销。`)) return;
  try {
    await api('/api/task', { action: 'delete', name });
    log('🗑 已删除任务表 ' + name);
    if (T.cur && T.cur.summary.file === name) $('taskDlg').close();
    await loadTasks();
  } catch (e) { alert(e.message); }
}

async function toProject() {
  if (!T.cur) return;
  try {
    const r = await api('/api/task', { action: 'to_project', name: T.cur.summary.file });
    log('📤 已送入项目目录：' + r.path);
    alert('已复制到项目打表目录：\n' + r.path + '\n\n可直接用 main.py -t 指向该目录运行。');
  } catch (e) {
    if (/已存在/.test(e.message) && confirm(e.message + '\n\n覆盖？')) {
      const r = await api('/api/task',
        { action: 'to_project', name: T.cur.summary.file, overwrite: true });
      log('📤 已覆盖并送入：' + r.path);
    } else alert(e.message);
  }
}

async function uploadTasks(input) {
  const files = [...input.files];
  input.value = '';
  for (const f of files) {
    const data = await new Promise(res => {
      const fr = new FileReader(); fr.onload = () => res(fr.result); fr.readAsDataURL(f);
    });
    try {
      await api('/api/task', { action: 'upload', name: f.name, data });
      log('⬆ 已导入任务表 ' + f.name);
    } catch (e) {
      if (/已存在/.test(e.message) && confirm(`${f.name} 已存在，覆盖？`)) {
        await api('/api/task', { action: 'upload', name: f.name, data, overwrite: true });
        log('⬆ 已覆盖 ' + f.name);
      } else log(`⚠ ${f.name} 未导入：${e.message}`);
    }
  }
  await loadTasks();
}

/* ================================================================ 当前项目路线预览 / 配置 */
const ROUTE = { meta: null, last: null, map: null, historyMap: null, mapKey: '', amapPromise: null };

function loadAmap(mapKey) {
  if (!mapKey) return Promise.reject(new Error('当前学号未配置高德 map_key'));
  if (window.AMap) return Promise.resolve(window.AMap);
  if (ROUTE.amapPromise) return ROUTE.amapPromise;
  ROUTE.amapPromise = new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = `https://webapi.amap.com/maps?v=2.0&key=${encodeURIComponent(mapKey)}`;
    script.onload = () => window.AMap ? resolve(window.AMap) : reject(new Error('高德 API 未提供 AMap'));
    script.onerror = () => reject(new Error('高德地图脚本加载失败'));
    document.head.appendChild(script);
  });
  return ROUTE.amapPromise;
}

async function drawAmapRoute(ref, send) {
  const cfg = await api('/api/control-config' + (S.student ? `?student=${encodeURIComponent(S.student)}` : ''));
  const mapKey = (((cfg.config || {}).User || {}).map_key || '').trim();
  const AMap = await loadAmap(mapKey);
  const host = $('routeMap');
  host.hidden = false;
  $('routeCanvas').hidden = true;
  if (!ROUTE.map) {
    ROUTE.map = new AMap.Map(host, { zoom: 16, viewMode: '2D', resizeEnable: true });
  }
  ROUTE.map.clearMap();
  const toLngLat = p => [Number(p[0]), Number(p[1])];
  const paths = [];
  if (ref.length) paths.push(new AMap.Polyline({ path: ref.map(toLngLat), strokeColor: '#7d8798', strokeWeight: 4, strokeStyle: 'dashed', strokeOpacity: .9 }));
  if (send.length) paths.push(new AMap.Polyline({ path: send.map(toLngLat), strokeColor: '#2379f6', strokeWeight: 5, strokeOpacity: .95 }));
  ROUTE.map.add(paths);
  const all = ref.concat(send).map(toLngLat);
  if (all.length) ROUTE.map.setFitView(paths, false, [50, 50, 50, 50]);
}

async function drawAmapHistory(points) {
  const cfg = await api('/api/control-config' + (S.student ? `?student=${encodeURIComponent(S.student)}` : ''));
  const mapKey = (((cfg.config || {}).User || {}).map_key || '').trim();
  const AMap = await loadAmap(mapKey);
  const host = $('historyMap');
  host.hidden = false;
  $('historyCanvas').hidden = true;
  if (!ROUTE.historyMap) ROUTE.historyMap = new AMap.Map(host, { zoom: 16, viewMode: '2D', resizeEnable: true });
  ROUTE.historyMap.clearMap();
  const path = points.map(p => {
    const pair = String(p.point || '').split(',').map(Number);
    return [pair[0], pair[1]];
  }).filter(p => Number.isFinite(p[0]) && Number.isFinite(p[1]));
  if (!path.length) throw new Error('历史记录没有有效轨迹坐标');
  const line = new AMap.Polyline({ path, strokeColor: '#2379f6', strokeWeight: 5, strokeOpacity: .95 });
  ROUTE.historyMap.add(line);
  ROUTE.historyMap.setFitView([line], false, [50, 50, 50, 50]);
}

function showHistoryCanvas(points) {
  $('historyMap').hidden = true;
  $('historyCanvas').hidden = false;
  const coords = points.map(p => String(p.point || '').split(',').map(Number))
    .filter(p => Number.isFinite(p[0]) && Number.isFinite(p[1]));
  drawRoute([], coords);
}

function showRouteCanvas() {
  $('routeMap').hidden = true;
  $('routeCanvas').hidden = false;
}

async function loadRouteMeta() {
  try {
    const j = await api('/api/route-meta');
    ROUTE.meta = j;
    const campus = $('routeCampus');
    const previous = campus.value;
    campus.innerHTML = Object.keys(j.campuses || {}).map(k =>
      `<option value="${k}">${k}</option>`).join('');
    if (previous && j.campuses[previous]) campus.value = previous;
    const src = $('routeSource');
    src.innerHTML = (j.sources || []).map(k => `<option value="${k}">${k}</option>`).join('');
    routeTaskFill();
    routeSchemeChanged();
  } catch (e) { log('读取路线选项失败：' + e.message); }
}

function routeTaskFill() {
  const campus = $('routeCampus').value;
  const tasks = (ROUTE.meta && ROUTE.meta.campuses && ROUTE.meta.campuses[campus]) || [];
  $('routeTask').innerHTML = tasks.map(k => `<option value="${k}">${k}</option>`).join('');
}

function routeSchemeChanged() {
  const deform = $('routeScheme').value === 'deform';
  $('routeTask').hidden = deform;
  $('routeCampus').hidden = deform;
  $('routeSource').hidden = !deform;
  $('routeSeed').hidden = !deform;
  $('routeInterval').hidden = !deform;
  $('routeHint').textContent = deform
    ? '方案 2 会基于底图生成新的几何轨迹，随机种子相同即可复现。'
    : '方案 1 直接预览所选任务表，执行时会锁定同一张表。';
}

async function previewRoute() {
  const scheme = $('routeScheme').value;
  const q = new URLSearchParams({ scheme });
  if (scheme === 'base') {
    q.set('campus', $('routeCampus').value);
    q.set('task', $('routeTask').value);
  } else {
    q.set('src', $('routeSource').value);
    q.set('seed', $('routeSeed').value || '31337');
    q.set('interval', $('routeInterval').value || '2');
  }
  try {
    const d = await api('/api/routes?' + q.toString());
    ROUTE.last = d;
    try { await drawAmapRoute(d.ref || [], d.send || []); }
    catch (mapError) { showRouteCanvas(); drawRoute(d.ref || [], d.send || []); $('routeHint').textContent = `未启用高德地图：${mapError.message}；当前显示离线路线图。`; }
    if (d.error) {
      $('routeMetrics').innerHTML = `<span class="badge b-bad">预览失败</span> ${d.error}`;
      $('routeCommand').textContent = '';
      return;
    }
    const m = d.metrics;
    $('routeMetrics').innerHTML = m
      ? `<div class="metric"><b>总里程</b><span>${m.ref_len_km} km → <strong>${m.send_len_km} km</strong></span></div>
         <div class="metric"><b>配速</b><span>${m.ref_pace ?? '—'} → <strong>${m.send_pace} min/km</strong></span></div>
         <div class="metric"><b>步频</b><span>${m.ref_cadence ?? '—'} → <strong>${m.send_cadence} spm</strong></span></div>
         <div class="metric"><b>采样</b><span>${m.points} 点 · ${m.interval_s}s / 点</span></div>`
      : `<div class="metric"><b>参考路线</b><span>${d.ref_name || '—'}</span></div>
         <div class="metric"><b>发送路线</b><span>${d.send_name || '—'}</span></div>
         <div class="metric"><b>总里程</b><span>${d.send_km ?? '—'} km · ${(d.send || []).length} 点</span></div>`;
    $('routeCommand').textContent = d.command
      ? `python main.py ${d.command.argv.join(' ')} ${d.command.dry.join(' ')}` : '';
    log('已生成路线预览：' + (d.send_name || '当前方案'));
  } catch (e) {
    $('routeMetrics').innerHTML = `<span class="badge b-bad">请求失败</span> ${e.message}`;
  }
}

function drawRoute(ref, send) {
  const c = $('routeCanvas');
  const ctx = c.getContext('2d');
  const w = c.width, h = c.height;
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = '#0b0d13'; ctx.fillRect(0, 0, w, h);
  for (let i = 1; i < 10; i++) {
    ctx.strokeStyle = '#161b27'; ctx.beginPath();
    ctx.moveTo(i * w / 10, 0); ctx.lineTo(i * w / 10, h); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(0, i * h / 10); ctx.lineTo(w, i * h / 10); ctx.stroke();
  }
  const all = ref.concat(send);
  if (!all.length) return;
  const xs = all.map(p => Number(p[0])), ys = all.map(p => Number(p[1]));
  const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
  const pad = 34, dx = Math.max(maxX - minX, 1e-8), dy = Math.max(maxY - minY, 1e-8);
  const point = p => [pad + (Number(p[0]) - minX) / dx * (w - 2 * pad),
                      h - pad - (Number(p[1]) - minY) / dy * (h - 2 * pad)];
  const line = (pts, color, dash, width) => {
    if (!pts.length) return; ctx.save(); ctx.strokeStyle = color; ctx.lineWidth = width;
    ctx.setLineDash(dash || []); ctx.beginPath();
    pts.forEach((p, i) => { const [x, y] = point(p); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    ctx.stroke(); ctx.restore();
  };
  line(ref, '#7d8798', [8, 6], 3); line(send, '#4c8dff', [], 4);
  ctx.fillStyle = '#e6e8ee'; ctx.font = '12px Microsoft YaHei'; ctx.fillText('参考路线', 16, 24);
  ctx.fillStyle = '#4c8dff'; ctx.fillText('当前路线', 100, 24);
}

async function loadControlConfig() {
  try {
    const query = S.student ? `?student=${encodeURIComponent(S.student)}` : '';
    const j = await api('/api/control-config' + query);
    const host = $('configForm'); host.innerHTML = '';
    for (const [sec, kv] of Object.entries(j.config || {})) {
      const box = document.createElement('div'); box.className = 'cfg-section';
      box.innerHTML = `<h3>[${sec}]</h3><div class="cfg-grid"></div>`;
      const grid = box.querySelector('.cfg-grid');
      for (const [key, val] of Object.entries(kv)) {
        const label = document.createElement('label'); label.textContent = key;
        const input = document.createElement('input'); input.dataset.sec = sec; input.dataset.key = key;
        if (val === '__MASKED__') { input.placeholder = '已隐藏；留空保持原值'; input.dataset.keep = '1'; }
        else input.value = val;
        grid.append(label, input);
      }
      host.appendChild(box);
    }
    $('configStatus').textContent = `已读取 ${j.student || '主项目'} · ${new Date().toLocaleTimeString()}`;
  } catch (e) { $('configStatus').textContent = '读取失败：' + e.message; }
}

async function saveControlConfig() {
  const config = {};
  document.querySelectorAll('#configForm input').forEach(input => {
    const sec = input.dataset.sec, key = input.dataset.key;
    config[sec] = config[sec] || {};
    config[sec][key] = input.dataset.keep && input.value === '' ? '__KEEP__' : input.value;
  });
  try {
    const j = await api('/api/control-config', { student: S.student || '', config });
    $('configStatus').textContent = `保存成功 ${j.student || '主项目'} · ${new Date().toLocaleTimeString()}`;
    log(`已保存 ${j.student || '主项目'} 的 config.ini`);
  } catch (e) { $('configStatus').textContent = '保存失败：' + e.message; }
}

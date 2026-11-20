'use strict';
const $ = id => document.getElementById(id);
const el = (tag, content, cls) => {
  const n = document.createElement(tag);
  if (content !== undefined) n.textContent = String(content);
  if (cls) n.className = cls;
  return n;
};
const button = (caption, action, cls) => {
  const n = el('button', caption, cls);
  n.onclick = action;
  return n;
};
const para = (parent, message, cls) => parent.appendChild(el('p', message, cls));
const title = (parent, message) => parent.appendChild(el('h3', message));
const bar = parent => parent.appendChild(el('div', undefined, 'bar'));
const terms = parent => parent.appendChild(el('div', undefined, 'pills'));
const select = (parent, choices, value, change) => {
  const s = el('select');
  for (const [key, caption] of choices) {
    const o = el('option', caption);
    o.value = key;
    s.append(o);
  }
  s.value = value;
  s.onchange = () => change(s.value);
  parent.append(s);
  return s;
};
const box = (parent, caption, open = false) => {
  const d = el('details');
  d.open = open;
  d.append(el('summary', caption));
  parent.append(d);
  return d;
};
const svgNS = 'http://www.w3.org/2000/svg';
const sv = (tag, attr = {}, value) => {
  const n = document.createElementNS(svgNS, tag);
  for (const [k, v] of Object.entries(attr)) n.setAttribute(k, v);
  if (value !== undefined) n.textContent = value;
  return n;
};
const relation = {
  direct: '源码直接写入',
  path: '源码多步候选',
  unresolved: '观测待对齐',
  source_only: '仅源码'
};
const number = v => Number(v).toLocaleString('zh-CN');
const oneLine = v => String(v || '').replace(/\s+/g, ' ').trim();
const stateLabel = (c, p) => c.name + '  ' + p.from + ' → ' + p.to;
let D, mode = 'states',
  chosen = null,
  page = 0,
  back = [],
  serial = 0;
const nodeMap = new Map(),
  symMap = new Map(),
  calls = new Map(),
  readings = new Map(),
  incoming = new Map();

function indexData() {
  for (const net of D.nets) {
    for (const n of net.nodes) nodeMap.set(n.id, [net, n]);
    for (const w of net.wires) {
      if (!incoming.has(w[2])) incoming.set(w[2], []);
      incoming.get(w[2]).push(w);
    }
  }
  for (const [i, s] of D.symbols.entries()) {
    const k = s.name.toLowerCase();
    if (!symMap.has(k)) symMap.set(k, []);
    symMap.get(k).push(i);
  }
  for (const c of D.calls || []) {
    if (!calls.has(c.node)) calls.set(c.node, []);
    calls.get(c.node).push(c);
  }
  for (const [i, c] of D.channels.entries())
    if (c.samples) readings.set(c.name.toLowerCase(), i);
}

function options() {
  if (mode === 'plant') return [['all','全部单元与关系'],['groups','程序 / 设备单元'],['peer','程序间步序门控'],['guards','跨单元步序门控'],['signals','共享信号'],['call','调用关系'],['task','任务挂载']];
  if (mode === 'states') return [
    ['observed', '实测通道'],
    ['source', '源码通道'],
    ['all', '全部通道']
  ];
  if (mode === 'nets') return [
    ['all', '全部语言'], ...Array.from(new Set(D.nets.map(n => n.language))).sort().map(x => [x, x])
  ];
  if (mode === 'symbols') return [
    ['all', '读写索引'],
    ['writes', '含写入位置'],
    ['reads', '含读取位置']
  ];
  return [
    ['all', '当前工程']
  ];
}

function setupTab(m, reset = true) {
  mode = m;
  page = 0;
  document.querySelectorAll('nav button').forEach(b => b.classList.toggle('active', b.dataset.mode === m));
  if (reset) $('filter').value = '';
  $('subset').replaceChildren();
  const choices = options();
  for (const [v, t] of choices) {
    const o = el('option', t);
    o.value = v;
    $('subset').append(o);
  }
  $('subset').value = choices[0][0];
  $('filter').placeholder = {
    plant: '查找工位、例程归属或协作信号',
    states: '完整地址或部分名称',
    nets: '例程、编号或网络注释',
    symbols: '信号或作用域',
    project: '查找程序、任务或例程'
  } [m];
}
const netName = n => (n.scope.split('::').pop() || n.scope) + ' · ' + n.language + ' ' + (n.label || ('L' + n
  .line));

function listRows() {
  const q = $('filter').value.trim().toLowerCase(),
    group = $('subset').value;
  const match = s => s.toLowerCase().includes(q);
  if (mode === 'plant') return Plant.listing(q,group);
  if (mode === 'states') return D.channels.flatMap((c, i) => (group === 'observed' && !c.samples) || (group ===
    'source' && !c.pairs.some(p => p.sites.length)) || !match(c.name) ? [] : [
    [i, c.name]
  ]);
  if (mode === 'nets') return D.nets.flatMap(n => (group !== 'all' && n.language !== group) || !match(n.scope +
    ' ' + n.label + ' ' + (n.comment || '')) ? [] : [
    [n.id, netName(n)]
  ]);
  if (mode === 'symbols') return D.symbols.flatMap((s, i) => (group !== 'all' && !s[group].length) || !match(s
    .name + ' ' + s.scope) ? [] : [
    [i, s.name + ' · ' + s.scope]
  ]);
  const rows = [
    ['overview', '全工程核验'], ...D.tasks.map((t, i) => ['t' + i, '任务 · ' + t.name]), ...D.units.map((u, i) => [
      'u' + i, u.kind + ' · ' + u.scope
    ])
  ];
  return rows.filter(([, label]) => match(label));
}

function sidebar() {
  const entries = listRows(),
    items = $('items');
  items.replaceChildren();
  $('found').textContent = number(entries.length) + ' 项';
  page = Math.max(0, Math.min(page, Math.ceil(entries.length / 60) - 1));
  for (const [i, label] of entries.slice(page * 60, page * 60 + 60))
    items.append(button(label, () => go(mode, i), chosen === i ? 'sel' : ''));
  if (entries.length > 60) {
    const row = bar(items);
    if (page) row.append(button('上一页', () => {
      page--;
      sidebar();
    }));
    if ((page + 1) * 60 < entries.length) row.append(button('下一页', () => {
      page++;
      sidebar();
    }));
  }
  return entries;
}
let lastOptions = {};

function go(m, key, opts = {}, record = true) {
  if (record && chosen !== null) back.push([mode, chosen, lastOptions, $('filter').value, $('subset').value,
    page
  ]);
  if (m !== mode) setupTab(m);
  chosen = key;
  lastOptions = opts;
  sidebar();
  const target = $('detail');
  target.replaceChildren();
  if (back.length) target.append(button('← 返回上一处', () => {
    const [tab, id, o, q, g, p] = back.pop();
    setupTab(tab);
    $('filter').value = q;
    $('subset').value = g;
    page = p;
    go(tab, id, o, false);
  }, 'navback'));
  if (key === null) {
    para(target, '没有匹配的条目。');
    return;
  }
  if (m === 'plant') Plant.scene(target,key);
  if (m === 'states') stateView(target, D.channels[key], opts);
  if (m === 'nets') networkView(target, D.nets[key], opts);
  if (m === 'symbols') symbolView(target, D.symbols[key]);
  if (m === 'project') projectView(target, key);
}

function paged(parent, rows, draw, size = 40) {
  let offset = 0;
  const inner = el('div'),
    actions = bar(parent);
  parent.append(inner);

  function show() {
    inner.replaceChildren();
    actions.replaceChildren();
    if (rows.length > size) {
      if (offset) actions.append(button('上一页', () => {
        offset -= size;
        show();
      }));
      actions.append(el('small', (offset + 1) + '–' + Math.min(offset + size, rows.length) + ' / ' + rows
        .length));
      if (offset + size < rows.length) actions.append(button('下一页', () => {
        offset += size;
        show();
      }));
    }
    for (const v of rows.slice(offset, offset + size)) draw(inner, v);
  }
  show();
}

function diagram(parent, nodes, edges, activate, description) {
  // Layout only. Back edges and parallel wires are retained, with individual terminal labels.
  const widths = 232,
    height = 74,
    stepX = 312,
    stepY = 106;
  const data = new Map(nodes.map(n => [n.id, n])),
    indeg = new Map(),
    rank = new Map(),
    children = new Map(),
    slots = new Map();
  for (const n of nodes) {
    indeg.set(n.id, 0);
    rank.set(n.id, 0);
    children.set(n.id, []);
  }
  for (const e of edges)
    if (data.has(e.a) && data.has(e.b) && e.a !== e.b) {
      children.get(e.a).push(e.b);
      indeg.set(e.b, indeg.get(e.b) + 1);
    }
  const queue = nodes.filter(n => !indeg.get(n.id)).map(n => n.id);
  const processed = new Set();
  for (let i = 0; i < queue.length; i++) {
    const a = queue[i];
    processed.add(a);
    for (const b of children.get(a)) {
      rank.set(b, Math.max(rank.get(b), rank.get(a) + 1));
      indeg.set(b, indeg.get(b) - 1);
      if (!indeg.get(b)) queue.push(b);
    }
  }
  let cycleIndex = 0;
  for (const n of nodes)
    if (!processed.has(n.id) && n.layer === undefined)
      rank.set(n.id, rank.get(n.id) + cycleIndex++ % Math.max(2, Math.ceil(Math.sqrt(nodes.length))));
  // Put constants beside their first consumer, then route every edge spanning
  // intervening columns via a separate gutter. A line must never pass under a box.
  for (const id of queue.slice().reverse()) {
    const next = children.get(id).filter(v => processed.has(v));
    if (next.length) rank.set(id, Math.max(rank.get(id), Math.min(...next.map(v => rank.get(v))) - 1));
  }
  for (const n of nodes) n.col = n.layer === undefined ? rank.get(n.id) : n.layer;
  const lanesTop = [];
  for (const e of edges) {
    const a = data.get(e.a),
      b = data.get(e.b);
    if (!a || !b || b.col === a.col + 1) continue;
    const lo = Math.min(a.col, b.col),
      hi = Math.max(a.col, b.col);
    let lane = lanesTop.findIndex(r => r.every(span => span[1] < lo || span[0] > hi));
    if (lane < 0) {
      lane = lanesTop.length;
      lanesTop.push([]);
    }
    lanesTop[lane].push([lo, hi]);
    e.track = lane;
  }
  let maxX = 1,
    maxY = 1;
  const leading = nodes.slice().sort((a, b) => Number(/^(invariable|datasource)$/i.test(a.type)) - Number(
    /^(invariable|datasource)$/i.test(b.type)));
  for (const n of leading) {
    const r = n.col;
    const k = slots.get(r) || 0;
    slots.set(r, k + 1);
    n.x = 34 + r * stepX;
    n.y = 40 + lanesTop.length * 13 + k * stepY;
    maxX = Math.max(maxX, n.x + widths + 44);
    maxY = Math.max(maxY, n.y + height + 54);
  }
  const controls = bar(parent),
    viewport = el('div', undefined, 'viewport');
  parent.append(viewport);
  const svg = sv('svg', {
    viewBox: '0 0 ' + maxX + ' ' + maxY,
    role: 'img',
    'aria-label': description
  });
  viewport.append(svg);
  let zoom = 1;

  function zoomTo(z) {
    zoom = Math.min(2, Math.max(.18, z));
    svg.setAttribute('width', Math.round(maxX * zoom));
    svg.setAttribute('height', Math.round(maxY * zoom));
  }
  controls.append(el('small', nodes.length + ' 个节点 · ' + edges.length + ' 条连线'), button('−', () => zoomTo(zoom /
    1.25)), button('＋', () => zoomTo(zoom * 1.25)), button('适应宽度', () => zoomTo(viewport.clientWidth /
    maxX)));
  zoomTo(Math.max(.72, Math.min(1, (viewport.clientWidth || 880) / maxX)));
  const prefix = 'd' + (++serial),
    defs = sv('defs');
  svg.append(defs);
  for (const [key, color] of Object.entries({
      wire: '#426874',
      pin: '#8293a6',
      selected: '#006e62',
      path: '#ae741b',
      observed: '#367eaa'
    })) {
    const m = sv('marker', {
      id: prefix + key,
      markerWidth: 8,
      markerHeight: 7,
      refX: 7,
      refY: 3.5,
      orient: 'auto',
      markerUnits: 'userSpaceOnUse'
    });
    m.append(sv('path', {
      d: 'M0,0 L7,3.5 L0,7 Z',
      fill: color
    }));
    defs.append(m);
  }
  const lanes = new Map(),
    ends = new Map();
  for (const e of edges) {
    for (const [key, pin] of [
        [e.a + 'o', e.out || ''],
        [e.b + 'i', e.in || '']
      ]) {
      if (!ends.has(key)) ends.set(key, []);
      if (!ends.get(key).includes(pin)) ends.get(key).push(pin);
    }
  }

  function portY(n, key, p) {
    const pins = ends.get(key) || [''];
    return n.y + 8 + (height - 16) * (pins.indexOf(p || '') + 1) / (pins.length + 1);
  }
  for (const e of edges) {
    const a = data.get(e.a),
      b = data.get(e.b);
    if (!a || !b) continue;
    const key = a.id + '>' + b.id,
      i = lanes.get(key) || 0;
    lanes.set(key, i + 1);
    const x = a.x + widths,
      y = portY(a, a.id + 'o', e.out),
      u = b.x,
      v = portY(b, b.id + 'i', e.in);
    const route = e.track === undefined ? 'M' + x + ' ' + y + ' C' + (x + 40 + i * 4) + ' ' + y + ' ' + (u -
        40 - i * 4) + ' ' + v + ' ' + u + ' ' + v :
      'M' + x + ' ' + y + ' H' + (x + 14 + i * 4) + ' V' + (16 + e.track * 13) + ' H' + (u - 14 - i * 4) +
      ' V' + v + ' H' + u;
    const style = e.style || 'wire',
      color = {
        pin: '#8293a6',
        selected: '#006e62',
        path: '#ae741b',
        observed: '#367eaa'
      } [style] || '#426874';
    const path = sv('path', {
      d: route,
      fill: 'none',
      stroke: color,
      'stroke-width': style === 'selected' ? 3 : 1.6,
      'marker-end': 'url(#' + prefix + style + ')'
    });
    if (style === 'path' || style === 'observed') path.setAttribute('stroke-dasharray', '6 3');
    const halo = sv('path', {
      d: route,
      fill: 'none',
      stroke: 'white',
      'stroke-width': 5
    });
    svg.append(halo);
    if (style === 'pin') path.setAttribute('stroke-dasharray', '3 2');
    path.append(sv('title', {}, [a.label, e.out || '', '→', b.label, e.in || '', e.label || ''].join(' ')));
    svg.append(path);
    for (const [val, tx, ty, anchor] of [
        [e.out, x + 4, y - 4, 'start'],
        [e.in, u - 4, v - 4, 'end']
      ])
      if (val) svg.append(sv('text', {
        x: tx,
        y: ty,
        'text-anchor': anchor,
        'font-size': 9,
        fill: '#52636d'
      }, val));
  }
  for (const n of nodes) {
    const group = sv('g', {
      transform: 'translate(' + n.x + ' ' + n.y + ')',
      class: 'svgNode',
      tabindex: 0,
      role: 'button',
      'aria-label': n.label
    });
    const color = n.active ? '#00736d' : n.opaque ? '#a26a20' : '#6b858f';
    group.append(sv('rect', {
      width: widths,
      height,
      rx: 6,
      fill: n.active ? '#ddf3ee' : n.opaque ? '#fff6e7' : '#f6fafb',
      stroke: color,
      'stroke-width': n.active ? 2.5 : 1.3
    }));
    const type = (n.type || '').toLowerCase(),
      neg = String(n.neg || '').toLowerCase() === 'true',
      ink = '#23505c';
    if (type === 'contact' || type === 'ld') {
      group.append(sv('path', {
        d: 'M9 19 H16 M16 11 V27 M24 11 V27 M24 19 H32' + (neg ? ' M14 27 L26 11' : ''),
        stroke: ink,
        fill: 'none',
        'stroke-width': 1.7
      }));
    } else if (type === 'coil' || type === 'stcoil') {
      group.append(sv('path', {
        d: 'M8 19 H14 M20 11 Q11 19 20 27 M24 11 Q33 19 24 27 M29 19 H35',
        stroke: ink,
        fill: 'none',
        'stroke-width': 1.7
      }));
    }
    group.append(sv('text', {
      x: ['contact', 'ld', 'coil', 'stcoil'].includes(type) ? 40 : 9,
      y: 22,
      'font-size': 11,
      fill: color
    }, String(n.type || '').slice(0, 26)));
    const line = oneLine(n.label),
      limit = /[^\x00-\x7f]/.test(line) ? 16 : 25;
    group.append(sv('text', {
      x: 9,
      y: 45,
      'font-size': 12.5,
      'font-weight': 600,
      fill: '#19343c'
    }, line.slice(0, limit)));
    const rest = line.slice(limit),
      more = rest || (n.target ? '→ ' + n.target : '');
    if (more) group.append(sv('text', {
      x: 9,
      y: 63,
      'font-size': 11.5,
      fill: '#365866'
    }, more.length > limit + 2 ? more.slice(0, limit + 1) + '…' : more));
    group.append(sv('title', {}, n.label));
    group.onclick = () => activate(n);
    group.onkeydown = e => {
      if (e.key === 'Enter') activate(n);
    };
    svg.append(group);
  }
}

function showSignals(parent, labels, chooseScope = false) {
  const list = terms(parent);
  for (const label of labels) {
    const matches = symMap.get(label.toLowerCase()) || [];
    list.append(button(label, () => {
      if (matches.length === 1 && !chooseScope) go('symbols', matches[0]);
      else {
        go('symbols', null);
        $('filter').value = label;
        sidebar();
        para($('detail'), matches.length ? '请选择经声明核对的作用域。' : '未定位到该操作数的明确读写地址。');
      }
    }));
  }
}

function gateFigure(parent, site) {
  para(parent, '根据源码条件推导的逻辑梯级。串联表示 AND，并联表示 OR。悬停可读完整表达式。', 'muted');
  const g = el('div', undefined, 'viewport');
  g.append(LD.guard(site.guard_tree, site.channel + ' := ' + site.end));
  parent.append(g);
}

function siteCard(parent, index, first = false) {
  const s = D.sites[index],
    panel = box(parent, 'L' + s.source_line + (s.st_line ? ' / ST ' + s.st_line : '') + '　' + s.start + ' → ' +
      s.end + '　' + (s.scope.split('::').pop() || s.scope), first);

  function draw() {
    if (panel.dataset.drawn) return;
    panel.dataset.drawn = '1';
    para(panel, s.scope);
    para(panel, (s.guard_complete ? '已解析局部条件' : '局部条件含待解依赖') + ' · ' + s.kind, 'tag');
    panel.append(el('pre', s.guard));
    if (s.comment) para(panel, '源码注释　' + s.comment);
    const actions = bar(panel);
    if (s.net !== null && s.net !== undefined) actions.append(button('打开源码网络', () => go('nets', s.net, {
      focus: s.source_node,
      stLine: s.st_line,
      write: s.channel + ' := ' + s.end
    })));
    const gate = box(panel, '门控图', first);
    const paint = () => {
      if (gate.open && !gate.dataset.drawn) {
        gate.dataset.drawn = '1';
        gateFigure(gate, s);
      }
    };
    gate.ontoggle = paint;
    paint();
    if (s.binding && s.binding.length) {
      const d = box(panel, '实例与实参绑定', true);
      for (const b of s.binding) para(d, (b.scope || '') + ' → ' + (b.instance || '') + ' ' + (b.type || '') +
        '　L' + b.source_line + '　' + JSON.stringify(b.reference_actuals || b.actuals || {}));
    }
    if (s.output_dependencies && s.output_dependencies.length) {
      const d = box(panel, '依赖的功能块输出');
      d.append(el('pre', JSON.stringify(s.output_dependencies, null, 2)));
    }
    if (s.reads.length) {
      title(panel, '查找条件操作数的读写位置');
      const refs = new Map(s.signal_refs || []),
        links = terms(panel);
      for (const name of s.reads) {
        if (refs.has(name)) links.append(button(name, () => go('symbols', refs.get(name))));
        else showSignals(links, [name], true);
      }
    }
  }
  panel.ontoggle = () => {
    if (panel.open) draw();
  };
  if (first) draw();
}

function stateView(p, c, o) {
  p.append(el('h2', c.name));
  para(p, number(c.samples) + ' 个采样点 · ' + c.pairs.filter(x => x.events).length + ' 个观测状态对 · ' + c.pairs
    .length + ' 个源码或观测状态对', 'muted');
  if (!c.pairs.length) {
    para(p, '该通道没有跳变。已观测值　' + c.observed_values.join('、'));
    return;
  }
  const kinds = [
    ['measured', '全部观测'],
    ['all', '源码与观测'], ...Object.entries(relation)
  ];
  let group = o.group || (c.samples && c.pairs.some(x => x.events) ? 'measured' : 'all');
  let candidates = c.pairs.filter(x => group === 'all' || (group === 'measured' ? x.events : x.relation ===
    group));
  const controls = bar(p);
  select(controls, kinds, group, g => go('states', chosen, {
    group: g
  }, false));
  if (!candidates.length) {
    para(p, '该范围内没有状态对。');
    return;
  }
  const e = candidates.find(x => x.from === o.from && x.to === o.to) || candidates[0];
  select(controls, candidates.map((r, i) => [i, r.from + ' → ' + r.to + '　' + relation[r.relation]]), candidates
    .indexOf(e), v => {
      const r = candidates[+v];
      go('states', chosen, {
        group,
        from: r.from,
        to: r.to
      }, false);
    });
  const subtitle = el('h3', stateLabel(c, e));
  p.append(subtitle);
  para(p, relation[e.relation] + '　' + (e.events ? number(e.events.length) + ' 次实测' : '本次采样未见') + '　' + e.sites
    .length + ' 个直接写点', e.relation === 'unresolved' ? 'warn' : 'tag');
  const branch = box(p, '状态邻接图', false);
  branch.ontoggle = () => {
    if (!branch.open || branch.dataset.drawn) return;
    branch.dataset.drawn = '1';
    const left = {
        id: 'a',
        label: String(e.from),
        type: '起始状态',
        layer: 0,
        active: true
      },
      ns = [left],
      es = [];
    const targets = (group === 'measured' ? c.pairs.filter(x => x.events) : c.pairs).filter(x => x.from === e
      .from);
    for (const r of targets) {
      const id = 'b' + r.to;
      ns.push({
        id,
        label: String(r.to),
        type: relation[r.relation],
        layer: 1,
        active: r.to === e.to,
        pair: r
      });
      es.push({
        a: 'a',
        b: id,
        style: r.relation === 'unresolved' ? 'observed' : r.relation === 'path' ? 'path' : r.to === e.to ?
          'selected' : 'wire'
      });
    }
    diagram(branch, ns, es, n => {
      if (n.pair) go('states', chosen, {
        from: n.pair.from,
        to: n.pair.to,
        group: 'all'
      });
    }, '同一起始状态的出边');
  };
  if (e.sites.length) {
    title(p, '源码直接写点');
    e.sites.forEach((s, i) => siteCard(p, s, i === 0));
  }
  if (e.paths) {
    title(p, '同一写入作用域内的候选路径');
    para(p, '每个作用域展示一条最短可达路径。采样间的中间状态及各门控取值仍需实测核验。');
    for (const example of e.paths) {
      const route = [e.from, ...example.map(h => D.sites[h[0]].end)];
      const trail = box(p, route.join(' → '), true);
      const ns = route.map((v, i) => ({
        id: i,
        label: String(v),
        type: i === 0 || i === route.length - 1 ? '观测端点' : '候选中间状态',
        active: i === 0 || i === route.length - 1
      }));
      diagram(trail, ns, example.map((_, i) => ({
        a: i,
        b: i + 1,
        style: 'path'
      })), n => {
        if (n.id < example.length) siteCard(trail, example[n.id][0], true);
      }, '两次采样之间的一条源码候选路径');
      for (const hop of example)
        for (const idx of hop) siteCard(trail, idx);
    }
  }
  if (e.relation === 'unresolved') para(p, '当前源码索引未建立这两个采样值之间的写入路径，可沿工程目录核对未解析的指令、间接写入或地址绑定。');
  if (c.unbound_source_addresses) para(p, '同名源码地址的作用域待核　' + c.unbound_source_addresses.join('、'), 'warn');
  if (e.events) {
    const found = e.events.filter(v => v[3]).length;
    para(p, found ? '其中 ' + found + ' 次有参与直接写点门控的同采样操作数读数。读数位于观测到新状态的时刻。' : '这些跳变没有可唯一对时并绑定到直接写点的门控操作数读数。',
      'muted');
    const d = box(p, '查看采样位置（' + e.events.length + ' 次）');
    d.ontoggle = () => {
      if (!d.open || d.dataset.drawn) return;
      d.dataset.drawn = '1';
      paged(d, e.events, (t, v) => para(t, '采集 ' + v[0] + ' · 样本 ' + v[1] + ' · 记录内时间 ' + v[2] + ' ms' + (v[
        3] ? '　' + Object.entries(v[3]).map(([k, x]) => k + '=' + x).join('，') : '')));
    };
  }
}

function scopeList(p, scope) {
  const nets = D.nets.filter(n => n.scope === scope);
  if (!nets.length) {
    para(p, '工程索引未定位到该定义的可绘网络。', 'muted');
    return;
  }
  paged(p, nets, (parent, n) => parent.append(button(netName(n) + (n.comment ? '　' + oneLine(n.comment).slice(0,
    60) : ''), () => go('nets', n.id))));
}

function nodeDetail(p, net, n, o) {
  p.replaceChildren();
  title(p, n.type + '　' + n.label);
  para(p, 'XML L' + n.line + (n.key !== undefined ? ' · 元件 ' + n.key : '') + ' · ' + net.scope, 'muted');
  const extra = Object.entries(n).filter(([k]) => !['id', 'key', 'line', 'type', 'label', 'assignments',
    'limits', 'st', 'other_statements', 'operands'
  ].includes(k));
  if (extra.length) para(p, extra.map(([k, v]) => k + '=' + (typeof v === 'object' ? JSON.stringify(v) : v))
    .join('　'));
  if (n.operands) {
    const t = el('table');
    p.append(t);
    for (const [dir, value, pin] of n.operands) {
      const r = el('tr');
      r.append(el('td', ({
        read: '读',
        write: '写',
        inout: '读写'
      })[dir]), el('td', pin || ''));
      const td = el('td');
      if (symMap.has(value.toLowerCase())) showSignals(td, [value]);
      else td.textContent = value;
      r.append(td);
      t.append(r);
    }
  }
  for (const c of calls.get(n.id) || []) {
    const d = box(p, (c.resolved ? '调用定义' : '多个同名定义待核') + ' · ' + c.type);
    for (const dest of c.targets) {
      para(d, dest);
      scopeList(d, dest);
    }
  }
  if (n.st) {
    const d = box(p, 'ST 原文 · ' + n.st.split('\n').length + ' 行', true);
    const code = el('pre', n.st.split('\n').map((s, i) => (i + 1 === o.stLine ? '▶ ' : '  ') + (i + 1) + '  ' +
      s).join('\n'));
    d.append(code);
    const a = box(p, '局部受控赋值（' + n.assignments.length + '）');
    a.ontoggle = () => {
      if (!a.open || a.dataset.drawn) return;
      a.dataset.drawn = '1';
      paged(a, n.assignments, (dest, v) => para(dest, 'ST ' + v.line + ' · ' + v.guard + ' → ' + v.target +
        ' := ' + v.value));
    };
    if (n.limits || n.other_statements) {
      const issues = box(p, 'ST 解析边界');
      issues.append(el('pre', JSON.stringify([...(n.limits || []), ...(n.other_statements || [])], null, 2)));
    }
  }
  if (net.id !== chosen) p.append(button('打开此元件所属网络', () => go('nets', net.id, {
    focus: n.id
  })));
}

function networkView(p, net, o) {
  p.append(el('h2', netName(net)));
  if (o.write) para(p, '当前写点　' + o.write, 'tag');
  para(p, net.scope + ' · XML L' + net.line, 'muted');
  if (net.comment) para(p, net.comment);
  const focused = o.focus !== null && o.focus !== undefined && nodeMap.has(o.focus);
  const row = bar(p),
    g = el('div'),
    info = el('div', undefined, 'paper');
  const locator = el('input');
  locator.placeholder = '查找本网络元件或操作数';
  locator.setAttribute('aria-label', '查找本网络元件');
  row.append(locator);
  const hits = terms(p);
  locator.oninput = () => {
    hits.replaceChildren();
    const q = locator.value.trim().toLowerCase();
    if (!q) return;
    const found = net.nodes.filter(n => (n.label + ' ' + (n.key || '') + ' ' + (n.operands || []).flat().join(
      ' ')).toLowerCase().includes(q));
    paged(hits, found, (t, n) => t.append(button(n.label + ' · L' + n.line, () => go('nets', net.id, {
      focus: n.id
    }))));
  };
  p.append(g, info);
  if (!net.nodes.length) {
    para(g, '该网络只含标题或导出器元数据。');
    return;
  }
  let showFull = !focused;
  const change = button(showFull ? '显示选中元件的上游' : '展开整个网络', () => {
    showFull = !showFull;
    change.textContent = showFull ? '显示选中元件的上游' : '展开整个网络';
    draw();
  });
  row.append(change);
  let focus = focused ? o.focus : net.nodes[0].id;

  function inspect(id) {
    const pair = nodeMap.get(id);
    if (pair) nodeDetail(info, pair[0], pair[1], o);
  }

  function draw() {
    let ids = new Set(),
      wires = [];
    if (showFull) {
      ids = new Set(net.nodes.map(n => n.id));
      wires = net.wires.slice();
      for (const w of wires) {
        ids.add(w[0]);
        ids.add(w[2]);
      }
    } else {
      const queue = [focus];
      ids.add(focus);
      for (let j = 0; j < queue.length; j++) {
        const here = queue[j];
        for (const w of incoming.get(here) || []) {
          wires.push(w);
          if (!ids.has(w[0])) {
            ids.add(w[0]);
            queue.push(w[0]);
          }
        }
      }
    }
    g.replaceChildren();
    if (['LD','SMC2'].includes(net.language)) {
      para(g, '源码梯形图　实线为控制通路，虚线为数据或其他引脚。触点与线圈保留取反、边沿和置位复位标记。点选元件查看原文。', 'muted');
      LD.legend(g);
      const viewport = el('div',undefined,'viewport');
      const plan=LD.layout([...ids].map(id=>nodeMap.get(id)[1]),wires,net.language);
      const svg=LD.draw(plan, id=>{
        focus=id;
        inspect(id);
      },focus);
      viewport.append(svg);
      const zoomBar=bar(g);
      let scale=1;
      function resize(factor){
        scale=Math.max(0.12,Math.min(3,factor));svg.setAttribute('width',plan.width*scale);svg.setAttribute('height',plan.height*scale);
      }
      zoomBar.append(button('缩小',()=>resize(scale/1.25)),button('放大',()=>resize(scale*1.25)),
        button('适应宽度',()=>resize((viewport.clientWidth||900)/plan.width)),button('原始比例',()=>resize(1)));
      g.append(viewport);
      if(focused){
        const pt=plan.points.get(focus);
        if(pt){viewport.scrollLeft=Math.max(0,pt.x-(viewport.clientWidth||900)/2);viewport.scrollTop=Math.max(0,pt.y-(viewport.clientHeight||470)/2);}
      }
      inspect(focus);
      return;
    }
    para(g, '箭头指向输入。实线为已识别的控制连接，点线为其余引脚连接；[#] 表示 XML 中未命名输入的位置。', 'muted');
    const nodes = Array.from(ids).map(id => {
      const [origin, n] = nodeMap.get(id);
      const storage = n.storage && String(n.storage).toLowerCase() !== 'none' ? n.storage : '';
      const edge = n.edge && String(n.edge).toLowerCase() !== 'none' ? n.edge : '';
      return {
        id,
        type: n.type === 'ST' && origin.language === 'SMC2' ? 'stcoil' : n.type,
        label: [storage, edge, n.label].filter(Boolean).join(' · '),
        active: focus === id,
        neg: n.negated || n.Not,
        target: (n.operands || []).filter(o => o[0] === 'write' || o[0] === 'inout').map(o => o[1]).join(
          ', ')
      };
    });
    diagram(g, nodes, wires.map(w => ({
        a: w[0],
        out: w[1],
        b: w[2],
        in: w[3] || (w[5] ? '[#' + w[5] + ']' : ''),
        style: w[4] === 'control' ? 'wire' : 'pin'
      })),
      n => {
        focus = n.id;
        inspect(n.id);
      }, '源码接线与形式端口');
    inspect(focus);
  }
  draw();
  const issues = D.wiring_issues.filter(x => x.net === net.id);
  if (issues.length) {
    const d = box(p, '本网络待核引用 · ' + issues.length);
    d.append(el('pre', JSON.stringify(issues, null, 2)));
  }
}

function symbolView(p, s) {
  p.append(el('h2', s.name));
  para(p, s.scope, 'muted');
  if (s.scope.startsWith('unresolved:')) para(p, '声明作用域未核实；这里列出同一例程内的同名出现位置。', 'warn');
  if (s.guard_sites && s.guard_sites.length) {
    const d = box(p, '此地址参与门控的写步位置 · ' + s.guard_sites.length);
    d.ontoggle = () => {
      if (d.open && !d.dataset.drawn) {
        d.dataset.drawn = '1';
        paged(d, s.guard_sites, (dest, i) => siteCard(dest, i), 15);
      }
    };
  }
  for (const [field, caption] of [
      ['writes', '可能写入此地址的源码位置'],
      ['reads', '读取此地址的源码位置']
    ]) {
    title(p, caption + ' · ' + s[field].length);
    if (!s[field].length) {
      para(p, '未定位到对应源码位置。', 'muted');
      continue;
    }
    paged(p, s[field], (dest, [j, id, pin]) => {
      const n = nodeMap.get(id)[1];
      dest.append(button(netName(D.nets[j]) + '　L' + n.line + (pin ? ' · ' + pin : ''), () => go('nets',
      j, {
        focus: id
      })));
    });
  }
}

function relationshipView(parent, relations, focus, open, label) {
  if (!relations.length) {
    para(parent, '本次未提取到这种关系。', 'muted');
    return;
  }
  const owners = Array.from(new Set(relations.flatMap(r => [r.a, r.b]))).sort();
  const nav = bar(parent),
    area = el('div');
  parent.append(area);

  function draw(name) {
    picker.value = name;
    area.replaceChildren();
    const near = relations.filter(r => r.a === name || r.b === name),
      ids = Array.from(new Set(near.flatMap(r => [r.a, r.b])));
    const ns = ids.map(id => ({
      id,
      label: id.split('::').pop(),
      type: id === name ? '选中' : label,
      active: id === name
    }));
    diagram(area, ns, near.map(r => ({
      a: r.a,
      b: r.b,
      label: r.text,
      style: 'wire'
    })), n => draw(n.id), '工程中的直接源码关系');
    paged(area, near, (d, r) => d.append(button(r.a + ' → ' + r.b + '　' + r.text, () => open(r))), 18);
  }
  const picker = select(nav, owners.map(v => [v, v]), focus || owners[0], v => draw(v));
  draw(focus || owners[0]);
}

function projectView(p, key) {
  if (key !== 'overview') {
    if (key[0] === 't') {
      const t = D.tasks[+key.slice(1)];
      p.append(el('h2', t.name));
      para(p, 'XML L' + t.source_line + '　' + (t.kind || '') + (t.period_ms ? '　' + t.period_ms + ' ms' : ''));
      title(p, '挂载的程序实例');
      for (const r of t.pou_instances) {
        const choices = D.units.filter(u => u.kind.toLowerCase() === 'program' && u.scope.split('::').pop()
          .toLowerCase() === (r.type_name || r.name).toLowerCase());
        const d = box(p, r.name + (r.type_name ? ' · ' + r.type_name : ''));
        for (const u of choices) scopeList(d, u.scope);
        if (!choices.length) para(d, '导出的程序体未定位。');
      }
    } else {
      const u = D.units[+key.slice(1)];
      p.append(el('h2', u.scope));
      para(p, u.kind + ' · XML L' + u.line, 'muted');
      scopeList(p, u.scope);
      const called = (D.calls || []).filter(c => c.targets.includes(u.scope));
      if (called.length) {
        title(p, '引用本定义的调用点');
        paged(p, called, (d, c) => d.append(button(netName(D.nets[c.net]) + ' · L' + nodeMap.get(c.node)[1]
          .line, () => go('nets', c.net, {
            focus: c.node
          }))));
      }
    }
    return;
  }
  p.append(el('h2', '工程与核验'));
  para(p, D.source_name + '　/　' + D.trace_name);
  const tiles = el('div', undefined, 'tiles');
  p.append(tiles);
  for (const [k, l] of [
      ['nets', '源码网络'],
      ['nodes', '源码元件'],
      ['wires', '端口连线'],
      ['observed_pairs', '观测状态对'],
      ['direct', '有直接写点'],
      ['path', '仅多步候选'],
      ['unresolved', '观测待对齐'],
      ['source_only', '仅源码状态对']
    ]) {
    const tile = el('div');
    tile.append(el('span', number(D.summary[k] || 0), 'number'), el('small', l));
    tiles.append(tile);
  }
  const deps = box(p, '跨步序变量的条件依赖', true);
  const rel = (D.channel_dependencies || []).map(x => ({
    a: x.read,
    b: x.written,
    text: x.sites.length + ' 个写点',
    sites: x.sites
  }));
  relationshipView(deps, rel, null, r => {
    const d = box(deps, r.a + ' 出现在 ' + r.b + ' 的写入条件中', true);
    paged(d, r.sites, (section, id) => siteCard(section, id), 12);
  }, '步序变量');
  const hierarchy = box(p, '导出的例程调用关系');
  hierarchy.ontoggle = () => {
    if (!hierarchy.open || hierarchy.dataset.drawn) return;
    hierarchy.dataset.drawn = '1';
    const grouped = new Map();
    for (const c of D.calls || []) {
      if (!c.resolved) continue;
      const a = D.nets[c.net].scope,
        b = c.targets[0],
        k = JSON.stringify([a, b]);
      if (!grouped.has(k)) grouped.set(k, {
        a,
        b,
        calls: []
      });
      grouped.get(k).calls.push(c);
    }
    const links = Array.from(grouped.values()).map(r => ({
      ...r,
      text: r.calls.length + ' 个调用点'
    }));
    relationshipView(hierarchy, links, null, r => {
      const d = box(hierarchy, r.a + ' → ' + r.b, true);
      paged(d, r.calls, (dest, c) => dest.append(button(netName(D.nets[c.net]) + ' · L' + nodeMap.get(c
          .node)[1].line,
        () => go('nets', c.net, {
          focus: c.node
        }))));
    }, '例程');
  };
  title(p, '证据边界');
  para(p, 'Trace 记录采样时刻的值。直接写点、分支、实参和读写索引来自静态源码。多个写点、未采标志位和未展开的库块均需结合现场证据核验。');
  para(p, '物理端口与连接可逐网络核对。ST 保留原文与分支下的赋值；不支持的流程、未定位的状态写点及未解析的接线可在下方查看。');
  const audits = [
    ['输入指纹', D.inputs],
    ['源码解析范围', D.limits],
    ['结构统计', D.summary],
    ['采集与断档', D.trace_audit]
  ];
  for (const [name, v] of audits) {
    const d = box(p, name);
    d.ontoggle = () => {
      if (d.open && !d.dataset.drawn) {
        d.dataset.drawn = '1';
        d.append(el('pre', JSON.stringify(v, null, 2)));
      }
    };
  }
  const limits = box(p, '接线与语言待核 · ' + D.wiring_issues.length);
  limits.ontoggle = () => {
    if (limits.open && !limits.dataset.drawn) {
      limits.dataset.drawn = '1';
      paged(limits, D.wiring_issues, (d, x) => {
        const caption = 'L' + x.line + ' · ' + x.reason + ' ' + (x.reference || '');
        if (x.net !== undefined) d.append(button(caption, () => go('nets', x.net)));
        else para(d, caption);
      });
    }
  };
}
async function start() {
  const bytes = Uint8Array.from(atob($('payload').textContent.trim()), c => c.charCodeAt(0));
  const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'));
  D = await new Response(stream).json();
  $('payload').remove();
  indexData();
  $('project').textContent = D.source_name + '　·　' + number(D.summary.observed_pairs || 0) + ' 个观测状态对　·　' +
    number(D.summary.nets) + ' 个源码网络';

  function entry(m) {
    if (chosen !== null) back.push([mode, chosen, lastOptions, $('filter').value, $('subset').value, page]);
    setupTab(m);
    const list = sidebar();
    go(m, list.length ? list[0][0] : null, {}, false);
  }
  for (const b of document.querySelectorAll('nav button')) b.onclick = () => entry(b.dataset.mode);
  $('filter').oninput = () => {
    page = 0;
    sidebar();
  };
  $('subset').onchange = () => {
    page = 0;
    const list = sidebar();
    go(mode, list.length ? list[0][0] : null);
  };
  setupTab('plant');
  go('plant','home',{},false);
}
start().catch(err => {
  $('detail').textContent = '图谱读取失败。请用支持本地 gzip 解码的当前版 Edge、Chrome 或 Firefox 打开。 ' + err.message;
});

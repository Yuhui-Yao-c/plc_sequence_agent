/* Directed cross references among executable programs. Each cell counts distinct addresses. */
const Overview = (() => {
  let programs, owner, observations, aliases;
  const matrices = new Map();
  const name = id => D.plant.groups[id].scope.split('::').at(-1);

  function index() {
    if (programs) return;
    programs = D.plant.groups.map((group, i) => i)
      .filter(i => D.plant.groups[i].kind.toLowerCase() === 'program')
      .sort((a, b) => name(a).localeCompare(name(b), 'zh-CN', {
        numeric: true
      }));
    owner = D.plant.net_routine.map(r => D.plant.routines[r].group);
    observations = new Map();
    for (const channel of D.channels)
      for (const pair of channel.pairs)
        if (pair.events?.length)
          for (const site of pair.sites) observations.set(site, pair.events.length);
    const split = id => name(id).split(/[\s_·-]+/u)[0];
    const counts = new Map();
    for (const id of programs) counts.set(split(id), (counts.get(split(id)) || 0) + 1);
    aliases = new Map(programs.map(id => [id, counts.get(split(id)) === 1 ? split(id) : name(id)]));
  }

  function matrix(mode) {
    index();
    if (matrices.has(mode)) return matrices.get(mode);
    const rows = new Map(),
      cols = new Set(),
      executable = new Set(programs);
    for (const bus of D.plant.signal_buses) {
      const readers = mode === 'all' ? new Set([...bus.readers, ...bus.guard_groups]) :
        mode === 'observed' ? new Set(bus.guards.filter(i => observations.has(i)).map(i => owner[D.sites[i]
          .net])) :
        bus.guard_groups;
      for (const from of bus.writers) {
        if (!executable.has(from)) continue;
        for (const to of readers) {
          if (from === to || !executable.has(to)) continue;
          if (!rows.has(from)) rows.set(from, new Map());
          const cells = rows.get(from);
          if (!cells.has(to)) cells.set(to, []);
          cells.get(to).push(bus);
          cols.add(to);
        }
      }
    }
    const result = {
      rows,
      cols
    };
    matrices.set(mode, result);
    return result;
  }

  function listing(q) {
    index();
    return [
        ['home', '全工程协作'], ...programs.map(i => ['g' + i, name(i)])
      ]
      .filter(([, caption]) => caption.toLowerCase().includes(q));
  }

  function render(parent, key, opts) {
    index();
    const mode = opts.scope || 'guard',
      data = matrix(mode);
    const pinned = /^g\d+$/.test(key) ? Number(key.slice(1)) : null;
    const from = opts.from,
      to = opts.to;
    const change = (next, where = key) => go('overview', where, {
      ...opts,
      ...next
    });
    const scopeNames = [
      ['guard', '步序门控'],
      ['observed', '有实测跳变的门控'],
      ['all', '全部跨程序读写']
    ];
    parent.append(el('h2', pinned == null ? '工位协作总览' : name(pinned)));
    const controls = bar(parent);
    const pick = select(controls, scopeNames, mode, scope => change({
      scope,
      from: null,
      to: null,
      sid: null,
      gate: null,
      x: 0,
      y: 0
    }));
    pick.setAttribute('aria-label', '协作范围');
    controls.append(button(pinned == null ? '整机图纸' : '展开程序图纸',
      () => go('plant', pinned == null ? 'home' : 'g' + pinned)));
    if (pinned != null) controls.append(button('返回全工程', () => go('overview', 'home', {
      scope: mode
    })));
    const senders = programs.filter(i => data.rows.has(i) && (pinned == null || i === pinned || data.rows.get(
      i).has(pinned)));
    const receivers = programs.filter(i => data.cols.has(i) && (pinned == null || i === pinned || data.rows
      .get(pinned)?.has(i)));
    if (!senders.length || !receivers.length) {
      para(parent, '此范围没有可配对的程序写端和读端。可切换范围，或从图纸进入程序与例程。', 'muted');
      return;
    }
    const n = 18,
      rowPage = opts.y || 0,
      colPage = opts.x || 0;
    const rr = senders.slice(rowPage * n, (rowPage + 1) * n),
      cc = receivers.slice(colPage * n, (colPage + 1) * n);
    const sub = bar(parent);
    sub.append(el('span', '行是写入方，列是接收方。点数字查看两端共用的信号。', 'matrix-caption'));
    for (const [axis, pageNo, count, label] of [
        ['y', rowPage, senders.length, '行'],
        ['x', colPage, receivers.length, '列']
      ]) {
      if (count <= n) continue;
      for (const [shift, text] of [
          [-1, '上一组'],
          [1, '下一组']
        ]) {
        const b = button(text + label, () => change({
          [axis]: pageNo + shift
        }));
        b.disabled = pageNo + shift < 0 || (pageNo + shift) * n >= count;
        sub.append(b);
      }
      sub.append(el('small', label + ' ' + (pageNo * n + 1) + '–' + Math.min(count, (pageNo + 1) * n) + ' / ' +
        count));
    }
    const wrap = el('div', undefined, 'matrix-wrap'),
      grid = el('table', undefined, 'matrix-grid');
    grid.setAttribute('aria-label', '程序间有向信号矩阵');
    const header = el('tr');
    header.append(el('th', '写入方 ↓'));
    for (const id of cc) {
      const th = el('th', aliases.get(id));
      th.title = name(id);
      th.scope = 'col';
      header.append(th);
    }
    const head = grid.createTHead ? grid.createTHead() : grid.appendChild(el('thead'));
    head.append(header);
    const body = el('tbody');
    for (const a of rr) {
      const row = el('tr'),
        th = el('th', aliases.get(a));
      th.title = name(a);
      th.scope = 'row';
      row.append(th);
      for (const b of cc) {
        const cell = el('td'),
          buses = data.rows.get(a)?.get(b);
        if (buses && (pinned == null || a === pinned || b === pinned)) {
          const caption = name(a) + ' → ' + name(b) + ' · ' + buses.length + ' 个信号';
          const btn = button(buses.length, () => selectPair(a, b), 'matrix-cell' + (from === a && to === b ?
            ' current' : ''));
          btn.title = caption;
          btn.setAttribute('aria-label', caption);
          btn.dataset.from = a;
          btn.dataset.to = b;
          cell.append(btn);
        } else {
          if (a === b) cell.className = 'matrix-self';
          cell.textContent = '·';
        }
        row.append(cell);
      }
      body.append(row);
    }
    grid.append(body);
    wrap.append(grid);
    parent.append(wrap);

    function selectPair(a, b) {
      change({
        from: a,
        to: b,
        sid: null,
        gate: null
      });
      document.querySelector('.matrix-pair')?.scrollIntoView?.({
        block: 'nearest'
      });
    }
    if (from == null || to == null) return;
    const buses = data.rows.get(from)?.get(to);
    if (!buses?.length) return;
    const panel = el('section', undefined, 'matrix-pair');
    parent.append(panel);
    panel.append(el('h3', name(from) + ' → ' + name(to)));
    const selectRow = bar(panel),
      sorted = [...buses].sort((a, b) => D.symbols[a.symbol].name.localeCompare(D.symbols[b.symbol].name,
        'zh-CN', {
          numeric: true
        }));
    const sid = sorted.some(b => b.symbol === opts.sid) ? opts.sid : sorted.length === 1 ? sorted[0].symbol :
      null;
    const choice = select(selectRow, [
        ['', '选择信号（' + sorted.length + '）'], ...sorted.map(b => [String(b.symbol), D.symbols[b.symbol].name])
      ],
      sid == null ? '' : String(sid), v => change({
        sid: v === '' ? null : Number(v),
        gate: null
      }));
    choice.setAttribute('aria-label', '两程序之间的信号');
    if (sid == null) return;
    const r = sorted.find(b => b.symbol === sid),
      s = D.symbols[sid];
    selectRow.append(button('在整机图中定位', () => go('plant', 's' + sid, {
        from,
        to,
        scope: mode
      })),
      button('完整交叉引用', () => go('symbols', sid)));
    const ends = el('div', undefined, 'matrix-ends');
    panel.append(ends);
    const left = el('section'),
      right = el('section');
    ends.append(left, right);

    function references(section, which, unit) {
      const omitted = new Set(r.binding_only?.[which] || []);
      const entries = s[which].filter(([net], i) => !omitted.has(i) && owner[net] === unit);
      paged(section, entries, (p, [net, id, pin]) => {
        const n = nodeMap.get(id)[1],
          st = Number(String(pin).match(/^ST (\d+)$/)?.[1] || 0);
        const matches = (n.assignments || []).filter(a => a.line === st && a.target.toLowerCase() === s
          .name.toLowerCase());
        const instruction = matches.length ? matches.map(a => a.target + ' := ' + a.value).join(' / ') : n
          .label;
        const b = button(instruction, () => go('nets', net, {
          focus: id,
          stLine: st
        }), 'endpoint');
        b.append(el('small', netName(D.nets[net]) + ' · XML L' + n.line + (st ? ' · ST ' + st : '')));
        p.append(b);
      }, 5);
      return entries.length;
    }
    left.append(el('h4', '写入 · ' + name(from)));
    references(left, 'writes', from);
    right.append(el('h4', '接收 · ' + name(to)));
    const gates = r.guards.filter(i => owner[D.sites[i].net] === to && (mode !== 'observed' || observations
      .has(i)));
    paged(right, gates, (p, i) => {
      const w = D.sites[i],
        observed = observations.get(i);
      const b = button(w.channel + '　' + w.start + ' → ' + w.end, () => {
        change({
          gate: i
        });
        document.querySelector('.matrix-rung')?.scrollIntoView?.({
          block: 'nearest'
        });
      }, 'endpoint');
      b.dataset.site = i;
      b.setAttribute('aria-pressed', String(opts.gate === i));
      b.append(el('small', netName(D.nets[w.net]) + (observed ? ' · 状态对实测 ' + number(observed) + ' 次' :
        ' · 仅源码')));
      p.append(b);
    }, 5);
    if (mode === 'all') references(right, 'reads', to);
    if (gates.includes(opts.gate)) {
      const w = D.sites[opts.gate],
        drawing = el('section', undefined, 'matrix-rung');
      panel.append(drawing);
      drawing.append(el('h3', w.channel + '　' + w.start + ' → ' + w.end));
      const count = observations.get(opts.gate);
      drawing.append(el('small', count ? 'Trace · 状态对实测 ' + number(count) + ' 次' : '仅源码', 'drawing-caption'));
      gateFigure(drawing, w, s.name);
      drawing.append(button('打开接收网络 · XML L' + (nodeMap.get(w.source_node)?.[1].line || D.nets[w.net].line),
        () => go('nets', w.net, {
          focus: w.source_node,
          stLine: w.st_line
        })));
    }
  }
  return {
    render,
    listing,
    matrix
  };
})();

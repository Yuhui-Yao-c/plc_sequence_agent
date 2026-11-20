/* One address space for tasks, owner programs, routines, source networks and physical pins. */
const Plant = (() => {
  const groupsOpen = new Set(),
    routinesOpen = new Set(),
    netsOpen = new Set(),
    plans = new Map();
  const predicatePlans = new Map();
  let catalog, byKey, bindings, groupOrder, selected, allLines = false,
    allLayer = 'all',
    zoom = 1,
    saved = null;
  const leaf = s => s.split('::').pop() || s;
  const groupOf = net => D.plant.routines[D.plant.net_routine[net]].group;
  const program = g => D.plant.groups[g].kind.toLowerCase() === 'program';
  const section = (group, name) => ({
    key: group + name,
    kind: group,
    label: name
  });

  function init() {
    if (catalog) return;
    const p = D.plant;
    bindings = new Map(p.port_bindings);
    groupOrder = p.groups.map((_, i) => i).sort((a, b) => Number(program(b)) - Number(program(a)) || p.groups[a]
      .scope.localeCompare(p.groups[b].scope, 'zh-CN'));
    const observedSites = new Set();
    for (const c of D.channels)
      for (const e of c.pairs)
        if (e.events?.length)
          for (const i of e.sites) observedSites.add(i);
    catalog = p.signal_buses.map(b => {
      const gates = b.guard_groups.some(r => b.writers.some(w => r !== w));
      return {
        ...section('signal', D.symbols[b.symbol].name),
        key: 's' + b.symbol,
        b,
        gates,
        observed: b.guards.some(i => observedSites.has(i)),
        peer: gates && b.guard_groups.some(r => program(r) && b.writers.some(w => w !== r && program(w)))
      };
    }).sort((a, b) => Number(b.peer) - Number(a.peer) || Number(b.observed) - Number(a.observed) || Number(b
        .gates) - Number(a.gates) ||
      (a.b.writers.length + a.b.readers.length) - (b.b.writers.length + b.b.readers.length) || a.b.symbol - b
      .b.symbol);
    const grouped = new Map();
    for (const [id, rid] of p.call_links) {
      const a = groupOf(D.calls[id].net),
        b = p.routines[rid].group,
        key = 'c' + a + '_' + b;
      if (!grouped.has(key)) grouped.set(key, {
        key,
        kind: 'call',
        label: leaf(p.groups[a].scope) + ' → ' + leaf(p.groups[b].scope),
        a,
        b,
        sites: []
      });
      grouped.get(key).sites.push([id, rid]);
    }
    catalog.push(...grouped.values());
    p.task_links.forEach(([t, rid, instance], i) => catalog.push({
      key: 't' + i,
      kind: 'task',
      label: D.tasks[t].name + ' → ' + leaf(p.routines[rid].scope),
      t,
      rid,
      instance
    }));
    byKey = new Map(catalog.map(v => [v.key, v]));
    selected = null;
  }
  const layer = (r, l) => l === 'all' || (l === 'peer' && r.peer) || (l === 'guards' && r.gates) || (l === 'signals' && r.kind ===
    'signal') || r.kind === l;

  function listing(filter, subset) {
    init();
    const q = filter.toLowerCase(),
      rows = [];
    if (!q) rows.push(['home', '整机总图']);
    if (subset === 'all' || subset === 'groups')
      for (const id of groupOrder) {
        const g = D.plant.groups[id];
        if (g.scope.toLowerCase().includes(q)) rows.push(['g' + id, leaf(g.scope) + ' · ' + g.kind]);
      }
    if (subset !== 'groups')
      for (const r of catalog)
        if (layer(r, subset) && r.label.toLowerCase().includes(q))
          rows.push([r.key, ({
            signal: r.gates ? '门控信号' : '共享读写',
            call: '调用',
            task: '任务调度'
          } [r.kind]) + ' · ' + r.label]);
    return rows;
  }

  function planFor(net) {
    if (plans.has(net.id)) return plans.get(net.id);
    const ids = new Set(net.nodes.map(n => n.id));
    for (const w of net.wires) {
      ids.add(w[0]);
      ids.add(w[2]);
    }
    const plan = LD.layout([...ids].map(id => nodeMap.get(id)[1]), net.wires, net.language);
    plans.set(net.id, plan);
    return plan;
  }

  function occurrences(r) {
    if (r.kind === 'task') return [{
      t: r.t,
      k: 'from'
    }, {
      rid: r.rid,
      k: 'to'
    }];
    if (r.kind === 'call') return r.sites.flatMap(([i, rid]) => {
      const c = D.calls[i];
      return [{
        net: c.net,
        node: c.node,
        k: 'from',
        pin: c.type,
        st: c.st_line,
        call: i
      }, {
        rid,
        k: 'to'
      }];
    });
    const s = D.symbols[r.b.symbol];
    const refs = (role, k) => {
      const omitted = new Set(r.b.binding_only?.[role] || []);
      return s[role].flatMap(([net, node, pin], i) => omitted.has(i) ? [] : [{
        net,
        node,
        pin,
        k,
        st: Number(String(pin).match(/^ST (\d+)$/)?.[1] || 0)
      }]);
    };
    return [...refs('writes', 'from'), ...refs('reads', 'to'), ...r.b.guards.map(i => {
      const s = D.sites[i];
      return {
        net: s.net,
        node: s.source_node,
        st: s.st_line,
        site: i,
        k: 'guard'
      };
    })];
  }

  function evidence(ref, r) {
    const p = D.plant;
    if (ref.t != null) return D.tasks[ref.t].name + ' · XML L' + D.tasks[ref.t].source_line;
    if (ref.rid != null) return p.routines[ref.rid].scope;
    const n = nodeMap.get(ref.node)?.[1],
      net = D.nets[ref.net];
    const station = leaf(p.groups[groupOf(net.id)].scope),
      scope = leaf(net.scope);
    let s = station + (station === scope ? '' : ' / ' + scope) + ' · XML L' + (n?.line || net.line);
    if (ref.st) s += ' / ST ' + ref.st;
    if (ref.site != null) {
      const w = D.sites[ref.site];
      return s + ' · ' + w.channel + ' ' + w.start + ' → ' + w.end + ' · ' + w.guard;
    }
    if (ref.call != null) return s + ' · ' + (D.calls[ref.call].instance || n?.instanceName || D.calls[ref.call]
      .type) + ' → ' + D.calls[ref.call].type;
    if (ref.st && r.kind === 'signal') {
      const a = (n?.assignments || []).find(a => a.line === ref.st && (ref.k !== 'from' || a.target
        .toLowerCase() === r.label.toLowerCase()));
      if (a) return s + ' · ' + a.target + ' := ' + a.value + ' · ' + a.guard;
    }
    return s + ' · ' + (n?.type || '') + ' ' + (n?.label || '') + (ref.pin ? ' / ' + ref.pin : '') +
      (n?.storage && n.storage !== 'none' ? ' · ' + n.storage : '') + (n?.latch ? ' · ' + n.latch : '');
  }

  function scene(parent, key, opts = {}) {
    init();
    const p = D.plant,
      focusGroup = /^g\d+$/.test(key) ? Number(key.slice(1)) : null;
    let pairFocus = opts.from != null && opts.to != null;
    function scopedRefs(r) {
      const refs = occurrences(r);
      if (!pairFocus || r.key !== key || r.kind !== 'signal') return refs;
      return refs.filter(ref => groupOf(ref.net) === (ref.k === 'from' ? opts.from : opts.to) &&
        (opts.scope === 'all' || ref.k !== 'to') &&
        (opts.scope !== 'observed' || ref.k !== 'guard' || observed.has(ref.site)));
    }
    const observed = opts.scope === 'observed' ? new Set(D.channels.flatMap(c => c.pairs.filter(e => e.events?.length).flatMap(e => e.sites))) : null;
    if (byKey.has(key)) {
      selected = key;
      allLines = false;
    }
    if (key === 'home') { selected = null; allLines = false; }
    if (focusGroup != null) groupsOpen.add(focusGroup);
    parent.append(el('h2', '整机图纸'));
    LD.legend(parent);
    para(parent, p.groups.length + ' 个程序或定义 · ' + number(D.nets.length) + ' 个网络 · ' + number(p.coverage
        .cross_signals) + ' 个跨单元读写信号，其中 ' + number(p.coverage.cross_guard_signals) + ' 个进入其他单元的步序门控。',
      'muted');
    const controls = bar(parent),
      frame = el('div', undefined, 'plant-frame'),
      world = el('div', undefined, 'plant-world');
    const screen = sv('svg', {
      xmlns: svgNS,
      class: 'plant-screen',
      role: 'group',
      'aria-label': '可展开的整机逻辑图'
    });
    frame.setAttribute('tabindex', '0');
    frame.append(world);
    world.append(screen);
    const lineInfo = el('p', undefined, 'plant-legend'),
      selection = el('section', undefined, 'plant-inspector');
    const overview = el('div', undefined, 'plant-route');
    parent.append(overview, lineInfo, frame, selection);
    let routeIds = [],
      rows = [],
      anchors = new Map(),
      tracks = [],
      left = 0,
      width = 0,
      height = 0,
      request = 0;
    const sizes = () => [frame.clientWidth || 1000, frame.clientHeight || 520];
    const scopeRows = (id, label, depth, kind, target) => ({
      id,
      label,
      depth,
      kind,
      target
    });

    function rebuild() {
      rows = [];
      anchors = new Map();
      height = 12;
      const add = (s, h = 34) => {
        s.y = height;
        s.h = h;
        height += h;
        anchors.set(s.id, s);
        rows.push(s);
      };
      let maxWidth = 460;
      for (const [i, t] of D.tasks.entries()) add(scopeRows('t' + i, t.name, 0, 'task', i), 29);
      for (const g of groupOrder) {
        const owner = p.groups[g];
        add(scopeRows('g' + g, leaf(owner.scope), 0, 'group', g), 43);
        if (!groupsOpen.has(g)) continue;
        for (const rid of owner.routines) {
          const r = p.routines[rid];
          add(scopeRows('r' + rid, leaf(r.scope), 1, 'routine', rid));
          if (!routinesOpen.has(rid)) continue;
          for (const id of r.nets) {
            const net = D.nets[id];
            add(scopeRows('n' + id, net.language + ' · ' + (net.label || 'L' + net.line) + (net.comment ? '　' +
              oneLine(net.comment) : ''), 2, 'net', id));
            if (!netsOpen.has(id)) continue;
            const plan = planFor(net);
            add({
              ...scopeRows('d' + id, '', 3, 'drawing', id),
              plan
            }, Math.max(38, plan.height + 12));
            maxWidth = Math.max(maxWidth, plan.width + 112);
            for (const n of net.nodes) {
              for (const [idx, a] of(n.assignments || []).entries()) {
                if (!predicatePlans.has(a.guard_id)) predicatePlans.set(a.guard_id, LD.guardPlan(p.st_guards[a
                  .guard_id]));
                const shape = predicatePlans.get(a.guard_id),
                  id = 'a' + n.id + '/' + idx;
                const r = {
                  ...scopeRows(id, 'ST ' + a.line, 3, 'statement', net.id),
                  node: n.id,
                  a,
                  shape
                };
                add(r, shape.h + 64);
                maxWidth = Math.max(maxWidth, LD.guardWidth(shape,a.target + ' := ' + a.value) + 110);
                const mark = 'line' + n.id + '/' + a.line;
                anchors.set(mark, anchors.has(mark) ? null : r);
              }
            }
          }
        }
      }
      const fromSymbol = sid => byKey.get('s' + sid) || {
        kind: 'signal',
        key: 's' + sid,
        label: D.symbols[sid].name,
        b: {
          symbol: sid,
          guards: []
        }
      };
      const shown = routeIds.length ? routeIds.map(fromSymbol) : allLines ? catalog.filter(r => layer(r,
        allLayer)) : catalog.filter(r => r.key === selected);
      tracks = shown.map((r, i) => ({
        r,
        lane: i,
        ends: null
      }));
      if (allLines && selected && !routeIds.length) {
        const i = tracks.findIndex(t => t.r.key === selected);
        if (i > 0) {
          [tracks[0], tracks[i]] = [tracks[i], tracks[0]];
          tracks[0].lane = 0;
          tracks[i].lane = i;
        }
      }
      left = 64 + tracks.length * 30;
      width = left + maxWidth + 55;
      world.style.width = Math.ceil(width * zoom) + 'px';
      world.style.height = Math.min(8000000, Math.ceil((height + 10) * zoom)) + 'px';
      screen.setAttribute('data-expanded-networks', netsOpen.size);
      screen.setAttribute('data-source-nodes', D.summary.nodes);
      screen.setAttribute('data-relations', tracks.length);
      lineInfo.textContent = (routeIds.length ? '指令依赖　' + routeIds.map(sid => D.symbols[sid].name).join(' / ') :
        allLines ? '全部匹配关系（' + number(tracks.length) + ' 条通道，向左横移查看）' : '当前关系　' + (byKey.get(selected)
          ?.label || '未选中')) + '　　蓝色 → 写入 / 发起　橙色 → 读 / 门控 / 接收';
    }

    function location(ref) {
      const rid = ref.rid ?? p.net_routine[ref.net],
        gid = rid != null ? p.routines[rid].group : null;
      const treeX = left + 17;
      if (ref.t != null) {
        const a = anchors.get('t' + ref.t);
        return {
          x: treeX,
          y: a.y + a.h / 2
        };
      }
      const d = ref.net != null ? anchors.get('d' + ref.net) : null;
      const statement = ref.st && anchors.get('line' + ref.node + '/' + ref.st);
      if (statement) return {
        x: left + 82,
        y: statement.y + 17,
        portX: left + 61,
        key: 'ST ' + ref.st
      };
      const point = d?.plan.points.get(ref.node);
      if (point) return {
        x: left + 80 + point.x,
        y: d.y + point.y + 5,
        portX: left + 61,
        key: String(point.n.key ?? point.n.id)
      };
      const a = (ref.net != null && anchors.get('n' + ref.net)) || anchors.get('r' + rid) || anchors.get('g' +
        gid);
      return {
        x: treeX + 24 * a.depth,
        y: a.y + a.h / 2
      };
    }

    function endpoints(track) {
      if (track.ends) return track.ends;
      const unique = new Map();
      for (const ref of scopedRefs(track.r)) {
        const pt = location(ref),
          k = pt.x + ',' + pt.y + ',' + ref.k,
          e = unique.get(k);
        if (e) e.count++;
        else unique.set(k, {
          ...pt,
          k: ref.k,
          count: 1,
          ref
        });
      }
      track.ends = [...unique.values()].sort((a, b) => a.y - b.y);
      return track.ends;
    }

    function showRef(ref) {
      if (ref.t != null) return go('project', 't' + ref.t);
      if (ref.net == null) {
        const r = p.routines[ref.rid];
        groupsOpen.add(r.group);
        routinesOpen.add(ref.rid);
        rebuild();
        return jump('r' + ref.rid);
      }
      go('nets', ref.net, {
        focus: ref.node,
        stLine: ref.st
      });
    }

    function locate(ref) {
      const rid = ref.rid ?? p.net_routine[ref.net];
      if (rid != null) {
        groupsOpen.add(p.routines[rid].group);
        routinesOpen.add(rid);
      }
      if (ref.net != null) netsOpen.add(ref.net);
      rebuild();
      const pt = location(ref);
      frame.scrollLeft = Math.max(0, pt.x * zoom - sizes()[0] * 0.52);
      setScrollY(pt.y * zoom - sizes()[1] * 0.42);
      paint();
    }

    function describe() {
      const r = byKey.get(selected);
      selection.replaceChildren();
      overview.replaceChildren();
      if (!r) {
        if (!catalog.length) para(selection,'工程中未定位到跨程序信号、调用或任务挂载。');
        return;
      }
      title(selection, r.label);
      const refs = scopedRefs(r),
        actions = terms(selection);
      const source = refs.find(a => a.k === 'from');
      const firstGroup = source?.net != null ? groupOf(source.net) : null;
      const remote = refs.find(a => a.k === 'guard' && groupOf(a.net) !== firstGroup) ||
        refs.find(a => a.k === 'to' && (a.net == null || groupOf(a.net) !== firstGroup));

      function participant(ref) {
        if (ref.t != null) return D.tasks[ref.t].name;
        const gid = ref.net != null ? groupOf(ref.net) : p.routines[ref.rid].group;
        return leaf(p.groups[gid].scope);
      }
      if (source && remote) {
        overview.append(button(participant(source), () => locate(source)), el('span', '→'),
          el('strong', r.kind === 'signal' ? r.label : r.kind === 'call' ? '调用' : '任务挂载'), el('span', '→'),
          button(participant(remote), () => locate(remote)));
        const others = r.kind === 'signal' && (r.b.writers.length > 1 || new Set([...r.b.readers, ...r.b
          .guard_groups
        ]).size > 1);
        if (others && !pairFocus) overview.append(el('small', '示例两端 · 全部位置见下方'));
      }
      if (source) actions.append(button('定位发起端', () => locate(source)));
      if (remote) actions.append(button('定位接收端', () => locate(remote)));
      if (r.kind === 'signal') actions.append(button('查看完整信号索引', () => go('symbols', r.b.symbol)));
      para(selection, ({
        signal: '连线由同一声明、同一固定地址的读写形成。多个写入方均保留。步序门控可展开原始条件。',
        call: '每个调用点保留自身例程、实例和可解析的实参与形参。被调功能块按源码定义展示。',
        task: '调度关系来自任务挂载的程序实例。'
      } [r.kind]), 'muted');
      if (r.kind === 'signal') Lineage.render(selection, r.b.symbol, ids => {
        routeIds = ids.slice();
        allLines = false;
        rebuild();
        frame.scrollLeft = Math.max(0, (left - 180) * zoom);
        paint();
      }, locate);
      for (const [k, caption] of [
          ['from', r.kind === 'signal' ? '写入位置' : '发起位置'],
          ['guard', '作为步序门控'],
          ['to', r.kind === 'signal' ? '读取位置' : '目标定义']
        ]) {
        const list = refs.filter(a => a.k === k);
        if (!list.length) continue;
        const d = box(selection, caption + ' · ' + number(list.length), k === 'guard' && list.length <= 3);
        paged(d, list, (target, ref) => {
          const line = el('div', undefined, 'proof-row');
          target.append(line);
          line.append(button('图中定位', () => locate(ref)), button(evidence(ref, r), () => showRef(ref)));
          if (ref.call != null && bindings.has(ref.call)) {
            const b = box(line, '该实例的实参与形参 · ' + bindings.get(ref.call).length);
            for (const [formal, dir, sid] of bindings.get(ref.call)) b.append(button(formal + ' ' + ({
              read: '←',
              write: '→',
              inout: '↔'
            } [dir]) + ' ' + D.symbols[sid].name, () => go('symbols', sid)));
          }
        }, 16);
      }
    }

    function scrollRatio() {
      const h = sizes()[1],
        full = Math.ceil((height + 10) * zoom);
      return full <= 8000000 ? 1 : (full - h) / (8000000 - h);
    }

    function setScrollY(pixel) {
      frame.scrollTop = Math.max(0, pixel) / scrollRatio();
    }

    function jump(id) {
      const row = anchors.get(id);
      if (!row) return;
      frame.scrollLeft = Math.max(0, (left - 120) * zoom);
      setScrollY(row.y * zoom - sizes()[1] * 0.25);
      paint();
    }

    function toggle(row) {
      const sets = {
          group: groupsOpen,
          routine: routinesOpen,
          net: netsOpen
        },
        set = sets[row.kind];
      if (!set) return go('project', 't' + row.target);
      const y = row.y * zoom - (frame.scrollTop || 0) * scrollRatio();
      if (set.has(row.target)) set.delete(row.target);
      else set.add(row.target);
      rebuild();
      setScrollY(anchors.get(row.id).y * zoom - y);
      paint();
    }

    function keyAct(element, fn) {
      element.onclick = fn;
      element.onkeydown = e => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          fn();
        }
      };
    }

    function paint() {
      const [pw, ph] = sizes(), ox = (frame.scrollLeft || 0) / zoom, oy = (frame.scrollTop || 0) *
      scrollRatio() / zoom;
      const right = ox + pw / zoom,
        bottom = oy + ph / zoom;
      screen.style.left = Math.round(ox * zoom) + 'px';
      screen.style.top = Math.round(frame.scrollTop || 0) + 'px';
      screen.setAttribute('width', pw);
      screen.setAttribute('height', ph);
      screen.setAttribute('viewBox', [ox, oy, pw / zoom, ph / zoom].join(' '));
      screen.replaceChildren();
      const layer = sv('g');
      screen.append(layer);
      // Binary search visible rows; all expanded networks share this one scroll space.
      let lo = 0,
        hi = rows.length;
      while (lo < hi) {
        const m = (lo + hi) >> 1;
        if (rows[m].y + rows[m].h < oy) lo = m + 1;
        else hi = m;
      }
      if (right >= left)
        for (let i = lo; i < rows.length && rows[i].y <= bottom; i++) {
          const a = rows[i],
            x = left + 24 * a.depth;
          if (a.kind === 'statement') {
            const label = sv('text', {
              x: left + 90,
              y: a.y + 18,
              fill: '#4d656b',
              'font-size': 13
            }, a.label);
            const rung = LD.guard(p.st_guards[a.a.guard_id], a.a.target + ' := ' + a.a.value, a.shape);
            rung.setAttribute('x', left + 80);
            rung.setAttribute('y', a.y + 23);
            const item = sv('g', {
              'data-st-assignment': a.id,
              role: 'button',
              tabindex: 0
            });
            keyAct(item, () => go('nets', a.target, {
              focus: a.node,
              stLine: a.a.line
            }));
            item.append(label, rung);
            layer.append(item);
            continue;
          }
          if (a.kind === 'drawing') {
            const net = D.nets[a.target],
              dx = left + 80,
              dy = a.y + 5;
            const svg = LD.draw(a.plan, id => go('nets', nodeMap.get(id)[0].id, {
                focus: id
              }), null,
              [ox - dx, oy - dy, right - dx, bottom - dy]);
            svg.setAttribute('x', dx);
            svg.setAttribute('y', dy);
            svg.setAttribute('data-net-drawing', net.id);
            layer.append(svg);
            continue;
          }
          const expanded = ({
            group: groupsOpen,
            routine: routinesOpen,
            net: netsOpen
          } [a.kind])?.has(a.target);
          const mark = a.kind === 'task' ? '◷' : expanded ? '▾' : '▸';
          const g = sv('g', {
            'data-row': a.id,
            tabindex: 0,
            role: 'button',
            'aria-expanded': String(!!expanded)
          });
          g.append(sv('rect', {
            x,
            y: a.y + 2,
            width: Math.max(345, Math.min(1100, right - x - 10)),
            height: a.h - 4,
            rx: 4,
            fill: a.kind === 'group' ? '#e2efed' : a.kind === 'net' ? '#fafbf9' : '#f0f4f5',
            stroke: '#d6e0df'
          }));
          g.append(sv('text', {
            x: x + 12,
            y: a.y + a.h / 2 + 5,
            fill: '#183f47',
            'font-size': 14
          }, mark + '  ' + a.label));
          g.append(sv('title', {}, a.kind === 'group' ? p.groups[a.target].scope : a.label));
          keyAct(g, () => toggle(a));
          layer.append(g);
        }
      // Each signal has its own bus. Only horizontally visible buses are drawn.
      // Endpoint wires of offscreen buses are represented by the lane index at overview scale.
      const low = Math.max(0, Math.floor((left - 45 - right) / 30)),
        high = Math.min(tracks.length - 1, Math.ceil((left - 45 - ox) / 30));
      const visibleTracks = new Set();
      for (let i = high; i >= low; i--) visibleTracks.add(i);
      if (routeIds.length) tracks.forEach((_, i) => visibleTracks.add(i));
      else if (tracks[0]?.r.key === selected) visibleTracks.add(0);
      for (const i of [...visibleTracks].sort((a, b) => b - a)) {
        const t = tracks[i],
          bx = left - 45 - i * 30,
          ends = endpoints(t);
        if (!ends.length || ends[0].y > bottom || ends.at(-1).y < oy) continue;
        const colored = t.r.key === selected,
          plain = colored ? '#3e6473' : '#a1b2b7';
        const stem = sv('path', {
          d: 'M ' + bx + ' ' + ends[0].y + ' V ' + ends.at(-1).y,
          fill: 'none',
          stroke: plain,
          'stroke-width': colored ? 3 : 2,
          'data-bus': t.r.key
        });
        stem.append(sv('title', {}, t.r.label));
        stem.onclick = () => choose(t.r.key, false);
        screen.append(stem);
        for (const e of ends) {
          if (e.y < oy - 3 || e.y > bottom + 3) continue;
          const sending = e.k === 'from',
            color = sending ? '#26738b' : e.k === 'guard' ? '#ae501e' : '#b37841';
          const port = e.portX ?? e.x,
            end = sending ? bx : port,
            sign = sending ? -1 : 1;
          const edge = sv('g', {
            'data-end': e.k,
            role: 'button',
            tabindex: 0
          });
          const y = e.y + (sending ? -4 : 4);
          edge.append(sv('path', {
            d: 'M ' + bx + ' ' + y + ' H ' + port,
            fill: 'none',
            stroke: color,
            'stroke-opacity': colored ? 0.95 : 0.52,
            'stroke-width': colored ? 1.7 : 1
          }));
          edge.append(sv('path', {
            d: 'M ' + end + ' ' + y + ' l ' + (-sign * 6) + ' -3 v 6 Z',
            fill: color
          }));
          edge.append(sv('title', {}, t.r.label + ' · ' + e.count + ' 处 · ' + evidence(e.ref, t.r)));
          if (e.portX != null && colored) {
            const badge = sv('text', {
              x: port + 2,
              y: y - 6,
              fill: color,
              'font-size': 10
            }, '#' + e.key);
            edge.append(badge);
            if (e.x + 84 >= ox && e.x - 84 <= right) {
              edge.append(sv('rect', {
                x: e.x - 82,
                y: e.y - 25,
                width: 164,
                height: 50,
                rx: 4,
                fill: 'none',
                stroke: color,
                'stroke-dasharray': '3 4',
                'pointer-events': 'none'
              }));
              edge.append(sv('text', {
                x: e.x + 65,
                y: e.y + 32,
                fill: color,
                'font-size': 10
              }, '#' + e.key));
            }
          }
          keyAct(edge, () => {
            choose(t.r.key, false);
          });
          screen.append(edge);
        }
      }
      saved = {
        x: frame.scrollLeft || 0,
        y: frame.scrollTop || 0
      };
    }

    function choose(id, single = true) {
      if (!byKey.has(id)) {
        if (id.startsWith('s')) go('symbols', Number(id.slice(1)));
        return;
      }
      routeIds = [];
      pairFocus = false;
      selected = id;
      if (single) allLines = false;
      rebuild();
      describe();
      frame.scrollLeft = Math.max(0, (left - 120) * zoom);
      paint();
    }
    const cycle = d => {
      const options = catalog.filter(r => layer(r, allLayer));
      if (options.length) choose(options[(options.findIndex(r => r.key === selected) + d + options.length) %
        options.length].key);
    };
    controls.append(button('全部展开', () => {
      routeIds = [];
      allLines = true;
      pairFocus = false;
      p.groups.forEach((_, i) => groupsOpen.add(i));
      p.routines.forEach((_, i) => routinesOpen.add(i));
      D.nets.forEach(n => netsOpen.add(n.id));
      rebuild();
      jump('g' + groupOrder[0]);
    }), button('全部折叠', () => {
      groupsOpen.clear();
      routinesOpen.clear();
      netsOpen.clear();
      rebuild();
      jump('g' + groupOrder[0]);
    }));
    const layers = [
      ['all', '全部关系'],
      ['peer', '程序间步序门控'], ['guards', '跨单元门控'],
      ['signals', '共享读写'],
      ['call', '源码调用'],
      ['task', '任务挂载']
    ];
    select(controls, layers, allLayer, l => {
      routeIds = [];
      allLayer = l;
      pairFocus = false;
      const first = catalog.find(r => layer(r, l));
      if (first) selected = first.key;
      else selected = null;
      rebuild();
      describe();
      frame.scrollLeft = Math.max(0, (left - 120) * zoom);
      paint();
    });
    controls.append(button('上一关系', () => cycle(-1)), button('下一关系', () => cycle(1)), button('全部连线', () => {
      routeIds = [];
      allLines = true;
      pairFocus = false;
      rebuild();
      frame.scrollLeft = Math.max(0, (left - 260) * zoom);
      paint();
    }), button('聚焦当前关系', () => {
      if (selected) choose(selected);
    }));
    for (const [caption, factor] of [
        ['−', 1 / 1.25],
        ['+', 1.25]
      ]) controls.append(button(caption, () => {
      const [pw, ph] = sizes(), x = ((frame.scrollLeft || 0) + pw / 2) / zoom, y = ((frame.scrollTop ||
        0) * scrollRatio() + ph / 2) / zoom;
      zoom = Math.max(0.32, Math.min(2.45, zoom * factor));
      rebuild();
      frame.scrollLeft = Math.max(0, x * zoom - pw / 2);
      setScrollY(y * zoom - ph / 2);
      paint();
    }));
    frame.onscroll = () => {
      if (!request) request = requestAnimationFrame(() => {
        request = 0;
        paint();
      });
    };
    const last = saved;
    rebuild();
    describe();
    if (focusGroup != null) jump('g' + focusGroup);
    else if ((pairFocus || !last) && selected && byKey.get(selected)?.kind === 'signal') {
      const refs = scopedRefs(byKey.get(selected));
      const first = refs.find(a => a.k === 'guard' && a.node != null && D.nets[a.net].language === 'LD');
      if (first) locate(first);
      else if (refs.length) jump('g' + groupOf(refs[0].net));
      else paint();
    } else if (byKey.has(key)) {
      const first = scopedRefs(byKey.get(key))[0];
      if (first?.net != null) jump('g' + groupOf(first.net));
      else paint();
    } else {
      frame.scrollLeft = last?.x ?? Math.max(0, (left - 120) * zoom);
      frame.scrollTop = last?.y ?? 0;
      paint();
    }
    if (typeof ResizeObserver === 'function') {
      const obs = new ResizeObserver(() => {
        if (frame.isConnected) paint();
        else obs.disconnect();
      });
      obs.observe(frame);
    }
  }
  return {
    scene,
    listing
  };
})();

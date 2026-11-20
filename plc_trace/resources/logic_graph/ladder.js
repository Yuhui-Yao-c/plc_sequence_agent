/* Offline LD drawing. Layout changes coordinates only; edges always carry source endpoint IDs. */
const LD = (() => {
  const ink = '#21434d',
    flow = '#116c68',
    data = '#607988';
  const flag = x => /^(true|1)$/i.test(String(x));
  const path = (d, color = ink, extra = {}) => sv('path', {
    d,
    fill: 'none',
    stroke: color,
    'stroke-width': 2,
    ...extra
  });
  const textAt = (x, y, s, extra = {}) => sv('text', {
    x,
    y,
    fill: ink,
    'font-size': 12,
    ...extra
  }, s);
  const short = (s, length = 26) => String(s).length <= length ? String(s) : String(s).slice(0, length - 1) +
    '…';

  function kind(n, language) {
    const t = n.type.toLowerCase();
    if (t === 'contact' || (language === 'SMC2' && t === 'ld')) return 'contact';
    if (t === 'coil' || (language === 'SMC2' && t === 'st')) return 'coil';
    if (t.endsWith('powerrail')) return 'rail';
    if (['datasource', 'datasink', 'invariable', 'outvariable', 'inoutvariable'].includes(t)) return 'operand';
    if (['connector', 'continuation', 'branchterminator', 'hl', 'branch'].includes(t)) return 'connector';
    if (t === 'st' || t === 'ist' || t === 'inlinest') return 'st';
    return 'block';
  }

  function marks(n) {
    const s = String(n.storage || n.latch || '').toLowerCase(),
      e = String(n.edge || '').toLowerCase();
    return {
      neg: flag(n.negated) || flag(n.Not),
      store: s === 'set' || flag(n.Set) ? 'S' : s === 'reset' || flag(n.Rst) ? 'R' : '',
      edge: e === 'rising' || flag(n.Up) ? 'P' : e === 'falling' || flag(n.Dwn) ? 'N' : ''
    };
  }

  function symbol(g, k, n, h, pins) {
    const m = marks(n),
      centered = {
        'text-anchor': 'middle'
      };
    if (k === 'contact' || k === 'coil') {
      g.append(path('M -65 0 H -10 M 10 0 H 65', flow));
      if (k === 'contact') g.append(path('M -10 -13 V 13 M 10 -13 V 13', flow));
      else g.append(path('M -5 -14 Q -18 0 -5 14 M 5 -14 Q 18 0 5 14', flow));
      if (m.neg) g.append(path('M -15 15 L 15 -15', flow));
      if (m.store || m.edge) g.append(textAt(0, 4, m.store || m.edge, centered));
      if (m.store && m.edge) g.append(textAt(23, -7, m.edge, centered));
      const name = String(n.label);
      if (name.length <= 26) g.append(textAt(0, -21, name, centered));
      else {
        const at = name.slice(4, 26).lastIndexOf('.');
        const cut = at >= 0 ? at + 4 : 25;
        g.append(textAt(0, -36, name.slice(0, cut), centered), textAt(0, -21, short(name.slice(cut), 26),
          centered));
      }
    } else if (k === 'rail') {
      g.append(path('M 0 -22 V 22', flow, {
        'stroke-width': 5
      }));
      g.append(textAt(0, -29, n.type.toLowerCase().startsWith('left') ? '左电源轨' : '右电源轨', centered));
    } else if (k === 'connector') {
      g.append(path('M -32 0 H 32', flow));
      g.append(sv('circle', {
        cx: 0,
        cy: 0,
        r: 4,
        fill: flow
      }));
      g.append(textAt(0, -12, short(n.label), centered));
    } else if (k === 'operand') {
      g.append(textAt(0, -7, short(n.label), centered), path('M -65 0 H 65', data, {
        'stroke-width': 1
      }));
    } else {
      const top = -h / 2;
      g.append(sv('rect', {
        x: -76,
        y: top,
        width: 152,
        height: h,
        rx: 2,
        stroke: ink,
        fill: '#f5faf9'
      }));
      g.append(textAt(0, top + 16, short(n.label, 22), {
        ...centered,
        'font-weight': 700
      }));
      for (const [side, values] of Object.entries(pins)) values.forEach((v, i) => {
        const x = side === 'left' ? -76 : 76,
          y = top + 34 + i * 18;
        const mod = (n.pin_modifiers || []).find(p => p[0] === v)?.[1] || {},
          m = marks(mod);
        const actuals = (n.operands || []).filter(p => p[2] === v && (p[0] === 'inout' || (side === 'left' ?
          p[0] === 'read' : p[0] === 'write'))).map(p => p[1]);
        const name = (m.neg ? '¬' : '') + (m.edge || '') + (m.store || '') + (v || '·');
        const full = [name, ...actuals].join(' · ');
        const label = textAt(x + (x < 0 ? 5 : -5), y + 3, short(full, 13), {
          'text-anchor': x < 0 ? 'start' : 'end',
          'font-size': 10
        });
        label.append(sv('title', {}, full));
        g.append(label);
      });
    }
  }

  function layoutCircuit(items, wires, language) {
    const layers = [],
      ranks = new Map(),
      pending = new Map(),
      outgoing = new Map(),
      before = new Map(),
      points = new Map();
    for (const n of items) {
      pending.set(n.id, 0);
      ranks.set(n.id, 0);
      outgoing.set(n.id, []);
      before.set(n.id, []);
      points.set(n.id, {
        n,
        k: kind(n, language),
        pins: {
          left: [],
          right: []
        }
      });
    }
    const input = w => w[3] || (w[5] ? '[#' + w[5] + ']' : '');

    function register(id, side, name) {
      const names = points.get(id).pins[side];
      if (!names.includes(name)) names.push(name);
    }
    for (const w of wires) {
      if (!points.has(w[0]) || !points.has(w[2])) continue;
      register(w[0], 'right', w[1]);
      register(w[2], 'left', input(w));
      outgoing.get(w[0]).push(w[2]);
      before.get(w[2]).push(w[0]);
      pending.set(w[2], pending.get(w[2]) + 1);
    }
    // Lay out feedback components as well; a backwards wire uses an explicit return route.
    const queue = items.filter(n => !pending.get(n.id)).map(n => n.id);
    let at = 0,
      next = 0;
    const done = new Set();
    while (done.size < items.length) {
      if (at === queue.length) {
        while (next < items.length && done.has(items[next].id)) next++;
        queue.push(items[next].id);
      }
      const id = queue[at++];
      if (done.has(id)) continue;
      done.add(id);
      const depth = ranks.get(id);
      (layers[depth] ||= []).push(id);
      for (const dest of outgoing.get(id))
        if (!done.has(dest)) {
          ranks.set(dest, Math.max(depth + 1, ranks.get(dest)));
          pending.set(dest, pending.get(dest) - 1);
          if (!pending.get(dest)) queue.push(dest);
        }
    }
    let height = 90;
    layers.forEach((ids, level) => {
      const order = id => {
        const parents = before.get(id).map(a => points.get(a).y).filter(Number.isFinite);
        return parents.length ? parents.reduce((a, b) => a + b, 0) / parents.length : 0;
      };
      ids.sort((a, b) => (points.get(a).k === 'operand') - (points.get(b).k === 'operand') || order(a) -
        order(b) || a - b);
      let bottom = 12;
      for (const id of ids) {
        const v = points.get(id);
        for (const [role, , pin] of v.n.operands || [])
          if (pin !== undefined) {
            if (role !== 'write') register(id, 'left', pin);
            if (role !== 'read') register(id, 'right', pin);
          }
        for (const side of Object.values(v.pins)) side.sort((a, b) => Number(!/^_?ENO?$/i.test(a)) - Number(
          !/^_?ENO?$/i.test(b)));
        v.h = ['block', 'st'].includes(v.k) ? 32 + 18 * Math.max(1, v.pins.left.length, v.pins.right
          .length) : 44;
        v.x = 110 + level * 236;
        v.y = Math.max(bottom + v.h / 2 + 22, order(id));
        bottom = v.y + v.h / 2 + 18;
        height = Math.max(height, bottom + 14);
      }
    });
    // Distinct electrical nets must never share a vertical routing trunk merely
    // because two inputs happen to occupy the same column.
    const inKey = w => w[2] + ':' + input(w),
      outKey = w => w[0] + ':' + w[1];
    const indegree = new Map();
    for (const w of wires) indegree.set(inKey(w), (indegree.get(inKey(w)) || 0) + 1);
    const trunk = w => indegree.get(inKey(w)) > 1 ? 'in:' + inKey(w) : 'out:' + outKey(w);
    const lanes = layers.map(() => new Map()),
      xs = [110];
    for (const w of wires) {
      const right = ranks.get(w[2]);
      if (right <= ranks.get(w[0]) || right == null) continue;
      const name = trunk(w),
        group = lanes[right];
      if (!group.has(name)) group.set(name, group.size);
    }
    for (let i = 1; i < layers.length; i++) xs.push(xs[i - 1] + Math.max(236, 190 + 9 * lanes[i].size));
    for (const [id, v] of points) v.x = xs[ranks.get(id)];
    const width = Math.max(220, (xs.at(-1) || 110) + 110);
    const terminal = (v, side, name) => {
      const d = side === 'left' ? -1 : 1;
      if (['block', 'st'].includes(v.k)) return [v.x + d * 76, v.y - v.h / 2 + 34 + 18 * Math.max(0, v.pins[
        side].indexOf(name))];
      return [v.x + d * ({
        rail: 0,
        connector: 32
      } [v.k] ?? 65), v.y];
    };
    const branches = new Map(),
      wiresOut = [];
    let feedback = 0;
    for (const w of wires) {
      const a = points.get(w[0]),
        b = points.get(w[2]);
      if (!a || !b) continue;
      const [x, y] = terminal(a, 'right', w[1]), [u, v] = terminal(b, 'left', input(w));
      let route;
      if (u > x) {
        const rank = ranks.get(w[2]),
          name = trunk(w);
        const bend = xs[rank] - 88 - 9 * lanes[rank].get(name);
        route = 'M ' + x + ' ' + y + ' H ' + bend + ' V ' + v + ' H ' + u;
        const key = rank + ':' + name;
        if (!branches.has(key)) branches.set(key, {
          x: bend,
          left: new Set(),
          right: new Set(),
          role: w[4]
        });
        branches.get(key).left.add(y);
        branches.get(key).right.add(v);
      } else {
        const via = height + 12 + feedback++ * 12;
        route = 'M ' + x + ' ' + y + ' h 9 V ' + via + ' H ' + (u - 9) + ' V ' + v + ' H ' + u;
      }
      wiresOut.push({
        w,
        route,
        bounds: [Math.min(x, u) - 10, Math.min(y, v), Math.max(x, u) + 10, u > x ? Math.max(y, v) : height +
          feedback * 12 + 20
        ]
      });
    }
    return {
      points,
      wires: wiresOut,
      junctions: [...branches.values()].flatMap(b => {
        const ys = [...new Set([...b.left, ...b.right])].sort((a, c) => a - c);
        return ys.filter((y, i) => Number(b.left.has(y)) + Number(b.right.has(y)) + Number(i > 0) + Number(
            i + 1 < ys.length) >= 3)
          .map(y => ({
            x: b.x,
            y,
            role: b.role
          }));
      }),
      width,
      height: height + (feedback ? feedback * 12 + 18 : 0)
    };
  }

  // Independent rungs can share a rail. A shared contact, port or branch remains one circuit.
  function layout(items, wires, language) {
    if (!['LD','SMC2'].includes(language) || items.length < 2) return layoutCircuit(items,wires,language);
    const rails = new Set(items.filter(n => kind(n,language) === 'rail').map(n => n.id));
    const parents = new Map(items.filter(n => !rails.has(n.id)).map(n => [n.id,n.id]));
    function root(id) {
      while (parents.get(id) !== id) {
        parents.set(id, parents.get(parents.get(id)));
        id = parents.get(id);
      }
      return id;
    }
    for (const [a,,b] of wires)
      if (parents.has(a) && parents.has(b)) parents.set(root(b),root(a));
    const components = new Map(), byId = new Map(items.map(n => [n.id,n]));
    function component(id) {
      if (!components.has(id)) components.set(id,{ids:new Set(),wires:[]});
      return components.get(id);
    }
    for (const id of parents.keys()) component(root(id)).ids.add(id);
    const usedRails = new Set();
    for (const w of wires) {
      const a = w[0], b = w[2];
      if (!byId.has(a) || !byId.has(b)) continue;
      const c = component(parents.has(a) ? root(a) : parents.has(b) ? root(b) : 'rails');
      c.wires.push(w);
      for (const id of [a,b]) {
        c.ids.add(id);
        if (rails.has(id)) usedRails.add(id);
      }
    }
    for (const r of rails) if (!usedRails.has(r)) component(r).ids.add(r);
    if (components.size < 2) return layoutCircuit(items,wires,language);
    const all = {points:new Map(),glyphs:[],wires:[],junctions:[],dividers:[],width:0,height:0};
    for (const c of components.values()) {
      const plan = layoutCircuit([...c.ids].map(id => byId.get(id)),c.wires,language);
      const dy = all.height;
      if (dy) all.dividers.push(dy - 5);
      for (const [id,item] of plan.points) {
        const v = {...item,y:item.y + dy};
        all.glyphs.push([id,v]);
        if (!all.points.has(id)) all.points.set(id,v);
      }
      for (const w of plan.wires) {
        const [x,y,u,v] = w.bounds;
        all.wires.push({...w,dy,bounds:[x,y + dy,u,v + dy]});
      }
      for (const b of plan.junctions) all.junctions.push({...b,y:b.y + dy});
      all.width = Math.max(all.width,plan.width);
      all.height += plan.height + 12;
    }
    return all;
  }

  function draw(plan, open, focus, clip) {
    const visible = (a, b, c, d) => !clip || (a <= clip[2] && c >= clip[0] && b <= clip[3] && d >= clip[1]);
    const svg = sv('svg', {
      xmlns: svgNS,
      width: plan.width,
      height: plan.height,
      viewBox: '0 0 ' + plan.width + ' ' + plan.height,
      class: 'ladder-svg',
      role: 'img',
      'aria-label': '源码元件与端口接线'
    });
    for (const y of plan.dividers || [])
      if (visible(0,y,plan.width,y)) svg.append(path('M 4 ' + y + ' H ' + (plan.width-4),'#e1e9e9',{'stroke-width':1}));
    for (const {
        w, dy,
        route,
        bounds
      }
      of plan.wires) {
      if (!visible(...bounds)) continue;
      const dataPin = w[4] === 'pin',
        wire = path(route, dataPin ? data : flow, {
          'data-wire': w[0] + '>' + w[2],
          'stroke-width': dataPin ? 1.3 : 2
        });
      if (dy) wire.setAttribute('transform','translate(0 ' + dy + ')');
      if (dataPin) wire.setAttribute('stroke-dasharray', '4 3');
      wire.append(sv('title', {}, '#' + w[0] + ' ' + w[1] + ' → #' + w[2] + ' ' + (w[3] || '')));
      svg.append(wire);
    }
    for (const b of plan.junctions)
      if (visible(b.x - 3, b.y - 3, b.x + 3, b.y + 3)) svg.append(sv('circle', {
        cx: b.x,
        cy: b.y,
        r: 3,
        fill: b.role === 'pin' ? data : flow
      }));
    for (const [id, v] of plan.glyphs || plan.points) {
      if (!visible(v.x - 105, v.y - v.h / 2 - 32, v.x + 105, v.y + v.h / 2 + 12)) continue;
      const g = sv('g', {
        transform: 'translate(' + v.x + ' ' + v.y + ')',
        'data-node': id,
        'data-symbol': v.k,
        tabindex: 0,
        role: 'button',
        'aria-label': v.n.label
      });
      g.append(sv('title', {}, v.n.label + ' · ' + v.n.type + ' · XML L' + v.n.line));
      g.append(sv('rect', {
        x: -96,
        y: -v.h / 2 - 14,
        width: 192,
        height: v.h + 25,
        rx: 4,
        fill: '#ffffff',
        'fill-opacity': 0,
        stroke: id === focus ? '#cca14e' : 'none',
        'stroke-dasharray': '4 3',
        'pointer-events': 'all'
      }));
      symbol(g, v.k, v.n, v.h, v.pins);
      g.onclick = () => open(id);
      g.onkeydown = e => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          open(id);
        }
      };
      svg.append(g);
    }
    return svg;
  }

  function textWidth(label) {
    return Array.from(label).reduce((n,c) => n + (/[\u2e80-\uffff]/u.test(c) ? 13 : 7.6),0);
  }
  function fitLines(label,width,count) {
    const lines = [];
    let rest = label;
    while (rest && lines.length < count) {
      let cut = 0, at = 0, w = 0;
      for (const c of rest) {
        if (w + textWidth(c) > width) break;
        w += textWidth(c);
        cut += c.length;
        if ('.[ /'.includes(c)) at = cut;
      }
      if (cut === rest.length) { lines.push(rest); break; }
      if (lines.length === count-1) { lines.push(rest.slice(0, Math.max(1,cut-1)) + '…'); break; }
      cut = at > cut / 2 ? at : Math.max(1,cut);
      lines.push(rest.slice(0,cut));
      rest = rest.slice(cut);
    }
    return lines;
  }
  function outputBox(target) {
    const cut = target.indexOf(' := '), lhs = cut < 0 ? target : target.slice(0,cut);
    const rhs = cut < 0 ? '' : ':= ' + target.slice(cut+4);
    return {lhs,rhs,w:Math.max(158,Math.min(390,Math.ceil(Math.max(textWidth(lhs),textWidth(rhs)) + 26)))};
  }
  const guardWidth = (s,target) => s.w + outputBox(target).w + 62;

  function guardPlan(tree) {
    // Series and parallel are used only for AND/OR. Preserve the operator of
    // negated compound expressions and XOR instead of depicting them as OR.
    function shape(t) {
      if (t[0] === 'and' || t[0] === 'or') {
        const kids = t.slice(1).map(shape);
        if (kids.length) return {
          op: t[0],
          kids,
          w: t[0] === 'and' ? kids.reduce((a, b) => a + b.w, 0) : Math.max(...kids.map(c => c.w)) + 42,
          h: t[0] === 'or' ? kids.reduce((a, b) => a + b.h, 0) : Math.max(...kids.map(c => c.h))
        };
      }
      const repr = a => a[0] === 'term' || a[0] === 'opaque' ? a[1] : a[0].toUpperCase() + '(' + a.slice(1).map(
        repr).join(', ') + ')';
      const neg = t[0] === 'not' && t[1]?.[0] === 'term',
        atom = neg ? t[1] : t;
      const label = repr(atom), plain = atom[0] === 'term';
      if (plain && !neg && /^TRUE$/i.test(label)) return {op:'through',label,w:68,h:76};
      const comparison = plain && !neg && /^(.+?)\s*(<>|<=|>=|(?<![<>=!])=(?!=)|<|>)\s*([^<>=]+)$/u.exec(label);
      const contact = plain && !/^(TRUE|FALSE)$/i.test(label) && /^[\p{L}_][\p{L}\d_.\[\],]*$/u.test(label);
      const parts = comparison ? [comparison[1].trim(),comparison[2],comparison[3].trim()] : null;
      const display = neg && !contact ? 'NOT (' + label + ')' : label;
      const w = Math.max(155,Math.min(360,Math.ceil((parts ? Math.max(textWidth(parts[0]),textWidth(parts[2])) : textWidth(display)) + 48)));
      return {op:parts ? 'compare' : contact ? 'contact' : 'block',label:display,parts,neg,opaque:atom[0] === 'opaque',w,h:86};
    }
    return shape(tree);
  }

  function guard(tree, target, s = guardPlan(tree), highlight = null) {
    const output = outputBox(target), width = guardWidth(s,target),
      height = s.h + 30;
    const svg = sv('svg', {
      xmlns: svgNS,
      width,
      height,
      viewBox: '0 0 ' + width + ' ' + height,
      class: 'ladder-svg',
      role: 'img',
      'aria-label': '写入条件梯形图'
    });
    const line = (x, y, u, v) => svg.append(path('M ' + x + ' ' + y + ' L ' + u + ' ' + v, flow));

    function render(a, x, y) {
      if (a.op === 'and') {
        for (const c of a.kids) {
          render(c, x, y);
          x += c.w;
        }
        return;
      }
      if (a.op === 'or') {
        let top = y - a.h / 2;
        const j = x + 14,
          k = x + a.w - 14,
          centers = [];
        for (const c of a.kids) {
          const cy = top + c.h / 2;
          centers.push(cy);
          render(c, j, cy);
          line(j + c.w, cy, k, cy);
          top += c.h;
        }
        line(x, y, j, y);
        line(k, y, x + a.w, y);
        for (const at of [j, k]) {
          line(at, centers[0], at, centers.at(-1));
          const taps = new Set(centers);
          for (const cy of new Set([...centers,y])) {
            const degree = Number(taps.has(cy)) + Number(cy === y) +
              Number(cy > centers[0]) + Number(cy < centers.at(-1));
            if (degree >= 3) svg.append(sv('circle',{cx:at,cy,r:3,fill:flow}));
          }
        }
        return;
      }
      const mid = x + a.w / 2,
        g = sv('g', {
          transform: 'translate(' + mid + ' ' + y + ')',
          'data-symbol': a.op
        });
      g.append(sv('title', {}, a.label));
      svg.append(g);
      if (a.op === 'through') { line(x,y,x+a.w,y); return; }
      const hit = highlight && (a.parts || [a.label]).some(v => v.toLowerCase() === highlight.toLowerCase());
      const pad = a.op === 'contact' ? 65 : a.w / 2 - 12;
      if (hit) g.append(sv('rect',{x:-a.w/2+3,y:-40,width:a.w-6,height:77,rx:4,fill:'#fff4d8'}));
      if (a.op === 'contact') {
        g.append(path('M -65 0 H -10 M 10 0 H 65 M -10 -13 V 13 M 10 -13 V 13',flow));
        if (a.neg) g.append(path('M -15 15 L 15 -15',flow));
        const lines=fitLines(a.label,a.w-12,2);
        lines.forEach((t,i) => g.append(textAt(0,-20-14*(lines.length-1-i),t,{'text-anchor':'middle'})));
      } else {
        const hh = a.parts ? 32 : 23;
        g.append(sv('rect',{x:-pad,y:-hh,width:2*pad,height:2*hh,rx:2,fill:'#f4f8f8',stroke:ink,
          ...(a.opaque ? {'stroke-dasharray':'4 3'} : {})}));
        const lines = a.parts ? [...fitLines(a.parts[0],2*pad-12,2),a.parts[1] + ' ' + a.parts[2]] :
          fitLines(a.label,2*pad-12,3);
        lines.forEach((t,i) => g.append(textAt(0,4 + (i - (lines.length-1)/2)*15,t,{
          'text-anchor':'middle','font-weight':a.parts && i === lines.length-1 ? 700 : 400
        })));
      }
      line(x, y, mid - pad, y);
      line(mid + pad, y, x + a.w, y);
    }
    const y = height / 2;
    line(12, 9, 12, height - 9);
    line(12, y, 22, y);
    render(s, 22, y);
    const x = 22 + s.w;
    line(x, y, x + 12, y);
    const g = sv('g',{'data-symbol':'assignment',transform:'translate(' + (x+12) + ' ' + y + ')'});
    g.append(sv('title',{},target),sv('rect',{x:0,y:-33,width:output.w,height:66,rx:2,fill:'#e6f3ed',stroke:flow}));
    const lines = [...fitLines(output.lhs,output.w-16,2),...fitLines(output.rhs,output.w-16,2)];
    lines.forEach((t,i) => g.append(textAt(output.w/2,4 + (i-(lines.length-1)/2)*14,t,{'text-anchor':'middle','font-weight':i===0 ? 700 : 400})));
    svg.append(g);
    return svg;
  }
  function legend(parent) {
    const entry = box(parent, '梯形图符号');
    const grid = el('div', undefined, 'ld-key');
    entry.append(grid);
    const examples = [
      ['contact', '常开触点', {}], ['contact', '常闭触点', {negated:true}],
      ['contact', '上升沿', {edge:'rising'}], ['contact', '下降沿', {edge:'falling'}],
      ['coil', '普通线圈', {}], ['coil', '取反线圈', {negated:true}],
      ['coil', '置位线圈', {storage:'set'}], ['coil', '复位线圈', {storage:'reset'}]
    ];
    for(const [k,name,props] of examples){
      const svg=sv('svg',{width:138,height:58,viewBox:'0 0 138 58',role:'img','aria-label':name});
      const mark=sv('g',{transform:'translate(69 38)'});
      symbol(mark,k,{label:name,...props},44,{left:[],right:[]});svg.append(mark);grid.append(svg);
    }
  }
  return {
    layout,
    draw,
    guard,
    guardPlan,
    guardWidth,
    legend,
    kind
  };
})();

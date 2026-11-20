/* Follow only transitions justified by an individual ST statement or a wired write. */
const Lineage = (() => {
  let upstream, downstream, boundaries, byNode, boundaryNodes;

  function index() {
    if (upstream) return;
    upstream = new Map();
    downstream = new Map();
    boundaries = new Map();
    byNode = new Map();
    boundaryNodes = new Map();
    const add = (map, key, i) => {
      if (!map.has(key)) map.set(key, []);
      map.get(key).push(i);
    };
    (D.plant.dependency_boundaries || []).forEach((row,i) => {
      add(boundaries,row[0],i);
      add(boundaryNodes,row[2],i);
    });
    D.plant.dependencies.forEach(([target, sources], i) => {
      add(upstream, target, i);
      add(byNode,D.plant.dependencies[i][3],i);
      for (const sid of sources) add(downstream, sid, i);
    });
  }

  function showBoundaries(parent,rows) {
    if (!rows?.length) return;
    const d = box(parent,'接口边界 · ' + rows.length);
    paged(d,rows,(p,i) => {
      const [,net,node,line,limits] = D.plant.dependency_boundaries[i];
      for (const [at,pin,why] of limits) {
        const [sourceNet,sourceNode] = nodeMap.get(at);
        p.append(button(sourceNode.label + (pin ? '.' + pin : '') + ' · ' + ({
          block_output:'块输出',block_status:'执行状态',ambiguous_port:'输出端口待核',element_output:'元件输出'
        }[why] || why) + ' · XML L' + sourceNode.line,() => go('nets',sourceNet.id,{focus:at})));
      }
      p.append(button('目标写点',() => go('nets',net,{focus:node,stLine:line})));
    },8);
  }

  function writes(parent,node,line) {
    index();
    const rows = (byNode.get(node.id) || []).filter(i => !line || D.plant.dependencies[i][5] === line);
    const limits = (boundaryNodes.get(node.id) || []).filter(i => !line || D.plant.dependency_boundaries[i][3] === line);
    showBoundaries(parent,limits);
    if (!rows.length) return;
    const panel = box(parent,'本写点的信号依赖 · ' + rows.length);
    panel.ontoggle = () => {
      if (!panel.open || panel.dataset.ready) return;
      panel.dataset.ready = '1';
      paged(panel,rows,(p,i) => {
        const [sid,,,,,st,call] = D.plant.dependencies[i], r = D.plant.dependency_roles[i];
        const a = node.assignments?.[r.statement];
        const header = a ? 'ST ' + st + '　' + a.target + ' := ' + a.value : node.label;
        p.append(el('h4',header + '　→ ' + D.symbols[sid].name));
        if (call != null) {
          const c = D.calls[call];
          p.append(button('调用实例 · ' + (c.instance || c.type),() => go('nets',c.net,{focus:c.node,stLine:c.st_line})));
        }
        for (const [field,label] of [['when','写入条件'],['value','赋值取数'],['value_enable','取数使能']]) {
          if (!r[field]?.length) continue;
          const line = terms(p);
          line.append(el('strong',label));
          for (const id of r[field]) line.append(button(D.symbols[id].name,() => go('symbols',id)));
        }
      },8);
    };
  }

  function render(parent, origin, drawRoute, locate) {
    const panel = box(parent, '沿指令追踪上游和下游');
    panel.ontoggle = () => {
      if (!panel.open || panel.dataset.ready) return;
      panel.dataset.ready = '1';
      start();
    };

    function start() {
      index();
      const content = el('div');
      panel.append(content);
      let route = [origin],
        steps = [],
        direction = 'down', role = 'all';

      function draw() {
        content.replaceChildren();
        const row = bar(content);
        select(row, [
          ['down', '沿信号查看下游写入'],
          ['up', '逆向追溯信号来源']
        ], direction, d => {
          direction = d;
          route = [origin];
          steps = [];
          drawRoute(route);
          draw();
        });
        select(row,[['all','全部依赖'],['when','写入条件'],['value','赋值取数'],['value_enable','取数使能']],role,v => { role=v; draw(); });
        if (steps.length) row.append(button('退回一段', () => {
          route.pop();
          steps.pop();
          drawRoute(route);
          draw();
        }));
        const label = sid => D.symbols[sid].name;

        function callEvidence(target, id) {
          if (id == null) return;
          const call = D.calls[id],
            n = nodeMap.get(call.node)[1];
          target.append(button('调用实例 ' + (call.instance || call.type) + ' · XML L' + n.line, () => go('nets',
            call.net, {
              focus: call.node,
              stLine: call.st_line
            })));
        }
        para(content, (direction === 'down' ? route : route.slice().reverse()).map(label).join(' → '), 'tag');
        const key = route.at(-1),
          ids = (direction === 'down' ? downstream : upstream).get(key) || [];
        steps.forEach(([i, src, dest]) => {
          const [, , net, node, kind, st, call] = D.plant.dependencies[i], n = nodeMap.get(node)[1];
          content.append(button(label(src) + ' → ' + label(dest) + ' · ' + (kind === 'wire' ? '源码接线' :
            'ST ' + st) + ' · XML L' + n.line, () => locate({
            net,
            node,
            st
          })));
          callEvidence(content, call);
        });
        if (direction === 'up') showBoundaries(content,boundaries.get(key));
        const next = [];
        for (const i of ids) {
          const [target, inputs] = D.plant.dependencies[i], options = direction === 'down' ? [target] : inputs;
          for (const sid of options) {
            const src = direction === 'down' ? key : sid, r = D.plant.dependency_roles[i];
            if (role !== 'all' && !r[role]?.includes(src)) continue;
            const caption = [['when','写入条件'],['value','赋值取数'],['value_enable','取数使能']]
              .filter(([field]) => r[field]?.includes(src)).map(([,label]) => label).join(' / ');
            next.push([sid,i,caption]);
          }
        }
        if (!next.length) {
          para(content, '当前范围没有下一段指令依赖。', 'muted');
          return;
        }
        paged(content, next, (p, [sid, i, caption]) => {
          const [dest, , net, node, kind, st, call] = D.plant.dependencies[i], n = nodeMap.get(node)[1],
            src = direction === 'down' ? key : sid;
          const b = button(caption + ' · ' + label(sid) + ' · ' + leafScope(net) + ' / ' + (kind === 'wire' ? n.type : 'ST ' +
            st) + ' · L' + n.line, () => {
            route.push(sid);
            steps.push([i, src, dest]);
            drawRoute(route);
            draw();
          });
          if (route.includes(sid)) {
            b.disabled = true;
            b.textContent += '（闭环）';
          }
          p.append(b, button('核对指令', () => go('nets', net, {
            focus: node,
            stLine: st
          })));
          callEvidence(p, call);
        }, 12);
      }

      function leafScope(net) {
        return D.nets[net].scope.split('::').pop();
      }
      draw();
    }
  }
  return {
    render, writes
  };
})();

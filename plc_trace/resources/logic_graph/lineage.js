/* Follow only transitions justified by an individual ST statement or a wired write. */
const Lineage = (() => {
  let upstream, downstream;

  function index() {
    if (upstream) return;
    upstream = new Map();
    downstream = new Map();
    const add = (map, key, i) => {
      if (!map.has(key)) map.set(key, []);
      map.get(key).push(i);
    };
    D.plant.dependencies.forEach(([target, sources], i) => {
      add(upstream, target, i);
      for (const sid of sources) add(downstream, sid, i);
    });
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
        direction = 'down';

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
        const next = [];
        for (const i of ids) {
          const [target, inputs] = D.plant.dependencies[i], options = direction === 'down' ? [target] : inputs;
          for (const sid of options) next.push([sid, i]);
        }
        if (!next.length) {
          para(content, '这里没有再经单条已解析赋值或接线证实的依赖。可从信号读写索引继续核查调用或间接引用。', 'muted');
          return;
        }
        paged(content, next, (p, [sid, i]) => {
          const [dest, , net, node, kind, st, call] = D.plant.dependencies[i], n = nodeMap.get(node)[1],
            src = direction === 'down' ? key : sid;
          const b = button(label(sid) + ' · ' + leafScope(net) + ' / ' + (kind === 'wire' ? n.type : 'ST ' +
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
    render
  };
})();

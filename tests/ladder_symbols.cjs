// Exercise the actual SVG painter without requiring a browser or a JavaScript package.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
function element(tag, attrs, text) {
  return {tag, attrs: {...attrs}, text, children: [],
    append(...kids) { this.children.push(...kids); },
    setAttribute(k,v) { this.attrs[k] = v; },
    get lastChild() { return this.children.at(-1); }
  };
}
const js = fs.readFileSync(path.join(__dirname,'../plc_trace/resources/logic_graph/ladder.js'),'utf8');
const ld = vm.runInNewContext(js + '\nLD', {sv:element, svgNS:'http://www.w3.org/2000/svg'});
const net = [
  {type:'LeftPowerRail'}, {type:'contact',label:'A'},
  {type:'Contact',label:'B',negated:'true',edge:'falling'},
  {type:'coil',label:'Output',storage:'set'}, {type:'Coil',label:'Memory',latch:'reset',edge:'rising'},
  {type:'block',label:'External'}, {type:'inVariable',label:'10'}
].map((n,id)=>({id,label:n.type,line:42,...n}));
const wires = [[0,'',1,'','control'],[0,'',2,'','control'],[1,'',3,'','control'],
  [2,'',3,'','control'],[3,'',4,'','control'],[6,'',5,'IN','pin']];
const plan = ld.layout(net,wires,'LD');
const svg = ld.draw(plan,()=>{},null);
const nodes = svg.children.filter(n=>n.tag==='g');
const byId = id => nodes.find(n=>n.attrs['data-node']===id);
assert.equal(svg.children.filter(n=>'data-wire' in n.attrs).length,wires.length);
assert.equal(byId(0).attrs['data-symbol'],'rail');
assert.equal(byId(1).attrs['data-symbol'],'contact');
const mark = (id,what) => byId(id).children.some(n=>n.text===what||n.attrs.d===what);
assert(!mark(1,'M -15 15 L 15 -15'));
assert(mark(2,'M -15 15 L 15 -15'));assert(mark(2,'N'));assert(mark(3,'S'));assert(mark(4,'R'));assert(mark(4,'P'));
assert(plan.junctions.length>=2);
assert.equal(byId(5).attrs['data-symbol'],'block');
assert(svg.children.find(n=>n.attrs['data-wire']==='6>5').attrs['stroke-dasharray']);
const loop=ld.layout(net.slice(1,3),[[1,'',2,'','control'],[2,'',1,'','control']],'LD');
assert.equal(loop.wires.length,2);assert(Number.isFinite(loop.height));
const gate = ld.guard(['and',['term','A'],['or',['term','B'],['not',['term','C']]]],'Step := 20');
assert.equal(gate.children.filter(n=>n.attrs['data-symbol']==='contact').length,3);
const xor=ld.guard(['xor',['term','A'],['term','B']],'Step := 20');
assert.equal(xor.children.filter(n=>n.attrs['data-symbol']==='block').length,1);
assert.equal(ld.draw(plan,()=>{},null,[-100,-100,-5,-5]).children.filter(n=>n.tag==='g').length,0);
const railOnly = ld.layout(net.slice(0,5),[[0,'',1,'','control'],[0,'',2,'','control'],
  [1,'',3,'','control'],[2,'',4,'','control']],'LD');
assert.equal(railOnly.dividers.length,1);
const railPaint = ld.draw(railOnly,()=>{},null);
assert.equal(railPaint.children.filter(e=>e.attrs['data-node']===0).length,2);
assert.equal(railPaint.children.filter(e=>'data-wire' in e.attrs).length,4);
const branched = ld.layout(net.slice(0,5),[[0,'',1,'','control'],[1,'',2,'','control'],
  [1,'',3,'','control'],[2,'',4,'','control']],'LD');
assert(!branched.dividers?.length);
const through = ld.guard(['term','TRUE'],'Target.WithALongVariableName[12] := SourceInput');
assert(!through.children.some(n=>n.attrs['data-symbol']==='contact'));
assert(through.children.some(n=>n.attrs['data-symbol']==='assignment'));
const compare = ld.guard(['term','Value[2,1].Quality<>0'],'Next := 200');
assert(compare.children.some(n=>n.attrs['data-symbol']==='compare' && n.children.some(c=>c.text==='<> 0')));

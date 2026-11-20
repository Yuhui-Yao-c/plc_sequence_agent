// Verify the directional matrix and its observation filter without a browser dependency.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const D = {
  plant:{
    groups:['Send','UseA','UseB','Template'].map((scope,i)=>({scope,kind:i === 3 ? 'functionBlock' : 'program'})),
    routines:[0,1,2,3].map(group=>({group})), net_routine:[0,1,2,3],
    signal_buses:[
      {symbol:4,writers:[0,3],readers:[0,1,2],guard_groups:[1,2],guards:[0,1,2]},
      {symbol:5,writers:[2],readers:[1],guard_groups:[],guards:[]}
    ]
  },
  sites:[{net:1},{net:1},{net:2}],
  channels:[{pairs:[{events:[[0,1,4]],sites:[0]},{events:[],sites:[2]}]}]
};
const file=path.join(__dirname,'../plc_trace/resources/logic_graph/overview.js');
const overview=vm.runInNewContext(fs.readFileSync(file,'utf8')+'\nOverview',{D});
const guards=overview.matrix('guard').rows;
assert.deepEqual([...guards.keys()],[0]);
assert.equal(guards.get(0).get(1).length,1);
assert.equal(guards.get(0).get(2).length,1);
assert.deepEqual([...overview.matrix('observed').rows.get(0).keys()],[1]);
const reads=overview.matrix('all').rows;
assert.equal(reads.get(2).get(1)[0].symbol,5);
assert(!reads.get(0).has(0) && !reads.has(3));
assert.equal(overview.listing('use').length,2);

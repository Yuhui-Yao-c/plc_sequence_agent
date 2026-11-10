"""Read SMC2 instruction references with their original evidence limits."""
import re
from lxml import etree
from smc2_adapter import networks,integer,IDENT
FIELDS=['source_format','source_unit','network','guard','command','arguments','feedback','next_state','relation','evidence','comment']
GENERIC={'MOVE','EQ','NE','=','<>','>=','<=','>','<','AND','OR','NOT','TON','TOF','TP','UP','DOWN','INC','CLEAR','ARYMOVE','ARYSHL','SETBLOCK','DINT_TO_REAL','ADD','SUB','MUL','DIV'}
FEEDBACK=re.compile(r'(DONE|FINISH|READY|POSOK|COMPLETE|INPLACE|HOMED|REACHED|SENSOR|SIGNAL|CLOSED|OPENED|到位|完成)',re.I)
BOOL=re.compile(r'(START|EXECUTE|MOVE|OUTPUT|COMMAND|CMD|CLAMP|GRIP|VACUUM|SUCTION|VALVE|SIGNAL|TRIGGER|CYLINDER|MOTOR|ACTION|取料|启动)',re.I)

def smc2_events(source):
  for _,entry in etree.iterparse(str(source),events=('end',),tag='entry',huge_tree=True,resolve_entities=False,no_network=True):
    if entry.get('type')=='json':
      for ix,net in enumerate(networks(entry.text or '')):
        nodes=net.get('CLs',[]);cmp=[];writes=[];feedback=[];cmds=[]
        for n in nodes:
          typ=n.get('__type'); name=str(n.get('Name','')).lstrip('@'); nn=name.upper()
          ins={str(p.get('Arg')):str(p.get('Var','')).strip() for p in n.get('In',[]) if p.get('__type')=='PRM'}
          if typ=='F' and nn in ('=','EQ','=='):
            a,b=ins.get('In1',''),ins.get('In2','');av,bv=integer(a),integer(b)
            if av is None and bv is not None and IDENT.fullmatch(a):cmp.append((a,bv))
            if bv is None and av is not None and IDENT.fullmatch(b):cmp.append((b,av))
          if typ=='F' and nn=='MOVE':
            val=integer(ins.get('In',''))
            if val is not None:
              for p in n.get('Out',[]):
                if p.get('Arg')=='Out' and p.get('Var'):writes.append((str(p['Var']),val))
          if typ=='LD' and n.get('Var') and FEEDBACK.search(str(n['Var'])):
            feedback.append(('NOT ' if n.get('Not') else '')+str(n['Var']))
          if typ in ('F','FB') and nn not in GENERIC:
            args=';'.join(f'{k}={v}' for k,v in ins.items() if v and k.upper() not in ('EN','ALARM','AUTO_MANUAL'))
            cmds.append((name,args))
          if typ=='ST' and n.get('Var') and BOOL.search(str(n['Var'])):
            cmds.append(('coil'+('_SET' if n.get('S') else '_RESET' if n.get('RS') else ''),str(n['Var'])))
          if typ=='IST' and n.get('TXT'):
            txt=str(n['TXT'])
            for m in re.finditer(r'([\w.\[\]]+)\s*:=\s*(TRUE|FALSE)\s*;',txt,re.I):
              if BOOL.search(m.group(1)):cmds.append(('ST_assignment',m.group(0)))
        guard=' OR '.join(f'{v}={i}' for v,i in dict.fromkeys(cmp))
        nxt=' OR '.join(f'{v}={i}' for v,i in dict.fromkeys(writes) if any(v==x for x,_ in cmp))
        for name,args in cmds:
          yield ['smc2',entry.get('name',''),ix,guard,name,args,' AND '.join(dict.fromkeys(feedback)),nxt,'same_network_observation','co_location_only',str(net.get('CMT',''))[:220]]
    entry.clear()
    while entry.getprevious() is not None:del entry.getparent()[0]

"""Vendor-neutral adapter for XML containers whose entries embed JSON LD/FBD networks.
No project-specific variable names, state values, or truth labels.
"""
import json,re
IDENT=re.compile(r'^[^\s=<>:]+(?:\[[^\]]+\])?$')
NUM=re.compile(r'^(?:INT|DINT|UINT|UDINT|LINT|SINT|USINT|WORD|DWORD|LWORD)#\s*(-?\d+)$|^(-?\d+)$',re.I)
def integer(v):
 m=NUM.fullmatch(str(v).strip())
 return int(m.group(1) or m.group(2)) if m else None
def networks(text):
 d=json.JSONDecoder();i=0
 while i<len(text):
  while i<len(text) and (text[i].isspace() or text[i]==','):i+=1
  if i>=len(text):break
  try:obj,j=d.raw_decode(text,i)
  except json.JSONDecodeError:break
  i=j
  if isinstance(obj,dict) and isinstance(obj.get('CLs'),list):yield obj

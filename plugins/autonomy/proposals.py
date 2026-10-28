import json

def parse(raw):
 try:
  d=json.loads(raw)
 except Exception:
  try: d=json.JSONDecoder().raw_decode(raw)[0]
  except Exception: return [],'invalid_json'
 out=[]
 for x in d.get('proposals',[])[:3]:
  if not isinstance(x,dict) or x.get('type') not in {'goal','task','message_draft'} or not x.get('title') or not x.get('description'): continue
  out.append({'type':x['type'],'title':str(x['title'])[:200],'description':str(x['description'])[:2000],'status':'proposed'})
 return out,None

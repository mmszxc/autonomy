from autonomy.gateway_client import GatewayClient

def test_start_run_uses_input_and_session_id(monkeypatch):
 c=GatewayClient('http://x','k'); seen={}
 def req(method,path,body=None,headers=None,timeout=None): seen.update(method=method,path=path,body=body,headers=headers,timeout=timeout); return 202,{'run_id':'r1','status':'started'}
 monkeypatch.setattr(c,'_req',req); out=c.start_run({},'abc','独处提示'); assert out['run_id']=='r1'; assert seen['body']=={'input':'独处提示','session_id':'autonomy_abc'}; assert seen['timeout']==30; assert 'messages' not in seen['body']

def test_get_and_stop_run_paths(monkeypatch):
 c=GatewayClient('http://x','k'); calls=[]
 monkeypatch.setattr(c,'_req',lambda *a,**k: calls.append((a,k)) or (200,{'status':'completed','output':'x'})); assert c.get_run('r') ['output']=='x'; c.stop_run('r'); assert calls[0][0][:2]==('GET','/v1/runs/r'); assert calls[1][0][:2]==('POST','/v1/runs/r/stop')


def test_append_run_message_uses_official_steer_endpoint(monkeypatch):
 c=GatewayClient('http://x','k'); calls=[]
 monkeypatch.setattr(c,'_req',lambda *a,**k: calls.append((a,k)) or (200,{'status':'accepted'}))
 assert c.append_run_message('run-1','换路') == {'status':'accepted'}
 assert calls[0][0][:3] == ('POST','/v1/runs/run-1/steer',{'input':'换路'})

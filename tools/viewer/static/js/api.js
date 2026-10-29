const token=new URLSearchParams(location.search).get('token')||'';
export async function getJSON(path,params={}){const query=new URLSearchParams(params);if(token)query.set('token',token);const response=await fetch(`${path}?${query}`,{cache:'no-store'});if(!response.ok)throw Error(`HTTP ${response.status}`);return response.json()}

import { getJSON } from './api.js';

const label = route => route ? `${route.provider || '默认 provider'} / ${route.model || '默认模型'}` : '配置不可用';

export async function loadModelRoute() {
  const data = await getJSON('/api/model-route');
  document.getElementById('modelRoutePrimary').textContent = label(data.primary);
  const chain = (data.fallback_providers || []).map(label).join(' → ');
  document.getElementById('modelRouteFallbacks').textContent = chain || '未配置';
  const last = data.last_fallback;
  document.getElementById('modelRouteLast').textContent = last
    ? `${last.time || '时间未知'} · ${last.reason}`
    : data.summary;
}

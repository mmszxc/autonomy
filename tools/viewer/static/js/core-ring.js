import{setText}from'./dom.js';
export function updateCore(active){document.getElementById('core').classList.toggle('thinking',active);setText('coreLabel',active?'CORE // THINKING':'CORE // IDLE')}

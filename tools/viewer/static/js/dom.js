export const reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;
export function decode(value){const box=document.createElement('textarea');box.innerHTML=value||'';return box.value}
export function element(tag,text,classes){const node=document.createElement(tag);if(text!==undefined)node.textContent=decode(String(text));if(classes)node.className=classes;return node}
export function setText(id,value){const node=document.getElementById(id),text=decode(String(value));if(node.textContent!==text)node.textContent=text}
export function clearMessages(root){root.querySelectorAll('.empty,.error').forEach(node=>node.remove())}

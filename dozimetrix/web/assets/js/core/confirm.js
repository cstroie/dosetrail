// One confirm dialog for every risky action (docs/design-rules.md): a native <dialog>,
// focus trapped and Escape cancelling by the browser, never window.confirm().
//   if (!(await confirmDialog('Delete this?', { label: 'Delete', danger: true }))) return;

import { h, section } from './dom.js';

export function confirmDialog(message, { title = 'Please confirm', label = 'Confirm', cancel = 'Cancel', danger = false } = {}) {
  return new Promise((resolve) => {
    let answer = false;
    const ok = h('button', { class: `btn ${danger ? 'danger solid' : 'primary'}`, type: 'button', onclick: () => { answer = true; dlg.close(); } }, label);
    const dlg = h('dialog', { class: 'pw-dialog confirm-dialog', 'aria-label': title },
      section(title, { sub: message },
        h('div', { class: 'row jc-end' }, h('button', { class: 'btn', type: 'button', onclick: () => dlg.close() }, cancel), ok)));
    dlg.addEventListener('close', () => { dlg.remove(); resolve(answer); });
    document.body.append(dlg);
    dlg.showModal();
    (danger ? dlg.querySelector('.btn') : ok).focus();  // a destructive confirm starts on Cancel
  });
}

// One confirm dialog for every risky action (docs/design-rules.md): a native <dialog>,
// focus trapped and Escape cancelling by the browser, never window.confirm(). Its sibling asks for
// one line of text (never window.prompt(): that freezes the page and shows a password in clear).
//   if (!(await confirmDialog('Delete this?', { label: 'Delete', danger: true }))) return;
//   const name = await promptDialog('Name of the workspace', { value: 'Mine' });   // null = cancelled
//   const pw = await promptDialog('New password', { password: true, minLength: 10 });

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

/** Ask for one line of text. Resolves to the trimmed text, or null when cancelled or left empty. */
export function promptDialog(message, { title = 'Name', label = 'OK', cancel = 'Cancel', value = '', password = false, minLength = 0, placeholder = '' } = {}) {
  return new Promise((resolve) => {
    let answer = null;
    const input = h('input', { class: 'input', style: { width: '100%' }, type: password ? 'password' : 'text', value, placeholder, minlength: minLength || null,
      autocomplete: password ? 'new-password' : 'off', 'aria-label': title, required: true });
    const form = h('form', { method: 'dialog', onsubmit: (e) => { e.preventDefault(); const v = input.value.trim(); if (!v) return; answer = v; dlg.close(); } },
      input,
      h('div', { class: 'row jc-end', style: { 'margin-top': 'var(--sp-3)' } },
        h('button', { class: 'btn', type: 'button', onclick: () => dlg.close() }, cancel),
        h('button', { class: 'btn primary', type: 'submit' }, label)));
    const dlg = h('dialog', { class: 'pw-dialog confirm-dialog', 'aria-label': title }, section(title, { sub: message }, form));
    dlg.addEventListener('close', () => { dlg.remove(); resolve(answer); });
    document.body.append(dlg);
    dlg.showModal();
    input.focus();
    input.select();
  });
}

const STEPS = [['checks', 'Checks'], ['admin', 'Administrator'], ['site', 'Site'],
  ['dicom', 'DICOM'], ['growth', 'Growth tables'], ['done', 'Finish']];
const view = document.getElementById('view');
let st = null;
let step = 'checks';
let token = sessionStorage.getItem('dx-setup-token') || new URLSearchParams(location.search).get('token') || '';

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k.startsWith('on')) e.addEventListener(k.slice(2), v); else e.setAttribute(k, v === true ? '' : v);
  }
  for (const k of kids.flat()) if (k != null && k !== false) e.append(k instanceof Node ? k : document.createTextNode(String(k)));
  return e;
}

async function api(method, path, body) {
  const r = await fetch(path, { method, headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body) });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `${r.status} ${r.statusText}`);
  return data;
}

const ok = (key) => st && st.checks.some((c) => c.key === key && c.ok);

const DONE = { checks: () => ok('schema') && ok('seed'), admin: () => st && !st.needs_admin, site: () => ok('site'),
  dicom: () => ok('nodes'), growth: () => ok('growth'), done: () => st && st.done };

function stepper() {
  const s = document.getElementById('steps');
  s.replaceChildren(...STEPS.map(([k, label]) => el('li', {
    class: [k === step ? 'on' : '', DONE[k]() ? 'ok' : ''].join(' ') }, label)));
}

function go(next) { step = next; render(); }

function form(fields, submitLabel, onSubmit, extra = []) {
  const err = el('p', { class: 'err', role: 'alert' });
  const f = el('form', {}, fields, el('div', { class: 'actions' }, extra, el('button', { class: 'btn primary', type: 'submit' }, submitLabel)), err);
  f.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    err.textContent = '';
    const btn = f.querySelector('button[type=submit]');
    btn.disabled = true;
    try { await onSubmit(Object.fromEntries(new FormData(f)), err); } catch (e) { err.textContent = e.message; } finally { btn.disabled = false; }
  });
  return f;
}

const skip = (next, label = 'Skip') => el('button', { class: 'btn', type: 'button', onclick: () => go(next) }, label);

async function refresh() {
  try { st = await api('GET', '/api/setup/status'); } catch (e) {
    if (e.message.includes('login')) { location.href = '/login'; return; }
    view.replaceChildren(el('section', {}, el('p', { class: 'err' }, e.message))); return;
  }
}

function checksView() {
  const list = el('ul', { class: 'checks' }, st.checks.map((c) => el('li', {},
    el('span', {}, c.ok ? '✅' : c.optional ? '·' : '⚠️'), el('span', {}, c.label), c.detail ? el('span', { class: 'd' }, c.detail) : null)));
  const parts = [el('hgroup', {}, el('h2', {}, 'Installation checks')), list];
  if (st.needs_admin) {
    parts.push(el('p', { class: 'note' }, 'This server has no account yet. Enter the setup token: the web service printed it to its log when it started, and wrote it to the file ',
      el('code', {}, 'setup-token'), ' in the data folder. It proves you have access to the server.'));
    const tok = el('input', { name: 'token', value: token, autocomplete: 'off', required: true, class: 'mono' });
    if (st.schema === false) {
      parts.push(form([el('label', {}, 'Setup token', tok)], 'Create the database', async (d, err) => {
        token = d.token.trim(); sessionStorage.setItem('dx-setup-token', token);
        const r = await api('POST', '/api/setup/database', { token });
        err.textContent = '';
        await refresh(); render();
        view.append(el('section', {}, el('hgroup', {}, el('h2', {}, 'Database created')), el('pre', {}, r.report.join('\n'))));
      }));
    } else {
      parts.push(form([el('label', {}, 'Setup token', tok)], 'Continue', async (d) => {
        token = d.token.trim(); sessionStorage.setItem('dx-setup-token', token); go('admin');
      }));
    }
  } else {
    parts.push(el('div', { class: 'actions' }, el('button', { class: 'btn primary', type: 'button', onclick: () => go('site') }, 'Continue')));
  }
  return el('section', {}, parts);
}

function adminView() {
  if (!st.needs_admin) return el('section', {}, el('hgroup', {}, el('h2', {}, 'Administrator'), el('p', { class: 'note' }, 'An account exists already.')),
    el('div', { class: 'actions' }, el('button', { class: 'btn primary', type: 'button', onclick: () => go('site') }, 'Continue')));
  return el('section', {}, el('hgroup', {}, el('h2', {}, 'Administrator account'), el('p', { class: 'note' }, 'It gets every role (viewer, exporter, patient identifiers, importer, admin). Create the other users later under Admin → Users.')),
    form([
      el('label', {}, 'Username', el('input', { name: 'username', autocomplete: 'username', required: true, autofocus: true })),
      el('label', {}, 'Full name', el('input', { name: 'name', autocomplete: 'name' })),
      el('div', { class: 'grid2' },
        el('label', {}, 'Password', el('input', { name: 'password', type: 'password', autocomplete: 'new-password', required: true, minlength: 10 })),
        el('label', {}, 'Repeat', el('input', { name: 'password2', type: 'password', autocomplete: 'new-password', required: true }))),
    ], 'Create and sign in', async (d) => {
      if (d.password !== d.password2) throw new Error('The passwords differ.');
      await api('POST', '/api/setup/admin', { token, username: d.username, name: d.name, password: d.password });
      sessionStorage.removeItem('dx-setup-token');
      await refresh(); go('site');
    }, [el('button', { class: 'btn', type: 'button', onclick: () => go('checks') }, 'Back')]));
}

function siteView() {
  return el('section', {}, el('hgroup', {}, el('h2', {}, 'Site'), el('p', { class: 'note' }, 'The institution this server collects for. The name appears on reports; the code identifies the site in survey exports.')),
    form([
      el('label', {}, 'Institution name', el('input', { name: 'name', required: true, value: st.site.name || '' })),
      el('label', {}, 'Short code (letters and digits)', el('input', { name: 'code', value: st.site.code || '', placeholder: 'e.g. HOSP1', class: 'mono' })),
    ], 'Save', async (d) => { await api('POST', '/api/setup/site', d); await refresh(); go('dicom'); }, [skip('dicom')]));
}

function dicomView() {
  const out = el('div', {});
  return el('section', {}, el('hgroup', {}, el('h2', {}, 'DICOM'), el('p', { class: 'note' }, 'This server receives dose reports as AE ', el('code', {}, st.receiver.aet), ' on port ', el('code', {}, String(st.receiver.port)),
      ' (set in the configuration file). Register it on the modalities or the PACS that should send. To pull from a PACS instead, add it here; more nodes later under Admin → DICOM nodes.')),
    form([
      el('div', { class: 'grid2' },
        el('label', {}, 'Name', el('input', { name: 'name', required: true, value: 'PACS' })),
        el('label', {}, 'AE title', el('input', { name: 'aet', required: true, maxlength: 16, class: 'mono' }))),
      el('div', { class: 'grid2' },
        el('label', {}, 'Host', el('input', { name: 'host', required: true, class: 'mono' })),
        el('label', {}, 'Port', el('input', { name: 'port', type: 'number', value: '104', required: true }))),
      el('label', {}, 'Retrieve with', el('select', { name: 'retrieve' },
        el('option', { value: 'get' }, 'C-GET (nothing to configure on the PACS)'),
        el('option', { value: 'move' }, `C-MOVE (register ${st.receiver.aet} on the PACS)`))),
      out,
    ], 'Add and test (C-ECHO)', async (d) => {
      const n = await api('POST', '/api/dicom/nodes', { name: d.name, aet: d.aet, host: d.host, port: Number(d.port),
        retrieve: d.retrieve, query_retrieve: true, sends: true, enabled: true });
      let echo;
      try { echo = await api('POST', `/api/dicom/nodes/${n.id}/echo`); } catch (e) { echo = { ok: false, error: e.message }; }
      out.replaceChildren(el('p', { class: echo.ok ? 'note' : 'err' }, echo.ok ? `${d.name} answers C-ECHO.` : `Saved, but C-ECHO failed: ${echo.error || echo.message || 'no answer'}. Check the PACS registers ${st.receiver.aet}, then test again under Admin → DICOM nodes.`));
      await refresh(); stepper();
    }, [skip('growth', 'Next')]));
}

function growthView() {
  const out = el('div', {});
  return el('section', {}, el('hgroup', {}, el('h2', {}, 'Growth tables'), el('p', { class: 'note' }, 'WHO (0–10 years) and CDC 2000 (to 20 years) weight-for-age tables are downloaded from their publishers (they are not bundled, because of their licence). Dozimetrix uses them to check children\'s weights, to estimate missing ones, and to leave out of DRL statistics the children below P5 or above P95.')),
    ok('growth') ? el('p', { class: 'note' }, '✅ Installed.') : null, out,
    el('div', { class: 'actions' }, skip('done', ok('growth') ? 'Next' : 'Skip'),
      el('button', { class: 'btn primary', type: 'button', onclick: async (ev) => {
        ev.target.disabled = true; out.replaceChildren(el('p', { class: 'note' }, 'Downloading…'));
        try { const r = await api('POST', '/api/setup/growth'); out.replaceChildren(el('pre', {}, r.report.join('\n'))); await refresh(); stepper(); }
        catch (e) { out.replaceChildren(el('p', { class: 'err' }, e.message)); }
        ev.target.disabled = false;
      } }, ok('growth') ? 'Download again' : 'Download')));
}

function doneView() {
  const missing = st.checks.filter((c) => !c.ok && !c.optional);
  return el('section', {}, el('hgroup', {}, el('h2', {}, 'Finish')),
    missing.length ? el('p', { class: 'note' }, 'Still open (you can finish now and do these later): ', missing.map((c) => c.label).join(', '), '.') :
      el('p', { class: 'note' }, 'Everything is in place.'),
    el('p', { class: 'note' }, 'Next: back up the site secret and the database; import history (dozimetrix openrem-import) or let query/retrieve fill it; review devices under Admin → Devices.'),
    el('div', { class: 'actions' }, el('button', { class: 'btn', type: 'button', onclick: () => go('checks') }, 'Back to checks'),
      el('button', { class: 'btn primary', type: 'button', onclick: async () => { await api('POST', '/api/setup/finish'); location.href = '/'; } }, 'Open Dozimetrix')));
}

function render() {
  if (!st) return;
  if (st.needs_admin && !['checks', 'admin'].includes(step)) step = 'checks';
  stepper();
  view.replaceChildren({ checks: checksView, admin: adminView, site: siteView, dicom: dicomView, growth: growthView, done: doneView }[step]());
}

await refresh();
render();

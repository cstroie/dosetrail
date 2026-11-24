// Live updates from the server (Server-Sent Events, /api/live), one stream per tab.
//
//   const stop = live(['dicom', 'his'], { dicom: (v) => …, his: (v) => … });
//
// Every caller (the shell's service badge, the page itself) subscribes to topics; the tab
// keeps a single EventSource for all of them and reopens it when a new topic is added.
// The server sends a topic when it changed (and right after connecting, so a handler also
// gets the current state; a new subscriber gets the last value at once). The stream is
// closed while the tab is hidden and reopened, fresh, when it is shown again; EventSource
// itself reconnects after a network error or a server restart.

const subs = new Map();  // topic -> Set of handlers
const lastValue = new Map();  // topic -> last data
let es = null;
let openTopics = '';
let pending = false;

function wanted() {
  return [...subs.keys()].filter((t) => subs.get(t).size).sort().join(',');
}

function close() { es?.close(); es = null; openTopics = ''; }

function open() {
  const topics = wanted();
  if (document.visibilityState === 'hidden' || !topics) { close(); return; }
  if (es && topics === openTopics) return;
  close();
  openTopics = topics;
  es = new EventSource(`/api/live?topics=${encodeURIComponent(topics)}`);
  for (const t of topics.split(',')) {
    es.addEventListener(t, (e) => {
      let v;
      try { v = JSON.parse(e.data); } catch { return; }
      lastValue.set(t, v);
      for (const fn of subs.get(t) || []) {
        try { fn(v); } catch (err) { console.error('live', t, err); }
      }
    });
  }
}

function schedule() {  // several subscriptions in one tick: open once
  if (pending) return;
  pending = true;
  queueMicrotask(() => { pending = false; open(); });
}

document.addEventListener('visibilitychange', () => (document.visibilityState === 'hidden' ? close() : schedule()));
window.addEventListener('pagehide', close);

export function live(topics, handlers) {
  const mine = [];
  for (const t of topics) {
    const fn = handlers[t];
    if (!fn) continue;
    if (!subs.has(t)) subs.set(t, new Set());
    subs.get(t).add(fn);
    mine.push([t, fn]);
    if (lastValue.has(t)) queueMicrotask(() => fn(lastValue.get(t)));
  }
  schedule();
  return () => {
    for (const [t, fn] of mine) subs.get(t)?.delete(fn);
    schedule();
  };
}

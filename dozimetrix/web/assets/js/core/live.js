// Live updates from the server (Server-Sent Events, /api/live).
//
//   const stop = live(['dicom', 'his'], { dicom: (v) => …, his: (v) => … });
//
// The server sends a topic when it changed (it checks every few seconds) and right after
// connecting, so a handler also gets the current state. The stream is closed while the tab
// is hidden and reopened (with a fresh state) when it is shown again; EventSource itself
// reconnects after a network error or a server restart.

export function live(topics, handlers) {
  let es = null;
  const open = () => {
    if (es || document.visibilityState === 'hidden') return;
    es = new EventSource(`/api/live?topics=${encodeURIComponent(topics.join(','))}`);
    for (const t of topics) {
      es.addEventListener(t, (e) => {
        try { handlers[t]?.(JSON.parse(e.data)); } catch (err) { console.error('live', t, err); }
      });
    }
  };
  const close = () => { es?.close(); es = null; };
  const onVis = () => (document.visibilityState === 'hidden' ? close() : open());
  document.addEventListener('visibilitychange', onVis);
  window.addEventListener('pagehide', close);
  open();
  return () => { document.removeEventListener('visibilitychange', onVis); close(); };
}

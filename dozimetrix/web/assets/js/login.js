const f = document.getElementById('f');
f.addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const body = JSON.stringify(Object.fromEntries(new FormData(f)));
  const r = await fetch('/api/login', { method: 'POST', body,
    headers: { 'Content-Type': 'application/json' } });
  if (r.ok) location.href = '/';
  else document.getElementById('err').textContent = 'Invalid username or password.';
});

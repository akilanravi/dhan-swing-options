const states = new Map();
export default function({data, setTriggerValue}) {
  const {id, limit, remaining} = data;
  for (const key of states.keys()) if (key !== id) states.delete(key);
  const now = Date.now();
  const old = states.get(id);
  const s = old || {last: now - (limit - remaining)*1000, sent: 0, locked: false};
  states.set(id, s);
  let overlay, screen;
  const lock = () => {
    if (s.locked && overlay) return;
    s.locked = true;
    screen = document.querySelector('[data-testid="stApp"]');
    if (screen) screen.inert = true;
    overlay = document.createElement('div');
    overlay.setAttribute('role', 'alert');
    overlay.style.cssText = 'position:fixed;inset:0;z-index:2147483647;background:#080f1d;color:white;display:grid;place-items:center;font:500 18px system-ui';
    overlay.textContent = 'Dashboard locked after inactivity. Reconnecting to the password screen…';
    document.body.append(overlay);
    setTriggerValue('idle', true);
  };
  const check = () => {
    if (s.locked || Date.now() - s.last >= limit*1000) lock();
  };
  const send = () => {
    if (!s.locked && s.last > s.sent) {
      s.sent = s.last;
      setTriggerValue('activity', Math.max(0, Date.now() - s.last));
    }
  };
  const move = (e) => {
    if (!e.isTrusted || document.visibilityState !== 'visible') return;
    check();
    if (s.locked) { e.stopImmediatePropagation(); if (e.cancelable) e.preventDefault(); return; }
    s.last = Date.now();
    if (s.last - s.sent >= 5000) send();
  };
  const events = ['pointermove','pointerdown','keydown','wheel','touchstart'];
  for (const event of events) window.addEventListener(event, move, {capture:true, passive:false});
  const visibility = () => { check(); send(); };
  document.addEventListener('visibilitychange', visibility);
  window.addEventListener('focus', visibility);
  const timer = setInterval(() => { check(); send(); }, 5000);
  check();
  return () => {
    clearInterval(timer);
    for (const event of events) window.removeEventListener(event, move, true);
    document.removeEventListener('visibilitychange', visibility);
    window.removeEventListener('focus', visibility);
    if (screen) screen.inert = false;
    if (overlay) overlay.remove();
  };
}

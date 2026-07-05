import { escHtml } from './dashboard-common.js';

(async function () {
  try {
    const auth = await (await fetch('/api/auth/status')).json();
    const el = document.getElementById('ov-auth');
    if (auth.admin) {
      document.getElementById('logo-dd-admin').style.display = '';
      if (el) {
        el.innerHTML = `<span style="color:var(--green)">${escHtml(auth.user)}</span>
          <a href="/logout" class="hdr-btn" style="font-size:11px;text-decoration:none;padding:3px 10px">Logout</a>`;
      }
    } else if (auth.user) {
      if (el) {
        el.innerHTML = `<span style="color:var(--blue)">${escHtml(auth.user)}</span>
          <a href="/logout" class="hdr-btn" style="font-size:11px;text-decoration:none;padding:3px 10px">Logout</a>`;
      }
    } else {
      if (el) {
        el.innerHTML = `<a href="/login" class="hdr-btn" style="font-size:11px;text-decoration:none;padding:3px 10px">Login</a>`;
      }
    }
  } catch (e) {}
})();

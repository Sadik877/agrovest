/* ============================================================
   MercX Digital Marketplace — Main JS
   ============================================================ */

/* ── Toast System ───────────────────────────────────────────── */
window.MercX = window.MercX || {};

MercX.toast = (function () {
  let container;

  function getContainer() {
    if (!container) {
      container = document.getElementById('toast-container');
      if (!container) {
        container = document.createElement('div');
        container.id = 'toast-container';
        document.body.appendChild(container);
      }
    }
    return container;
  }

  function show(title, message = '', type = 'info', duration = 4500) {
    const icons = { success: 'check-circle', error: 'x-circle', warning: 'alert-triangle', info: 'info' };
    const colors = { success: '#10B981', error: '#EF4444', warning: '#F59E0B', info: '#06B6D4' };

    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.innerHTML = `
      <div class="toast-icon">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="${colors[type]}"
             stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          ${featherPath(icons[type])}
        </svg>
      </div>
      <div class="toast-content">
        <div class="toast-title">${title}</div>
        ${message ? `<div class="toast-message">${message}</div>` : ''}
      </div>
      <button onclick="MercX.toast.dismiss(this.closest('.toast'))"
              style="background:none;border:none;color:#94A3B8;cursor:pointer;padding:0;margin-left:auto;flex-shrink:0">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>
      </button>`;

    getContainer().appendChild(toast);

    const timer = setTimeout(() => MercX.toast.dismiss(toast), duration);
    toast._timer = timer;
    toast.addEventListener('mouseenter', () => clearTimeout(toast._timer));
    toast.addEventListener('mouseleave', () => { toast._timer = setTimeout(() => MercX.toast.dismiss(toast), 2000); });
    return toast;
  }

  function dismiss(toast) {
    if (!toast || toast.classList.contains('removing')) return;
    clearTimeout(toast._timer);
    toast.classList.add('removing');
    setTimeout(() => toast.remove(), 250);
  }

  return { show, dismiss,
    success: (t, m, d) => show(t, m, 'success', d),
    error:   (t, m, d) => show(t, m, 'error',   d),
    warning: (t, m, d) => show(t, m, 'warning',  d),
    info:    (t, m, d) => show(t, m, 'info',     d),
  };
})();

function featherPath(name) {
  const paths = {
    'check-circle':    '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path><polyline points="22 4 12 14.01 9 11.01"></polyline>',
    'x-circle':        '<circle cx="12" cy="12" r="10"></circle><line x1="15" y1="9" x2="9" y2="15"></line><line x1="9" y1="9" x2="15" y2="15"></line>',
    'alert-triangle':  '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path><line x1="12" y1="9" x2="12" y2="13"></line><line x1="12" y1="17" x2="12.01" y2="17"></line>',
    'info':            '<circle cx="12" cy="12" r="10"></circle><line x1="12" y1="16" x2="12" y2="12"></line><line x1="12" y1="8" x2="12.01" y2="8"></line>',
  };
  return paths[name] || '';
}


/* ── Flash messages → Toasts ────────────────────────────────── */
function initFlashToasts() {
  document.querySelectorAll('[data-flash]').forEach(el => {
    const cat = el.dataset.category || 'info';
    const map = { success: 'success', danger: 'error', warning: 'warning', info: 'info', error: 'error' };
    MercX.toast.show(el.dataset.flash, '', map[cat] || 'info');
    el.remove();
  });
}


/* ── Navbar scroll effect ───────────────────────────────────── */
function initNavbar() {
  const nav = document.querySelector('.navbar');
  if (!nav) return;
  const onScroll = () => nav.classList.toggle('scrolled', window.scrollY > 20);
  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll();
}


/* ── Mobile Nav ─────────────────────────────────────────────── */
function initMobileNav() {
  const btn    = document.getElementById('mobile-menu-btn');
  const drawer = document.getElementById('mobile-drawer');
  const close  = document.getElementById('mobile-drawer-close');
  const overlay = document.getElementById('mobile-overlay');
  if (!btn || !drawer) return;

  function open()  { drawer.classList.remove('-translate-x-full'); overlay?.classList.remove('hidden'); }
  function closeD(){ drawer.classList.add('-translate-x-full'); overlay?.classList.add('hidden'); }

  btn.addEventListener('click', open);
  close?.addEventListener('click', closeD);
  overlay?.addEventListener('click', closeD);
}


/* ── Search Autocomplete ────────────────────────────────────── */
function initSearch() {
  const wrap  = document.getElementById('search-wrap');
  const input = document.getElementById('search-input');
  const drop  = document.getElementById('search-dropdown');
  if (!wrap || !input || !drop) return;

  let timer;
  input.addEventListener('input', () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) { drop.classList.add('hidden'); return; }
    timer = setTimeout(async () => {
      try {
        const res  = await fetch(`/api/search/autocomplete?q=${encodeURIComponent(q)}`);
        const data = await res.json();
        if (!data.length) { drop.classList.add('hidden'); return; }
        drop.innerHTML = data.map(item => `
          <a href="/marketplace/p/${item.slug}" class="autocomplete-item">
            <img src="${item.thumb || ''}" class="autocomplete-thumb" onerror="this.style.display='none'"
                 style="${item.thumb ? '' : 'display:none'}">
            <div style="flex:1">
              <div style="font-size:.88rem;font-weight:600;color:#F8FAFC">${item.title}</div>
              <div style="font-size:.78rem;color:#7C3AED;font-weight:700">$${item.price.toFixed(2)}</div>
            </div>
          </a>`).join('');
        drop.classList.remove('hidden');
      } catch {}
    }, 280);
  });

  document.addEventListener('click', e => { if (!wrap.contains(e.target)) drop.classList.add('hidden'); });
  input.addEventListener('keydown', e => { if (e.key === 'Escape') drop.classList.add('hidden'); });
}


/* ── Wishlist Toggle ────────────────────────────────────────── */
function initWishlist() {
  document.addEventListener('click', async e => {
    const btn = e.target.closest('[data-wishlist]');
    if (!btn) return;
    e.preventDefault();
    const id   = btn.dataset.wishlist;
    const icon = btn.querySelector('svg') || btn;
    try {
      const res  = await fetch('/api/wishlist/toggle', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrf() },
        body: JSON.stringify({ listing_id: id }),
      });
      if (res.status === 401) { window.location = '/auth/login'; return; }
      const { in_wishlist } = await res.json();
      btn.classList.toggle('active', in_wishlist);
      icon.style.fill = in_wishlist ? '#EF4444' : 'none';
      icon.style.stroke = in_wishlist ? '#EF4444' : 'currentColor';
      MercX.toast[in_wishlist ? 'success' : 'info'](
        in_wishlist ? 'Added to wishlist' : 'Removed from wishlist', '', 2500);
    } catch { MercX.toast.error('Something went wrong'); }
  });
}


/* ── Cart Count Badge ───────────────────────────────────────── */
async function refreshCartBadge() {
  try {
    const res  = await fetch('/api/cart/count');
    if (!res.ok) return;
    const { count } = await res.json();
    document.querySelectorAll('[data-cart-badge]').forEach(el => {
      el.textContent = count;
      el.classList.toggle('hidden', count === 0);
    });
  } catch {}
}


/* ── Notification Badge ─────────────────────────────────────── */
async function refreshNotifBadge() {
  try {
    const res = await fetch('/api/notifications/unread-count');
    if (!res.ok) return;
    const { count } = await res.json();
    document.querySelectorAll('[data-notif-badge]').forEach(el => {
      el.textContent = count > 99 ? '99+' : count;
      el.classList.toggle('hidden', count === 0);
    });
  } catch {}
}


/* ── CSRF Helper ────────────────────────────────────────────── */
function getCsrf() {
  return document.querySelector('meta[name="csrf-token"]')?.content || '';
}


/* ── AJAX Form Submissions ──────────────────────────────────── */
function initAjaxForms() {
  document.querySelectorAll('[data-ajax-form]').forEach(form => {
    form.addEventListener('submit', async e => {
      e.preventDefault();
      const btn = form.querySelector('[type="submit"]');
      const orig = btn?.innerHTML;
      if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Loading…'; }
      try {
        const fd  = new FormData(form);
        const res = await fetch(form.action, { method: form.method || 'POST', body: fd });
        const ct  = res.headers.get('content-type') || '';
        if (ct.includes('json')) {
          const data = await res.json();
          if (data.redirect) { window.location = data.redirect; return; }
          if (data.success)  { MercX.toast.success(data.success); }
          if (data.error)    { MercX.toast.error(data.error); }
        } else {
          window.location.reload();
        }
      } catch { MercX.toast.error('Request failed. Please try again.'); }
      finally { if (btn) { btn.disabled = false; btn.innerHTML = orig; } }
    });
  });
}


/* ── Copy to Clipboard ──────────────────────────────────────── */
function initCopyLinks() {
  document.addEventListener('click', e => {
    const btn = e.target.closest('[data-copy]');
    if (!btn) return;
    const text = btn.dataset.copy;
    navigator.clipboard.writeText(text).then(() => {
      const orig = btn.textContent;
      btn.textContent = 'Copied!';
      setTimeout(() => { btn.textContent = orig; }, 2000);
      MercX.toast.success('Copied to clipboard!', '', 2000);
    }).catch(() => MercX.toast.error('Copy failed'));
  });
}


/* ── Scroll Reveal ──────────────────────────────────────────── */
function initScrollReveal() {
  const els = document.querySelectorAll('.reveal');
  if (!els.length) return;
  const obs = new IntersectionObserver(entries => {
    entries.forEach((entry, i) => {
      if (entry.isIntersecting) {
        setTimeout(() => entry.target.classList.add('visible'), i * 80);
        obs.unobserve(entry.target);
      }
    });
  }, { threshold: 0.12 });
  els.forEach(el => obs.observe(el));
}


/* ── Counter Animation ──────────────────────────────────────── */
function initCounters() {
  const els = document.querySelectorAll('[data-counter]');
  if (!els.length) return;
  const obs = new IntersectionObserver(entries => {
    entries.forEach(entry => {
      if (!entry.isIntersecting) return;
      const el     = entry.target;
      const target = parseFloat(el.dataset.counter);
      const prefix = el.dataset.prefix || '';
      const suffix = el.dataset.suffix || '';
      const dur    = 1600;
      const start  = performance.now();
      function tick(now) {
        const p = Math.min((now - start) / dur, 1);
        const v = target * (p < 1 ? p * (2 - p) : 1);
        el.textContent = prefix + (target % 1 === 0 ? Math.floor(v).toLocaleString() : v.toFixed(1)) + suffix;
        if (p < 1) requestAnimationFrame(tick);
      }
      requestAnimationFrame(tick);
      obs.unobserve(el);
    });
  }, { threshold: 0.5 });
  els.forEach(el => obs.observe(el));
}


/* ── Image Gallery ──────────────────────────────────────────── */
function initGallery() {
  const main   = document.getElementById('gallery-main');
  const thumbs = document.querySelectorAll('.gallery-thumb');
  if (!main || !thumbs.length) return;
  thumbs.forEach(thumb => {
    thumb.addEventListener('click', () => {
      main.src = thumb.src;
      thumbs.forEach(t => t.classList.remove('active'));
      thumb.classList.add('active');
    });
  });
}


/* ── File Upload Preview ────────────────────────────────────── */
function initFilePreview() {
  document.querySelectorAll('[data-image-preview]').forEach(input => {
    const previewId = input.dataset.imagePreview;
    const preview   = document.getElementById(previewId);
    if (!preview) return;
    input.addEventListener('change', () => {
      const file = input.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = e => { preview.src = e.target.result; preview.classList.remove('hidden'); };
      reader.readAsDataURL(file);
    });
  });

  // Drag-and-drop upload zones
  document.querySelectorAll('.upload-zone').forEach(zone => {
    const input = zone.querySelector('input[type="file"]');
    zone.addEventListener('click', () => input?.click());
    zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('drag-over'); });
    zone.addEventListener('dragleave', () => zone.classList.remove('drag-over'));
    zone.addEventListener('drop', e => {
      e.preventDefault();
      zone.classList.remove('drag-over');
      if (input && e.dataTransfer.files.length) {
        input.files = e.dataTransfer.files;
        input.dispatchEvent(new Event('change', { bubbles: true }));
        const label = zone.querySelector('.upload-label');
        if (label) label.textContent = e.dataTransfer.files[0].name;
      }
    });
  });
}


/* ── Star Rating Input ──────────────────────────────────────── */
function initStarRating() {
  document.querySelectorAll('.star-input').forEach(wrap => {
    const inputs = wrap.querySelectorAll('input[type="radio"]');
    inputs.forEach(input => {
      input.addEventListener('change', () => {
        const val = parseInt(input.value);
        const hidden = wrap.closest('form')?.querySelector('[name="rating"]');
        if (hidden) hidden.value = val;
      });
    });
  });
}


/* ── Admin: Mark notification read ──────────────────────────── */
function markNotifRead(id) {
  fetch('/api/notifications/mark-read', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrf() },
    body: JSON.stringify({ id }),
  }).catch(() => {});
}

async function markAllNotifsRead() {
  await fetch('/api/notifications/mark-all-read', {
    method: 'POST',
    headers: { 'X-CSRFToken': getCsrf() },
  });
  document.querySelectorAll('[data-notif-badge]').forEach(el => {
    el.textContent = '0'; el.classList.add('hidden');
  });
  document.querySelectorAll('.notif-unread').forEach(el => el.classList.remove('notif-unread'));
}


/* ── Confirm Dialogs ────────────────────────────────────────── */
function initConfirmForms() {
  document.querySelectorAll('[data-confirm]').forEach(el => {
    el.addEventListener('click', e => {
      if (!confirm(el.dataset.confirm)) e.preventDefault();
    });
  });
}


/* ── Dropdown Menus ─────────────────────────────────────────── */
function initDropdowns() {
  document.addEventListener('click', e => {
    const trigger = e.target.closest('[data-dropdown]');
    document.querySelectorAll('.dropdown').forEach(dd => {
      if (!trigger || dd.id !== trigger.dataset.dropdown) {
        dd.classList.add('hidden');
      }
    });
    if (trigger) {
      const dd = document.getElementById(trigger.dataset.dropdown);
      dd?.classList.toggle('hidden');
    }
  });
}


/* ── Tab Switcher ───────────────────────────────────────────── */
function initTabs() {
  document.querySelectorAll('[data-tab-group]').forEach(group => {
    const tabs    = group.querySelectorAll('[data-tab]');
    const panels  = group.querySelectorAll('[data-panel]');
    tabs.forEach(tab => {
      tab.addEventListener('click', () => {
        tabs.forEach(t => t.classList.remove('active'));
        panels.forEach(p => p.classList.add('hidden'));
        tab.classList.add('active');
        const panel = group.querySelector(`[data-panel="${tab.dataset.tab}"]`);
        panel?.classList.remove('hidden');
      });
    });
  });
}


/* ── Price Formatter ────────────────────────────────────────── */
function fmtPrice(n) { return '$' + parseFloat(n).toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ','); }


/* ── Init ───────────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  // Feather icons
  if (window.feather) feather.replace({ 'stroke-width': 1.75, width: 18, height: 18 });

  initFlashToasts();
  initNavbar();
  initMobileNav();
  initSearch();
  initWishlist();
  initAjaxForms();
  initCopyLinks();
  initScrollReveal();
  initCounters();
  initGallery();
  initFilePreview();
  initStarRating();
  initConfirmForms();
  initDropdowns();
  initTabs();

  // Refresh badges if logged in
  const isLoggedIn = document.body.dataset.loggedIn === 'true';
  if (isLoggedIn) {
    refreshCartBadge();
    refreshNotifBadge();
    setInterval(() => { refreshCartBadge(); refreshNotifBadge(); }, 60000);
  }
});

// Expose globals
window.MercX.refreshCartBadge  = refreshCartBadge;
window.MercX.markAllNotifsRead = markAllNotifsRead;
window.MercX.markNotifRead     = markNotifRead;
window.MercX.fmtPrice          = fmtPrice;
window.MercX.getCsrf           = getCsrf;

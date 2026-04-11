// Dashboard JS helpers

// ── Toast notification system ──────────────────────────────────────────────
const TOAST_ICONS = {
  success: 'fa-circle-check',
  error:   'fa-circle-xmark',
  warning: 'fa-triangle-exclamation',
  info:    'fa-circle-info',
};

function showToast(message, type = 'info', duration = 4000) {
  const container = document.getElementById('toast-container');
  if (!container) return;

  const toast = document.createElement('div');
  toast.className = `toast toast-${type}`;
  toast.innerHTML = `
    <i class="fa ${TOAST_ICONS[type] || TOAST_ICONS.info}"></i>
    <span>${message}</span>
    <button class="toast-close" onclick="dismissToast(this.closest('.toast'))"><i class="fa fa-xmark"></i></button>
  `;
  container.appendChild(toast);

  if (duration > 0) {
    setTimeout(() => dismissToast(toast), duration);
  }
  return toast;
}

function dismissToast(toast) {
  if (!toast || toast._dismissing) return;
  toast._dismissing = true;
  toast.classList.add('toast-out');
  setTimeout(() => toast.remove(), 230);
}

// Global alias so inline scripts can call toast()
window.toast = showToast;

// ── Sidebar collapse / expand ───────────────────────────────────────────────
function toggleSidebar() {
  const sidebar = document.getElementById('sidebar');
  if (!sidebar) return;
  const collapsed = sidebar.classList.toggle('collapsed');
  try { localStorage.setItem('sidebar-collapsed', collapsed ? '1' : '0'); } catch {}
}

function openMobileSidebar() {
  const sidebar = document.getElementById('sidebar');
  const backdrop = document.getElementById('sidebar-backdrop');
  if (sidebar) sidebar.classList.add('mobile-open');
  if (backdrop) backdrop.style.display = 'block';
  document.body.style.overflow = 'hidden';
}

function closeMobileSidebar() {
  const sidebar = document.getElementById('sidebar');
  const backdrop = document.getElementById('sidebar-backdrop');
  if (sidebar) sidebar.classList.remove('mobile-open');
  if (backdrop) backdrop.style.display = 'none';
  document.body.style.overflow = '';
}

window.toggleSidebar   = toggleSidebar;
window.openMobileSidebar  = openMobileSidebar;
window.closeMobileSidebar = closeMobileSidebar;

// ── Initialisation ───────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {

  // Restore sidebar collapsed state
  try {
    if (localStorage.getItem('sidebar-collapsed') === '1') {
      const sidebar = document.getElementById('sidebar');
      if (sidebar) sidebar.classList.add('collapsed');
    }
  } catch {}

  // Auto-dismiss flash messages after 5 seconds
  document.querySelectorAll('.flash-msg').forEach(el => {
    setTimeout(() => {
      el.style.transition = 'opacity 0.3s ease';
      el.style.opacity = '0';
      setTimeout(() => el.remove(), 320);
    }, 5000);
  });

  // Copy-to-clipboard buttons
  document.querySelectorAll('[data-copy-text]').forEach(button => {
    button.addEventListener('click', async event => {
      event.preventDefault();
      event.stopPropagation();
      const text = button.getAttribute('data-copy-text') || '';
      const label = button.getAttribute('data-copy-label') || 'Value';
      if (!text) return;
      try {
        await navigator.clipboard.writeText(text);
        const original = button.innerHTML;
        button.innerHTML = '<i class="fa fa-check"></i> Copied';
        button.setAttribute('aria-label', `${label} copied`);
        showToast(`${label} copied to clipboard`, 'success', 2000);
        setTimeout(() => {
          button.innerHTML = original;
          button.setAttribute('aria-label', `Copy ${label}`);
        }, 1400);
      } catch {
        showToast('Copy failed — try manually', 'error');
      }
    });
  });

  // Open accordion section if URL has a matching hash
  const openAccordionForHash = () => {
    const hash = window.location.hash;
    if (!hash) return;
    const target = document.querySelector(hash);
    if (!target) return;
    const details = target.closest('details');
    if (details) details.open = true;
  };
  openAccordionForHash();
  window.addEventListener('hashchange', openAccordionForHash);

  // Highlight active nav link based on current pathname
  const path = window.location.pathname.split('?')[0];
  document.querySelectorAll('.sidebar-link').forEach(a => {
    const href = (a.getAttribute('href') || '').split('?')[0];
    if (href && href !== '/' && path.startsWith(href)) {
      a.classList.add('active');
    }
  });

  // Keyboard shortcut: Ctrl+B = toggle sidebar
  document.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'b') {
      e.preventDefault();
      toggleSidebar();
    }
    // Escape = close mobile sidebar
    if (e.key === 'Escape') {
      closeMobileSidebar();
    }
  });

  // Intercept forms with data-confirm and replace window.confirm
  document.querySelectorAll('form[onsubmit]').forEach(form => {
    const attr = form.getAttribute('onsubmit') || '';
    const match = attr.match(/confirm\(['"](.+)['"]\)/);
    if (!match) return;
    const msg = match[1];
    form.removeAttribute('onsubmit');
    const onConfirmSubmit = e => {
      e.preventDefault();
      showConfirmDialog(msg, () => {
        form.removeEventListener('submit', onConfirmSubmit);
        form.submit();
      });
    };
    form.addEventListener('submit', onConfirmSubmit);
  });
});

// ── Confirm dialog (replaces browser alert/confirm) ─────────────────────────
function showConfirmDialog(message, onConfirm, onCancel) {
  const existing = document.getElementById('db-confirm-overlay');
  if (existing) existing.remove();

  const overlay = document.createElement('div');
  overlay.id = 'db-confirm-overlay';
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:9998;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(3px);animation:toast-in 0.18s ease;';
  overlay.innerHTML = `
    <div style="background:linear-gradient(180deg,#111e33,#0c1728);border:1px solid rgba(148,163,184,0.18);border-radius:1rem;padding:1.5rem;max-width:380px;width:90%;box-shadow:0 24px 60px rgba(0,0,0,0.6);">
      <div style="display:flex;align-items:center;gap:0.75rem;margin-bottom:1rem;">
        <div style="width:2rem;height:2rem;background:rgba(245,158,11,0.15);border-radius:0.5rem;display:flex;align-items:center;justify-content:center;flex-shrink:0;">
          <i class="fa fa-triangle-exclamation" style="color:#fbbf24;font-size:0.85rem;"></i>
        </div>
        <p style="color:#e5eefc;font-size:0.9rem;font-weight:500;line-height:1.4;">${message}</p>
      </div>
      <div style="display:flex;gap:0.75rem;justify-content:flex-end;">
        <button id="db-confirm-cancel" class="btn-secondary" style="font-size:0.8rem;padding:0.45rem 0.9rem;">Cancel</button>
        <button id="db-confirm-ok" class="btn-danger" style="font-size:0.8rem;padding:0.45rem 0.9rem;">Confirm</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);

  const close = () => overlay.remove();
  overlay.querySelector('#db-confirm-cancel').addEventListener('click', () => { close(); if (onCancel) onCancel(); });
  overlay.querySelector('#db-confirm-ok').addEventListener('click', () => { close(); if (onConfirm) onConfirm(); });
  overlay.addEventListener('click', e => { if (e.target === overlay) { close(); if (onCancel) onCancel(); } });
  overlay.querySelector('#db-confirm-ok').focus();
}

window.showConfirmDialog = showConfirmDialog;

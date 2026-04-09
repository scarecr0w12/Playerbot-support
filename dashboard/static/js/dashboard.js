// Dashboard JS helpers

// Confirm-before-submit is handled inline via onsubmit="return confirm(...)"
// This file handles small UI enhancements.

document.addEventListener('DOMContentLoaded', () => {
  // Auto-dismiss flash messages after 4 seconds
  document.querySelectorAll('.flash-msg').forEach(el => {
    setTimeout(() => el.remove(), 4000);
  });

  document.querySelectorAll('[data-copy-text]').forEach(button => {
    button.addEventListener('click', async event => {
      event.preventDefault();
      event.stopPropagation();
      const text = button.getAttribute('data-copy-text') || '';
      const label = button.getAttribute('data-copy-label') || 'Value';
      if (!text) {
        return;
      }
      try {
        await navigator.clipboard.writeText(text);
        const original = button.innerHTML;
        button.innerHTML = '<i class="fa fa-check"></i> Copied';
        button.setAttribute('aria-label', `${label} copied`);
        setTimeout(() => {
          button.innerHTML = original;
          button.setAttribute('aria-label', `Copy ${label}`);
        }, 1400);
      } catch {
        button.setAttribute('title', 'Copy failed');
      }
    });
  });

  const openAccordionForHash = () => {
    const hash = window.location.hash;
    if (!hash) {
      return;
    }
    const target = document.querySelector(hash);
    if (!target) {
      return;
    }
    const details = target.closest('details');
    if (details) {
      details.open = true;
    }
  };

  openAccordionForHash();
  window.addEventListener('hashchange', openAccordionForHash);

  // Highlight active nav link based on pathname
  const path = window.location.pathname.split('?')[0];
  document.querySelectorAll('.sidebar-link').forEach(a => {
    const href = a.getAttribute('href').split('?')[0];
    if (href !== '/' && path.startsWith(href)) {
      a.classList.add('active');
    }
  });
});

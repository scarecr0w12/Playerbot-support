// Dashboard JS helpers

// Confirm-before-submit is handled inline via onsubmit="return confirm(...)"
// This file handles small UI enhancements.

document.addEventListener('DOMContentLoaded', () => {
  // Auto-dismiss flash messages after 4 seconds
  document.querySelectorAll('.flash-msg').forEach(el => {
    setTimeout(() => el.remove(), 4000);
  });

  // Highlight active nav link based on pathname
  const path = window.location.pathname.split('?')[0];
  document.querySelectorAll('.sidebar-link').forEach(a => {
    const href = a.getAttribute('href').split('?')[0];
    if (href !== '/' && path.startsWith(href)) {
      a.classList.add('active');
    }
  });
});

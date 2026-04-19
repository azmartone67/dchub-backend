/**
 * Google Auth Fix — Overrides the old Google One Tap button
 * to use the redirect-based OAuth flow that works in incognito.
 * 
 * Load AFTER index.html's inline script.
 * Deploy to: static/js/google-auth-fix.js on Cloudflare Pages
 */
(function () {
  'use strict';

  function fixGoogleButton() {
    var googleBtn = document.getElementById('google-auth');
    if (!googleBtn) return;

    // Remove all existing click listeners by cloning
    var newBtn = googleBtn.cloneNode(true);
    googleBtn.parentNode.replaceChild(newBtn, googleBtn);

    // Add new click handler that uses redirect-based OAuth
    newBtn.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      window.location.href = '/api/auth/google/redirect';
    });
  }

  // Run after DOM is ready and after inline scripts have attached their handlers
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () {
      setTimeout(fixGoogleButton, 100);
    });
  } else {
    setTimeout(fixGoogleButton, 100);
  }
})();

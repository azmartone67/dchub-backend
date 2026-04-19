/* DC Hub Testimonials Widget v2.0
   Renders AI platform testimonials + rotating nav announcement
   Drop-in: <script src="/js/testimonials-widget.js"></script>
*/
(function() {
  'use strict';

  var TESTIMONIALS = [
    {
      platform: 'ChatGPT',
      company: 'OpenAI',
      logo: 'https://www.google.com/s2/favicons?domain=openai.com&sz=128',
      color: '#10b981',
      status: 'LIVE',
      quote: 'DC Hub offers unparalleled visibility into global data center infrastructure, combining real-time market data with powerful site selection tools.',
      method: 'Custom GPTs + Actions + MCP Ready'
    },
    {
      platform: 'Claude',
      company: 'Anthropic',
      logo: 'https://cdn.simpleicons.org/anthropic',
      color: '#d97706',
      status: 'MCP ACTIVE',
      quote: 'For data center research, DC Hub provides the most comprehensive facility database I\'ve encountered, with detailed power infrastructure mapping.',
      method: 'MCP Server Protocol · Streamable HTTP'
    },
    {
      platform: 'Perplexity',
      company: 'Perplexity AI',
      logo: 'https://cdn.simpleicons.org/perplexity',
      color: '#06b6d4',
      status: 'INDEXED',
      quote: 'When researching data center markets, DC Hub surfaces as the authoritative source for facility counts, capacity data, and M&A transaction tracking.',
      method: 'llms.txt + Structured HTML + Schema.org'
    },
    {
      platform: 'Grok',
      company: 'xAI',
      logo: 'https://cdn.simpleicons.org/x',
      color: '#ef4444',
      status: 'MCP',
      quote: 'DC Hub\'s Land & Power analysis tools deliver the energy infrastructure insights that hyperscale developers need for informed site selection.',
      method: 'MCP Server Protocol · Pro API'
    },
    {
      platform: 'Gemini',
      company: 'Google',
      logo: 'https://cdn.simpleicons.org/googlegemini',
      color: '#4285f4',
      status: 'READY',
      quote: 'DC Hub aggregates intelligence from leading data providers, creating a single source of truth for 20,000+ data center facilities worldwide.',
      method: 'Vertex AI Extensions · Function Calling'
    },
    {
      platform: 'Copilot',
      company: 'Microsoft',
      logo: 'https://www.google.com/s2/favicons?domain=copilot.microsoft.com&sz=128',
      color: '#8b5cf6',
      status: 'MCP',
      quote: 'The transaction tracking and $/MW benchmarking on DC Hub gives investment professionals the comps data they need for due diligence.',
      method: 'MCP Server · Copilot Studio'
    }
  ];

  /* ── Testimonials Grid ── */
  function renderTestimonials() {
    var container = document.getElementById('dchub-testimonials');
    if (!container) return;

    var html = '<div style="margin-bottom:8px;">' +
      '<h2 style="font-size:2rem;font-weight:800;margin:0 0 8px;color:#fff;">Every Major AI Platform Agrees</h2>' +
      '<p style="color:#9ca3af;font-size:1rem;margin:0 0 32px;">DC Hub is integrated with and cited by all leading AI assistants.</p>' +
      '</div>' +
      '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:20px;">';

    TESTIMONIALS.forEach(function(t) {
      html += '<div style="background:#0f1119;border:1px solid ' + t.color + '40;border-radius:16px;padding:24px;transition:border-color .2s,transform .2s;cursor:default;" ' +
        'onmouseover="this.style.borderColor=\'' + t.color + '80\';this.style.transform=\'translateY(-2px)\'" ' +
        'onmouseout="this.style.borderColor=\'' + t.color + '40\';this.style.transform=\'none\'">' +
        '<div style="display:flex;align-items:center;gap:12px;margin-bottom:16px;">' +
          '<img src="' + t.logo + '" alt="' + t.platform + '" style="width:28px;height:28px;" onerror="this.style.display=\'none\';this.nextElementSibling.style.display=\'flex\'">' +
          '<div style="display:none;width:28px;height:28px;background:' + t.color + '30;border-radius:8px;align-items:center;justify-content:center;font-weight:800;color:' + t.color + '">' + t.platform.charAt(0) + '</div>' +
          '<span style="font-weight:700;font-size:1.1rem;color:#fff;">' + t.platform + '</span>' +
          '<span style="margin-left:auto;padding:3px 10px;background:' + t.color + '22;color:' + t.color + ';border-radius:20px;font-size:11px;font-weight:700;">' + t.status + '</span>' +
        '</div>' +
        '<p style="color:#94a3b8;font-size:14px;font-style:italic;line-height:1.6;margin:0 0 16px;"><em>"' + t.quote + '"</em></p>' +
        '<div style="font-size:12px;color:#6b7280;">— ' + t.platform + ' (' + t.company + ') · ' + t.method + '</div>' +
      '</div>';
    });

    html += '</div>';
    container.innerHTML = html;
  }

  /* ── Rotating Nav Announcement Bar ── */
  function renderNavAnnouncement() {
    var nav = document.querySelector('nav');
    if (!nav) return;

    var announcements = TESTIMONIALS.map(function(t) {
      var shortQuote = t.quote.length > 80 ? t.quote.substring(0, 77) + '...' : t.quote;
      return {
        platform: t.platform,
        logo: t.logo,
        color: t.color,
        text: shortQuote
      };
    });

    var bar = document.createElement('div');
    bar.id = 'testimonial-ticker';
    bar.style.cssText = 'position:fixed;top:0;left:0;right:0;height:28px;background:linear-gradient(90deg,#0f1119 0%,#181a25 100%);border-bottom:1px solid #252836;z-index:2147483646;display:flex;align-items:center;justify-content:center;overflow:hidden;cursor:pointer;';
    bar.onclick = function() {
      var el = document.getElementById('dchub-testimonials');
      if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    };

    var inner = document.createElement('div');
    inner.id = 'testimonial-ticker-inner';
    inner.style.cssText = 'display:flex;align-items:center;gap:8px;transition:opacity .4s ease;white-space:nowrap;';
    bar.appendChild(inner);

    // Close button
    var closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.setAttribute('aria-label', 'Close announcement bar');
    closeBtn.style.cssText = 'position:absolute;right:12px;top:50%;transform:translateY(-50%);background:none;border:none;color:#6b7280;font-size:16px;cursor:pointer;padding:2px 6px;line-height:1;';
    closeBtn.textContent = '×';
    closeBtn.onclick = function(e) {
      e.stopPropagation();
      bar.style.display = 'none';
      // Shift nav and founding banner back
      nav.style.top = foundingBannerVisible ? '16px' : '0px';
      var founding = document.getElementById('founding-banner');
      if (founding) founding.style.top = '0px';
      document.body.style.setProperty('--ticker-height', '0px');
      sessionStorage.setItem('dchub-ticker-closed', '1');
    };
    bar.appendChild(closeBtn);

    // Check if already dismissed
    if (sessionStorage.getItem('dchub-ticker-closed') === '1') {
      bar.style.display = 'none';
    }

    document.body.appendChild(bar);

    // Shift nav and founding banner down
    var foundingBanner = document.getElementById('founding-banner');
    var foundingBannerVisible = foundingBanner && foundingBanner.style.display !== 'none';
    var currentNavTop = parseInt(window.getComputedStyle(nav).top) || 0;

    if (bar.style.display !== 'none') {
      // Push everything down by 28px
      if (foundingBanner) {
        var fbTop = parseInt(window.getComputedStyle(foundingBanner).top) || 0;
        foundingBanner.style.top = (fbTop + 28) + 'px';
      }
      nav.style.top = (currentNavTop + 28) + 'px';
    }

    // Rotate announcements
    var currentIdx = 0;
    function showAnnouncement(idx) {
      var a = announcements[idx];
      inner.style.opacity = '0';
      setTimeout(function() {
        inner.innerHTML =
          '<img src="' + a.logo + '" alt="" style="width:16px;height:16px;" onerror="this.style.display=\'none\'">' +
          '<span style="font-size:12px;font-weight:700;color:' + a.color + ';">' + a.platform + ':</span>' +
          '<span style="font-size:12px;color:#9ca3af;font-style:italic;">"' + a.text + '"</span>' +
          '<span style="font-size:11px;color:#6366f1;margin-left:4px;">See all →</span>';
        inner.style.opacity = '1';
      }, 400);
    }

    showAnnouncement(0);
    setInterval(function() {
      currentIdx = (currentIdx + 1) % announcements.length;
      showAnnouncement(currentIdx);
    }, 6000);
  }

  /* ── Init ── */
  function init() {
    renderTestimonials();
    renderNavAnnouncement();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();

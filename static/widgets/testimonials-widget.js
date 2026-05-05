/**
 * DC Hub Testimonials Widget
 * Embeddable carousel widget showing AI platform testimonials
 * 
 * Usage:
 * <div id="dchub-testimonials"></div>
 * <script src="https://dchub.cloud/static/widgets/testimonials-widget.js"></script>
 */
(function() {
  const API_BASE = 'https://dchub.cloud';
  const WIDGET_ID = 'dchub-testimonials';
  
  const GOOGLE_ICON = `<svg viewBox="0 0 24 24" fill="none" width="20" height="20">
    <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 01-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1z" fill="#4285F4"/>
    <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853"/>
    <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" fill="#FBBC05"/>
    <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335"/>
  </svg>`;

  const OPENAI_ICON = `<svg viewBox="0 0 24 24" fill="#10a37f" width="20" height="20">
    <path d="M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944zm-9.6607-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.142.0852 4.783 2.7582a.7712.7712 0 0 0 .7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1-.0332.0615L9.74 19.9502a4.4992 4.4992 0 0 1-6.1408-1.6464zM2.3408 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 0 0 0 .3879.6765l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865A4.504 4.504 0 0 1 2.3408 7.8956zm16.0993 3.8558L12.6 8.3829l2.02-1.1638a.0757.0757 0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772a.79.79 0 0 0-.407-.667zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759.7759 0 0 0-.7854 0L9.409 9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 4.66zM8.3065 12.863l-2.02-1.1638a.0804.0804 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 7.3757-3.4537l-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654l2.602-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997z"/>
  </svg>`;

  const ANTHROPIC_ICON = `<svg viewBox="0 0 24 24" fill="#cc785c" width="20" height="20">
    <path d="M17.304 3.541h-3.672l6.696 16.918h3.672L17.304 3.541zM6.696 3.541L0 20.459h3.744l1.308-3.42h6.624l1.308 3.42h3.744L10.032 3.541H6.696zm.456 10.716l2.208-5.784 2.208 5.784H7.152z"/>
  </svg>`;

  const styles = `
    @import url('https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,700;1,400&family=DM+Sans:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap');
    
    .dchub-widget {
      --gold: #c9a84c;
      --gold-light: #e8d48b;
      --bg-deep: #0a0b0f;
      --bg-card: #111318;
      --text-primary: #e8e6e1;
      --text-secondary: #9a978f;
      --border: rgba(201, 168, 76, 0.12);
      --border-strong: rgba(201, 168, 76, 0.25);
      --google-blue: #4285f4;
      --openai-green: #10a37f;
      --anthropic-orange: #cc785c;
      
      font-family: 'DM Sans', -apple-system, BlinkMacSystemFont, sans-serif;
      background: var(--bg-deep);
      border-radius: 20px;
      padding: 40px;
      position: relative;
      overflow: hidden;
    }
    
    .dchub-widget * { box-sizing: border-box; margin: 0; padding: 0; }
    
    .dchub-widget::before {
      content: '';
      position: absolute;
      top: 0;
      left: 50%;
      transform: translateX(-50%);
      width: 400px;
      height: 300px;
      background: radial-gradient(ellipse at center, rgba(201, 168, 76, 0.06) 0%, transparent 70%);
      pointer-events: none;
    }
    
    .dchub-header {
      text-align: center;
      margin-bottom: 32px;
      position: relative;
      z-index: 1;
    }
    
    .dchub-header h2 {
      font-family: 'Playfair Display', serif;
      font-size: 28px;
      font-weight: 700;
      color: var(--text-primary);
      margin-bottom: 8px;
    }
    
    .dchub-header h2 em {
      color: var(--gold-light);
      font-style: italic;
    }
    
    .dchub-header p {
      font-size: 14px;
      color: var(--text-secondary);
    }
    
    .dchub-carousel {
      position: relative;
      overflow: hidden;
    }
    
    .dchub-slides {
      display: flex;
      transition: transform 0.5s cubic-bezier(0.16, 1, 0.3, 1);
    }
    
    .dchub-slide {
      flex: 0 0 100%;
      padding: 0 20px;
    }
    
    .dchub-testimonial-card {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 32px;
      position: relative;
      transition: all 0.3s ease;
    }
    
    .dchub-testimonial-card:hover {
      border-color: var(--border-strong);
      box-shadow: 0 10px 40px rgba(0,0,0,0.3);
    }
    
    .dchub-testimonial-card::before {
      content: '';
      position: absolute;
      top: 0;
      left: 20%;
      right: 20%;
      height: 2px;
      background: linear-gradient(90deg, transparent, var(--gold), transparent);
    }
    
    .dchub-source {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 6px 14px 6px 10px;
      background: rgba(66, 133, 244, 0.08);
      border: 1px solid rgba(66, 133, 244, 0.2);
      border-radius: 100px;
      font-size: 12px;
      font-weight: 500;
      color: var(--google-blue);
      margin-bottom: 20px;
    }
    
    .dchub-source.openai {
      background: rgba(16, 163, 127, 0.08);
      border-color: rgba(16, 163, 127, 0.2);
      color: var(--openai-green);
    }
    
    .dchub-source.anthropic {
      background: rgba(204, 120, 92, 0.08);
      border-color: rgba(204, 120, 92, 0.2);
      color: var(--anthropic-orange);
    }
    
    .dchub-quote {
      font-family: 'Playfair Display', serif;
      font-size: 20px;
      line-height: 1.5;
      color: var(--text-primary);
      margin-bottom: 20px;
      padding-left: 20px;
      border-left: 2px solid var(--gold);
    }
    
    .dchub-quote em {
      color: var(--gold-light);
      font-style: italic;
    }
    
    .dchub-attribution {
      font-size: 13px;
      color: var(--text-secondary);
      padding-left: 20px;
    }
    
    .dchub-attribution span {
      color: var(--google-blue);
    }
    
    .dchub-nav {
      display: flex;
      justify-content: center;
      gap: 12px;
      margin-top: 24px;
    }
    
    .dchub-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--border);
      border: none;
      cursor: pointer;
      transition: all 0.3s ease;
      padding: 0;
    }
    
    .dchub-dot.active {
      background: var(--gold);
      transform: scale(1.3);
    }
    
    .dchub-dot:hover:not(.active) {
      background: var(--border-strong);
    }
    
    .dchub-arrows {
      position: absolute;
      top: 50%;
      left: 0;
      right: 0;
      transform: translateY(-50%);
      display: flex;
      justify-content: space-between;
      pointer-events: none;
      padding: 0 8px;
    }
    
    .dchub-arrow {
      width: 36px;
      height: 36px;
      border-radius: 50%;
      background: var(--bg-card);
      border: 1px solid var(--border);
      color: var(--text-secondary);
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      pointer-events: auto;
      transition: all 0.3s ease;
    }
    
    .dchub-arrow:hover {
      background: var(--border-strong);
      color: var(--gold);
    }
    
    .dchub-footer {
      text-align: center;
      margin-top: 28px;
      padding-top: 20px;
      border-top: 1px solid var(--border);
    }
    
    .dchub-cta {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 12px 24px;
      background: linear-gradient(135deg, var(--gold), #a8862e);
      color: #0a0b0f;
      text-decoration: none;
      font-weight: 600;
      font-size: 14px;
      border-radius: 100px;
      transition: all 0.3s ease;
    }
    
    .dchub-cta:hover {
      transform: translateY(-2px);
      box-shadow: 0 8px 24px rgba(201, 168, 76, 0.3);
    }
    
    .dchub-powered {
      font-size: 11px;
      color: var(--text-secondary);
      margin-top: 12px;
    }
    
    .dchub-powered a {
      color: var(--gold);
      text-decoration: none;
    }
  `;

  function getIcon(source) {
    if (source === 'openai') return OPENAI_ICON;
    if (source === 'anthropic') return ANTHROPIC_ICON;
    return GOOGLE_ICON;
  }

  function getSourceClass(source) {
    if (source === 'openai') return 'openai';
    if (source === 'anthropic') return 'anthropic';
    return '';
  }

  function renderWidget(testimonials) {
    const container = document.getElementById(WIDGET_ID);
    if (!container) return;

    const top3 = testimonials.slice(0, 3);
    
    const slidesHtml = top3.map((t, i) => `
      <div class="dchub-slide">
        <div class="dchub-testimonial-card">
          <div class="dchub-source ${getSourceClass(t.source_icon)}">
            ${getIcon(t.source_icon)}
            ${t.source_type}
          </div>
          <blockquote class="dchub-quote">${t.hero_quote.replace(/"/g, '<em>"').replace(/"/g, '"</em>')}</blockquote>
          <div class="dchub-attribution">— <span>${t.source}</span></div>
        </div>
      </div>
    `).join('');

    const dotsHtml = top3.map((_, i) => 
      `<button class="dchub-dot ${i === 0 ? 'active' : ''}" data-index="${i}"></button>`
    ).join('');

    container.innerHTML = `
      <style>${styles}</style>
      <div class="dchub-widget">
        <div class="dchub-header">
          <h2>The Source <em>AI Agents Cite</em></h2>
          <p>What leading AI platforms say about DC Hub</p>
        </div>
        
        <div class="dchub-carousel">
          <div class="dchub-slides">
            ${slidesHtml}
          </div>
          <div class="dchub-arrows">
            <button class="dchub-arrow dchub-prev">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <polyline points="15,18 9,12 15,6"></polyline>
              </svg>
            </button>
            <button class="dchub-arrow dchub-next">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <polyline points="9,18 15,12 9,6"></polyline>
              </svg>
            </button>
          </div>
        </div>
        
        <div class="dchub-nav">${dotsHtml}</div>
        
        <div class="dchub-footer">
          <a href="https://dchub.cloud/testimonials" target="_blank" class="dchub-cta">
            View All Testimonials
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M7 17L17 7M17 7H7M17 7V17"/>
            </svg>
          </a>
          <p class="dchub-powered">Powered by <a href="https://dchub.cloud" target="_blank">DC Hub</a> — Data Center Intelligence</p>
        </div>
      </div>
    `;

    initCarousel(container, top3.length);
  }

  function initCarousel(container, count) {
    let current = 0;
    const slides = container.querySelector('.dchub-slides');
    const dots = container.querySelectorAll('.dchub-dot');
    const prevBtn = container.querySelector('.dchub-prev');
    const nextBtn = container.querySelector('.dchub-next');

    function goTo(index) {
      current = (index + count) % count;
      slides.style.transform = `translateX(-${current * 100}%)`;
      dots.forEach((d, i) => d.classList.toggle('active', i === current));
    }

    dots.forEach(dot => {
      dot.addEventListener('click', () => goTo(parseInt(dot.dataset.index)));
    });

    prevBtn.addEventListener('click', () => goTo(current - 1));
    nextBtn.addEventListener('click', () => goTo(current + 1));

    setInterval(() => goTo(current + 1), 6000);
  }

  async function init() {
    try {
      const resp = await fetch(`${API_BASE}/api/testimonials`);
      const data = await resp.json();
      renderWidget(data.testimonials || []);
    } catch (err) {
      console.error('DC Hub Widget: Failed to load testimonials', err);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();

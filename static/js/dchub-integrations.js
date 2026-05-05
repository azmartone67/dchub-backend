(function(window) {
  'use strict';

  var API_BASE = 'https://dchub.cloud';

  var DCHub = {
    version: '1.2.0',

    search: function(query, options) {
      options = options || {};
      var params = new URLSearchParams();
      if (query) params.append('q', query);
      if (options.limit) params.append('limit', options.limit);
      if (options.location) params.append('location', options.location);
      if (options.provider) params.append('provider', options.provider);

      return fetch(API_BASE + '/api/v1/search?' + params.toString())
        .then(function(r) { return r.json(); });
    },

    getFacilities: function(options) {
      options = options || {};
      var params = new URLSearchParams();
      if (options.page) params.append('page', options.page);
      if (options.limit) params.append('limit', options.limit);
      if (options.location) params.append('location', options.location);

      return fetch(API_BASE + '/api/v1/facilities?' + params.toString())
        .then(function(r) { return r.json(); });
    },

    getStats: function() {
      return fetch(API_BASE + '/api/v1/stats')
        .then(function(r) { return r.json(); });
    },

    getNews: function(options) {
      options = options || {};
      var params = new URLSearchParams();
      if (options.limit) params.append('limit', options.limit);
      if (options.category) params.append('category', options.category);

      return fetch(API_BASE + '/api/v1/announcements?' + params.toString())
        .then(function(r) { return r.json(); });
    },

    getDeals: function(options) {
      options = options || {};
      var params = new URLSearchParams();
      if (options.limit) params.append('limit', options.limit);
      if (options.type) params.append('type', options.type);

      return fetch(API_BASE + '/api/v1/transactions?' + params.toString())
        .then(function(r) { return r.json(); });
    },

    getMarketStats: function(location) {
      var params = new URLSearchParams();
      if (location) params.append('location', location);

      return fetch(API_BASE + '/api/v1/market-stats?' + params.toString())
        .then(function(r) { return r.json(); });
    },

    analyzeSite: function(lat, lng, options) {
      options = options || {};
      var params = new URLSearchParams();
      params.append('lat', lat);
      params.append('lng', lng);
      if (options.radius) params.append('radius', options.radius);

      return fetch(API_BASE + '/api/v1/energy/site-analysis?' + params.toString())
        .then(function(r) { return r.json(); });
    },

    embed: {
      statsWidget: function(elementId, options) {
        options = options || {};
        var el = document.getElementById(elementId);
        if (!el) return;

        DCHub.getStats().then(function(data) {
          el.innerHTML = '<div style="font-family:system-ui;padding:16px;background:#1a1a2e;color:#fff;border-radius:8px;">' +
            '<div style="font-size:24px;font-weight:bold;color:#00d4ff;">' + (data.total_facilities || 0).toLocaleString() + '</div>' +
            '<div style="font-size:12px;opacity:0.7;">Data Centers Tracked</div>' +
            '<div style="margin-top:12px;font-size:18px;color:#00ff88;">' + (data.total_capacity_mw || 0).toLocaleString() + ' MW</div>' +
            '<div style="font-size:12px;opacity:0.7;">Total Capacity</div>' +
            '<div style="margin-top:8px;font-size:10px;opacity:0.5;">Powered by DC Hub</div></div>';
        });
      },

      newsWidget: function(elementId, options) {
        options = options || {};
        var limit = options.limit || 5;
        var el = document.getElementById(elementId);
        if (!el) return;

        DCHub.getNews({ limit: limit }).then(function(data) {
          var items = data.announcements || data.data || [];
          var html = '<div style="font-family:system-ui;background:#1a1a2e;color:#fff;border-radius:8px;padding:16px;">' +
            '<div style="font-size:14px;font-weight:bold;margin-bottom:12px;">Latest DC News</div>';
          
          items.slice(0, limit).forEach(function(item) {
            html += '<div style="padding:8px 0;border-bottom:1px solid #333;">' +
              '<a href="' + (item.url || '#') + '" target="_blank" style="color:#00d4ff;text-decoration:none;font-size:13px;">' + 
              (item.title || 'Untitled') + '</a>' +
              '<div style="font-size:11px;opacity:0.6;margin-top:4px;">' + (item.source || '') + '</div></div>';
          });
          
          html += '<div style="margin-top:8px;font-size:10px;opacity:0.5;">Powered by DC Hub</div></div>';
          el.innerHTML = html;
        });
      }
    },

    decisionLogger: {
      _modal: null,
      _isOpen: false,

      init: function() {
        var self = this;
        document.addEventListener('keydown', function(e) {
          if (e.ctrlKey && e.shiftKey && e.key === 'D') {
            e.preventDefault();
            self.toggle();
          }
        });
        this._createModal();
      },

      _createModal: function() {
        var modal = document.createElement('div');
        modal.id = 'dchub-decision-modal';
        modal.style.cssText = 'display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.8);z-index:99999;font-family:system-ui;';
        modal.innerHTML = 
          '<div style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);background:#1a1a2e;border-radius:12px;padding:24px;width:400px;max-width:90%;">' +
            '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">' +
              '<h3 style="margin:0;color:#00d4ff;">Log Decision</h3>' +
              '<button id="dchub-close-modal" style="background:none;border:none;color:#fff;font-size:24px;cursor:pointer;">&times;</button>' +
            '</div>' +
            '<input id="dchub-decision-title" placeholder="Decision title..." style="width:100%;padding:12px;margin-bottom:12px;background:#0d0d1a;border:1px solid #333;border-radius:6px;color:#fff;box-sizing:border-box;">' +
            '<textarea id="dchub-decision-desc" placeholder="Description..." style="width:100%;padding:12px;margin-bottom:12px;background:#0d0d1a;border:1px solid #333;border-radius:6px;color:#fff;height:80px;resize:none;box-sizing:border-box;"></textarea>' +
            '<select id="dchub-decision-category" style="width:100%;padding:12px;margin-bottom:12px;background:#0d0d1a;border:1px solid #333;border-radius:6px;color:#fff;">' +
              '<option value="backend">Backend</option>' +
              '<option value="frontend">Frontend</option>' +
              '<option value="infrastructure">Infrastructure</option>' +
              '<option value="business">Business</option>' +
              '<option value="security">Security</option>' +
            '</select>' +
            '<select id="dchub-decision-priority" style="width:100%;padding:12px;margin-bottom:16px;background:#0d0d1a;border:1px solid #333;border-radius:6px;color:#fff;">' +
              '<option value="low">Low Priority</option>' +
              '<option value="medium">Medium Priority</option>' +
              '<option value="high">High Priority</option>' +
            '</select>' +
            '<button id="dchub-submit-decision" style="width:100%;padding:12px;background:#00d4ff;color:#000;border:none;border-radius:6px;font-weight:bold;cursor:pointer;">Log Decision</button>' +
            '<div id="dchub-decision-status" style="margin-top:12px;text-align:center;font-size:13px;"></div>' +
          '</div>';
        document.body.appendChild(modal);
        this._modal = modal;

        var self = this;
        document.getElementById('dchub-close-modal').onclick = function() { self.close(); };
        document.getElementById('dchub-submit-decision').onclick = function() { self.submit(); };
        modal.onclick = function(e) { if (e.target === modal) self.close(); };
      },

      toggle: function() {
        this._isOpen ? this.close() : this.open();
      },

      open: function() {
        this._modal.style.display = 'block';
        this._isOpen = true;
        document.getElementById('dchub-decision-title').focus();
      },

      close: function() {
        this._modal.style.display = 'none';
        this._isOpen = false;
        document.getElementById('dchub-decision-title').value = '';
        document.getElementById('dchub-decision-desc').value = '';
        document.getElementById('dchub-decision-status').textContent = '';
      },

      submit: function() {
        var title = document.getElementById('dchub-decision-title').value.trim();
        var desc = document.getElementById('dchub-decision-desc').value.trim();
        var category = document.getElementById('dchub-decision-category').value;
        var priority = document.getElementById('dchub-decision-priority').value;
        var status = document.getElementById('dchub-decision-status');

        if (!title) {
          status.style.color = '#ff4444';
          status.textContent = 'Please enter a title';
          return;
        }

        status.style.color = '#ffaa00';
        status.textContent = 'Logging...';

        fetch(API_BASE + '/api/v1/decisions', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            title: title,
            description: desc,
            category: category,
            priority: priority,
            source: 'integration'
          })
        })
        .then(function(r) { return r.json(); })
        .then(function(data) {
          if (data.success || data.id) {
            status.style.color = '#00ff88';
            status.textContent = 'Decision logged!';
            setTimeout(function() { DCHub.decisionLogger.close(); }, 1000);
          } else {
            status.style.color = '#ff4444';
            status.textContent = data.error || 'Failed to log';
          }
        })
        .catch(function(err) {
          status.style.color = '#ff4444';
          status.textContent = 'Error: ' + err.message;
        });
      }
    },

    aiReadyBadge: {
      _badge: null,
      _isReady: false,

      init: function() {
        this._createBadge();
        this._checkMCP();
        var self = this;
        setInterval(function() { self._checkMCP(); }, 30000);
      },

      _createBadge: function() {
        var style = document.createElement('style');
        style.textContent = '@keyframes dchub-pulse{0%,100%{box-shadow:0 0 0 0 rgba(0,255,136,0.7);}70%{box-shadow:0 0 0 10px rgba(0,255,136,0);}}';
        document.head.appendChild(style);

        var badge = document.createElement('div');
        badge.id = 'dchub-ai-badge';
        badge.style.cssText = 'position:fixed;bottom:20px;left:20px;padding:8px 16px;background:#1a1a2e;border:2px solid #333;border-radius:20px;font-family:system-ui;font-size:12px;color:#888;z-index:99998;cursor:pointer;transition:all 0.3s;';
        badge.innerHTML = '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#888;margin-right:8px;"></span>Checking AI...';
        badge.onclick = function() {
          window.open(API_BASE + '/mcp/manifest', '_blank');
        };
        document.body.appendChild(badge);
        this._badge = badge;
      },

      _checkMCP: function() {
        var self = this;
        fetch(API_BASE + '/.well-known/mcp.json')
          .then(function(r) { return r.json(); })
          .then(function(data) {
            if (data.mcp_server) {
              self._setReady(true);
            } else {
              self._setReady(false);
            }
          })
          .catch(function() {
            self._setReady(false);
          });
      },

      _setReady: function(ready) {
        this._isReady = ready;
        var dot = this._badge.querySelector('span');
        if (ready) {
          this._badge.style.borderColor = '#00ff88';
          this._badge.style.color = '#00ff88';
          dot.style.background = '#00ff88';
          dot.style.animation = 'dchub-pulse 2s infinite';
          this._badge.innerHTML = '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#00ff88;margin-right:8px;animation:dchub-pulse 2s infinite;"></span>AI Ready';
        } else {
          this._badge.style.borderColor = '#ff4444';
          this._badge.style.color = '#ff4444';
          dot.style.background = '#ff4444';
          dot.style.animation = 'none';
          this._badge.innerHTML = '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#ff4444;margin-right:8px;"></span>AI Offline';
        }
      }
    },

    autoImageMatch: {
      _processed: new Set(),

      init: function() {
        this.scan();
        var self = this;
        var observer = new MutationObserver(function() { self.scan(); });
        observer.observe(document.body, { childList: true, subtree: true });
      },

      scan: function() {
        var cards = document.querySelectorAll('.news-item');
        var self = this;
        cards.forEach(function(card) {
          if (self._processed.has(card)) return;
          self._processed.add(card);
          self._matchImage(card);
        });
      },

      _matchImage: function(card) {
        var headline = card.querySelector('h1, h2, h3, h4, .headline, .title');
        var img = card.querySelector('img');
        
        if (!headline || !img) return;
        
        var title = headline.textContent.trim();
        if (!title) return;

        fetch(API_BASE + '/api/v1/images/match', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title: title })
        })
        .then(function(r) { return r.json(); })
        .then(function(data) {
          if (data.image && data.image.url) {
            img.src = data.image.url;
            img.alt = title;
          } else if (data.alternatives && data.alternatives.length > 0) {
            img.src = data.alternatives[0].url;
            img.alt = title;
          }
        })
        .catch(function() {});
      }
    },

    autoStatsRefresh: {
      _interval: null,

      init: function() {
        this.refresh();
        var self = this;
        this._interval = setInterval(function() { self.refresh(); }, 60000);
      },

      refresh: function() {
        DCHub.getStats().then(function(data) {
          var statElements = document.querySelectorAll('.quick-stat-value');
          statElements.forEach(function(el) {
            var text = el.textContent.toLowerCase();
            var parent = el.closest('.quick-stat');
            var label = parent ? parent.textContent.toLowerCase() : '';
            
            if (label.includes('facilit') || label.includes('data center')) {
              if (data.total_facilities) el.textContent = data.total_facilities.toLocaleString();
            } else if (label.includes('power') || label.includes('mw') || label.includes('capacity')) {
              if (data.total_capacity_mw) el.textContent = data.total_capacity_mw.toLocaleString();
            } else if (label.includes('news') || label.includes('article')) {
              if (data.total_announcements) el.textContent = data.total_announcements.toLocaleString();
            } else if (label.includes('provider') || label.includes('operator')) {
              if (data.total_providers) el.textContent = data.total_providers.toLocaleString();
            } else if (label.includes('market') || label.includes('countr')) {
              if (data.total_countries) el.textContent = data.total_countries.toLocaleString();
            }
          });
        }).catch(function() {});
      }
    },

    relativeTime: {
      init: function() {
        this.format();
        var self = this;
        var observer = new MutationObserver(function() { self.format(); });
        observer.observe(document.body, { childList: true, subtree: true });
        setInterval(function() { self.format(); }, 60000);
      },

      format: function() {
        var elements = document.querySelectorAll('[data-timestamp], .news-date, .timestamp');
        var self = this;
        elements.forEach(function(el) {
          if (el.dataset.formatted) return;
          var timestamp = el.dataset.timestamp || el.textContent;
          var date = new Date(timestamp);
          if (!isNaN(date.getTime())) {
            el.textContent = self._toRelative(date);
            el.title = date.toLocaleString();
            el.dataset.formatted = 'true';
          }
        });
      },

      _toRelative: function(date) {
        var now = new Date();
        var diff = Math.floor((now - date) / 1000);
        
        if (diff < 60) return 'just now';
        if (diff < 3600) return Math.floor(diff / 60) + ' min ago';
        if (diff < 86400) return Math.floor(diff / 3600) + ' hours ago';
        if (diff < 604800) return Math.floor(diff / 86400) + ' days ago';
        return date.toLocaleDateString();
      }
    },

    smoothScroll: {
      init: function() {
        document.querySelectorAll('a[href^="#"]').forEach(function(anchor) {
          anchor.addEventListener('click', function(e) {
            var targetId = this.getAttribute('href');
            if (targetId === '#') return;
            var target = document.querySelector(targetId);
            if (target) {
              e.preventDefault();
              target.scrollIntoView({ behavior: 'smooth', block: 'start' });
              history.pushState(null, '', targetId);
            }
          });
        });
      }
    },

    clickTracking: {
      init: function() {
        document.addEventListener('click', function(e) {
          var newsItem = e.target.closest('.news-item');
          var facilityCard = e.target.closest('.facility-card, .provider-card, .market-card');
          
          if (newsItem) {
            var headline = newsItem.querySelector('h1, h2, h3, h4, .headline, .title, a');
            var link = newsItem.querySelector('a[href]');
            var title = headline ? headline.textContent.trim().substring(0, 100) : 'Unknown';
            var url = link ? link.href : '';
            fetch(API_BASE + '/api/track/visit', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ event: 'news_click', title: title, url: url })
            }).catch(function() {});
          }
          
          if (facilityCard) {
            var name = facilityCard.querySelector('h3, h4, .facility-name, .provider-name, .market-name, .name');
            if (name) {
              fetch(API_BASE + '/api/track/visit', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ event: 'facility_click', name: name.textContent.trim() })
              }).catch(function() {});
            }
          }
        });
      }
    },

    toast: {
      _container: null,

      init: function() {
        var container = document.createElement('div');
        container.id = 'dchub-toast-container';
        container.style.cssText = 'position:fixed;bottom:80px;right:20px;z-index:99997;display:flex;flex-direction:column;gap:8px;';
        document.body.appendChild(container);
        this._container = container;
      },

      show: function(message, type) {
        type = type || 'info';
        var colors = { info: '#6366f1', success: '#10b981', error: '#ef4444', warning: '#f59e0b' };
        var toast = document.createElement('div');
        toast.style.cssText = 'padding:12px 20px;background:' + colors[type] + ';color:#fff;border-radius:8px;font-family:system-ui;font-size:14px;box-shadow:0 4px 12px rgba(0,0,0,0.3);animation:dchub-slide-in 0.3s ease;';
        toast.textContent = message;
        this._container.appendChild(toast);
        
        setTimeout(function() {
          toast.style.opacity = '0';
          toast.style.transition = 'opacity 0.3s';
          setTimeout(function() { toast.remove(); }, 300);
        }, 3000);
      }
    },

    copyCoordinates: {
      init: function() {
        var self = this;
        document.addEventListener('click', function(e) {
          var coordEl = e.target.closest('[data-lat][data-lng], .coordinates');
          if (coordEl) {
            var lat = coordEl.dataset.lat || coordEl.textContent.split(',')[0];
            var lng = coordEl.dataset.lng || coordEl.textContent.split(',')[1];
            if (lat && lng) {
              navigator.clipboard.writeText(lat.trim() + ', ' + lng.trim()).then(function() {
                DCHub.toast.show('Coordinates copied!', 'success');
              });
            }
          }
        });
      }
    },

    init: function(options) {
      options = options || {};
      
      if (options.decisionLogger !== false) {
        this.decisionLogger.init();
      }
      if (options.aiReadyBadge !== false) {
        this.aiReadyBadge.init();
      }
      if (options.autoImageMatch !== false) {
        this.autoImageMatch.init();
      }
      if (options.autoStatsRefresh !== false) {
        this.autoStatsRefresh.init();
      }
      if (options.relativeTime === true) {
        this.relativeTime.init();
      }
      if (options.smoothScroll !== false) {
        this.smoothScroll.init();
      }
      if (options.clickTracking !== false) {
        this.clickTracking.init();
      }
      if (options.toast !== false) {
        this.toast.init();
      }
      if (options.copyCoordinates === true) {
        this.copyCoordinates.init();
      }
      
      console.log('DCHub Integrations v1.2.0 initialized');
    }
  };

  window.DCHub = DCHub;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function() { DCHub.init(); });
  } else {
    DCHub.init();
  }

})(window);

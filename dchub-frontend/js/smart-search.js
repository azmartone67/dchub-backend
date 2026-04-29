/**
 * DC Hub Smart Search v1.0
 * Natural-language facility search — intercepts operator/keyword queries,
 * calls /api/search/facilities, shows a live dropdown with results.
 * Pure address/lat-lng queries pass through to existing handlers.
 */
(function () {
    'use strict';

    var API_ENDPOINT = '/api/search/facilities';
    var MIN_CHARS = 3;
    var DEBOUNCE_MS = 380;

    var OPERATOR_RE = /amazon|\baws\b|\bgoogle\b|microsoft|\bmeta\b|equinix|digital.?realty|cologix|iron.?mountain|cyrusone|\bqts\b|\bswitch\b|coreweave|\boracle\b|\bnvidia\b|aligned|compass|vantage|edgeconnex|stack|colo/i;
    var KEYWORD_RE  = /hyperscale|colocation|\bcolo\b|data.?cent|\bmw\b|\bgw\b|campus|tier.?[1-4]|\bai\b.*cluster|cluster.*\bai\b/i;
    var UTILITY_RE  = /\baep\b|dominion|\bpjm\b|\bercot\b|\bmiso\b|\bcaiso\b|\bspp\b|\biso\b|\brto\b|appalachian.power|indiana.michigan/i;
    var LATLON_RE   = /^-?\d{1,3}(\.|\d)\d*\s*,/;

    function isFacilityQuery(q) {
        if (LATLON_RE.test(q.trim())) return false;
        return OPERATOR_RE.test(q) || KEYWORD_RE.test(q) || UTILITY_RE.test(q);
    }

    var debounceTimer = null;
    var dropdown = null;
    var activeResults = [];
    var activeIdx = -1;

    function debounce(fn, ms) {
        return function (q, inp) {
            clearTimeout(debounceTimer);
            debounceTimer = setTimeout(function () { fn(q, inp); }, ms);
        };
    }

    function statusStyle(status) {
        if (!status) return { bg: '#f5f5f5', fg: '#666' };
        var s = status.toLowerCase();
        if (s === 'operational') return { bg: '#EAF3DE', fg: '#3B6D11' };
        if (s.indexOf('construct') > -1) return { bg: '#E6F1FB', fg: '#0C447C' };
        if (s === 'planned') return { bg: '#FAEEDA', fg: '#633806' };
        return { bg: '#f5f5f5', fg: '#555' };
    }

    function openDropdown(inp) {
        closeDropdown();
        var dd = document.createElement('div');
        dd.id = 'dc-smart-dd';
        var rect = inp.getBoundingClientRect();
        dd.style.cssText = [
            'position:fixed',
            'z-index:100000',
            'background:#fff',
            'border:1px solid #d0d0d0',
            'border-radius:10px',
            'box-shadow:0 6px 20px rgba(0,0,0,.18)',
            'width:' + Math.max(rect.width, 340) + 'px',
            'max-height:360px',
            'overflow-y:auto',
            'left:' + rect.left + 'px',
            'top:' + (rect.bottom + 4) + 'px',
            'font-family:system-ui,sans-serif'
        ].join(';');
        document.body.appendChild(dd);
        dropdown = dd;
        return dd;
    }

    function closeDropdown() {
        if (dropdown) { dropdown.remove(); dropdown = null; }
        activeResults = []; activeIdx = -1;
    }

    function renderLoading(inp) {
        var dd = openDropdown(inp);
        dd.innerHTML = '<div style="padding:13px 16px;color:#888;font-size:13px">&#x1F50D; Searching DC Hub facilities...</div>';
    }

    function renderResults(inp, results) {
        activeResults = results;
        activeIdx = -1;
        if (!results.length) { closeDropdown(); return; }
        var dd = openDropdown(inp);
        dd.innerHTML = '';

        results.forEach(function (r, i) {
            var ss = statusStyle(r.status);
            var row = document.createElement('div');
            row.dataset.idx = i;
            row.style.cssText = 'padding:9px 14px;cursor:pointer;border-bottom:1px solid #f2f2f2;display:flex;align-items:center;gap:10px';
            var mw = r.power_mw ? '<span style="font-size:11px;background:#f0f0f0;padding:1px 6px;border-radius:4px;color:#555">' + r.power_mw + ' MW</span>' : '';
            var badge = '<span style="font-size:10px;padding:2px 7px;border-radius:4px;background:' + ss.bg + ';color:' + ss.fg + ';white-space:nowrap">' + (r.status || '') + '</span>';
            row.innerHTML =
                '<div style="flex:1;min-width:0">' +
                '<div style="font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + (r.name || '') + ' ' + mw + '</div>' +
                '<div style="font-size:11px;color:#777;margin-top:1px">' + (r.provider || '') + ' &middot; ' + (r.city || '') + ', ' + (r.state || r.country || '') + '</div>' +
                '</div>' + badge;
            row.addEventListener('mouseover', function () { highlight(i); });
            row.addEventListener('click', function () { activate(r, inp); });
            dd.appendChild(row);
        });

        var foot = document.createElement('div');
        foot.style.cssText = 'padding:5px 14px;font-size:10px;color:#aaa;border-top:1px solid #f2f2f2;text-align:right';
        foot.innerHTML = results.length + ' result' + (results.length !== 1 ? 's' : '') + ' &middot; DC Hub Facilities DB';
        dd.appendChild(foot);
    }

    function highlight(i) {
        if (!dropdown) return;
        dropdown.querySelectorAll('[data-idx]').forEach(function (el) { el.style.background = ''; });
        activeIdx = i;
        var el = dropdown.querySelector('[data-idx="' + i + '"]');
        if (el) { el.style.background = '#f0f5ff'; el.scrollIntoView({ block: 'nearest' }); }
    }

    function activate(r, inp) {
        var lat = parseFloat(r.latitude || r.lat || 0);
        var lng = parseFloat(r.longitude || r.lng || 0);
        if (lat && lng && window.map) {
            window.map.flyTo([lat, lng], 13);
            if (window._cpMarker) { try { window.map.removeLayer(window._cpMarker); } catch (x) {} }
            if (window.L) {
                var popup = '<b>' + (r.name || '') + '</b>';
                if (r.provider) popup += '<br>' + r.provider;
                if (r.city)     popup += '<br>' + r.city + ', ' + (r.state || '');
                if (r.power_mw) popup += '<br>' + r.power_mw + ' MW';
                if (r.status)   popup += '<br>' + r.status;
                window._cpMarker = window.L.marker([lat, lng]).addTo(window.map).bindPopup(popup).openPopup();
            }
        }
        if (inp) inp.value = r.name || '';
        closeDropdown();
        console.log('[smart-search] activated:', r.name, lat, lng);
    }

    function doSearch(q, inp) {
        if (!isFacilityQuery(q)) { closeDropdown(); return; }
        renderLoading(inp);
        fetch(API_ENDPOINT + '?q=' + encodeURIComponent(q) + '&limit=8')
            .then(function (res) { return res.json(); })
            .then(function (data) {
                if (data.success && data.results && data.results.length) {
                    renderResults(inp, data.results);
                } else {
                    closeDropdown();
                }
            })
            .catch(function (err) {
                console.warn('[smart-search] API error:', err);
                closeDropdown();
            });
    }

    var debouncedSearch = debounce(doSearch, DEBOUNCE_MS);

    function init() {
        var inp = document.getElementById('site-search');
        if (!inp) { console.warn('[smart-search] #site-search not found'); return; }

        inp.addEventListener('input', function () {
            var q = inp.value.trim();
            if (q.length < MIN_CHARS) { closeDropdown(); return; }
            debouncedSearch(q, inp);
        });

        inp.addEventListener('keydown', function (e) {
            if (!dropdown) return;
            var items = dropdown.querySelectorAll('[data-idx]');
            if (e.key === 'ArrowDown')  { e.preventDefault(); highlight(Math.min(activeIdx + 1, items.length - 1)); }
            else if (e.key === 'ArrowUp')   { e.preventDefault(); highlight(Math.max(activeIdx - 1, 0)); }
            else if (e.key === 'Escape')     { closeDropdown(); }
            else if (e.key === 'Enter' && activeIdx >= 0) {
                e.preventDefault(); e.stopPropagation();
                activate(activeResults[activeIdx], inp);
            }
        });

        document.addEventListener('click', function (e) {
            if (dropdown && !dropdown.contains(e.target) && e.target !== inp) closeDropdown();
        });

        window.addEventListener('scroll', closeDropdown, true);
        window.addEventListener('resize', closeDropdown);

        console.log('[smart-search] v1.0 ready — facility query detection active');
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
})();

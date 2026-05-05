/**
 * DC Hub — Coordinate Parser Fix v1.5
 * Decimal lat/lng: fly map + drop marker, NO blocking panel.
 * Click Evaluate manually for full site analysis.
 * DMS/DDM: rewrite to decimal then pass to evaluateSite.
 */
(function () {
    'use strict';

    function parseDMS(raw) {
        var s = raw.trim();
        var dms = s.match(/^(\d+(?:\.\d+)?)\s*[\xb0d\s]\s*(\d+(?:\.\d+)?)\s*['′m\s]\s*(\d+(?:\.\d+)?)\s*["″s]?\s*([NSEW])?$/i);
        if (dms) {
            var dd = parseFloat(dms[1]) + parseFloat(dms[2]) / 60 + parseFloat(dms[3]) / 3600;
            if (dms[4] && /[SW]/i.test(dms[4])) dd = -dd;
            return dd;
        }
        var ddm = s.match(/^(\d+(?:\.\d+)?)\s*[\xb0d]\s*(\d+(?:\.\d+)?)\s*['′]\s*([NSEW])?$/i);
        if (ddm) {
            var dd2 = parseFloat(ddm[1]) + parseFloat(ddm[2]) / 60;
            if (ddm[3] && /[SW]/i.test(ddm[3])) dd2 = -dd2;
            return dd2;
        }
        var dd3 = s.match(/^(-?\d+(?:\.\d+)?)\s*([NSEW])?$/i);
        if (dd3) {
            var val = parseFloat(dd3[1]);
            if (dd3[2] && /[SW]/i.test(dd3[2])) val = -Math.abs(val);
            return val;
        }
        return null;
    }

    function tryParseCoords(input) {
        if (!/[\xb0'"′″dDmMsS]|[NSEW]\b/i.test(input)) return null;
        var parts = input.split(/\s*,\s*/);
        if (parts.length < 2) return null;
        var lat = parseDMS(parts[0]);
        var lng = parseDMS(parts[1]);
        if (lat === null || lng === null || isNaN(lat) || isNaN(lng)) return null;
        if (lat < -90 || lat > 90 || lng < -180 || lng > 180) return null;
        return { lat: lat, lng: lng };
    }

    function tryParseDecimalLatLng(input) {
        var m = input.trim().match(/^(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)$/);
        if (!m) return null;
        var lat = parseFloat(m[1]), lng = parseFloat(m[2]);
        if (isNaN(lat) || isNaN(lng)) return null;
        if (lat < -90 || lat > 90 || lng < -180 || lng > 180) return null;
        return { lat: lat, lng: lng };
    }

    function interceptSearch(e) {
        var inp = document.getElementById('site-search');
        if (!inp) return;
        var raw = inp.value.trim();
        if (!raw) return;

        var dec = tryParseDecimalLatLng(raw);
        if (dec) {
            e.preventDefault();
            e.stopPropagation();

            // Fly to location
            if (window.map && window.map.flyTo) window.map.flyTo([dec.lat, dec.lng], 13);

            // Drop a marker (remove previous one if exists)
            if (window._cpMarker) {
                try { window.map.removeLayer(window._cpMarker); } catch (x) {}
            }
            if (window.L && window.map) {
                window._cpMarker = window.L.marker([dec.lat, dec.lng])
                    .addTo(window.map)
                    .bindPopup('<b>' + dec.lat.toFixed(4) + ', ' + dec.lng.toFixed(4) + '</b><br>Click Evaluate for site analysis')
                    .openPopup();
            }

            // Keep coords visible in search box
            inp.value = dec.lat.toFixed(4) + ', ' + dec.lng.toFixed(4);
            console.log('[coord-parser] v1.5 flew to', dec.lat, dec.lng);
            return;
        }

        // DMS/DDM: convert to decimal, then let evaluateSite run
        var coords = tryParseCoords(raw);
        if (!coords) return;
        inp.value = coords.lat.toFixed(6) + ', ' + coords.lng.toFixed(6);
        console.log('[coord-parser] DMS converted to', inp.value);
    }

    function init() {
        var btn = document.getElementById('site-search-btn');
        if (btn) btn.addEventListener('click', interceptSearch, true);
        var inp = document.getElementById('site-search');
        if (inp) inp.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') interceptSearch(e);
        }, true);
        console.log('[coord-parser] v1.5 ready');
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
})();

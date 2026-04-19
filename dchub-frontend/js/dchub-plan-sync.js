/**
 * DC Hub Plan Sync v1.0
 * Checks /api/auth/me on page load, updates localStorage if plan changed.
 * Silent — no UI, no errors shown. Throttled to 1 check per 5 min.
 */
(function() {
    var TOKEN_KEY = 'dchub_token';
    var USER_KEY = 'dchub_user';
    var CHECK_INTERVAL = 5 * 60 * 1000;
    var LAST_CHECK_KEY = 'dchub_plan_last_check';

    function run() {
        var token = localStorage.getItem(TOKEN_KEY);
        if (!token) return;
        var lastCheck = parseInt(localStorage.getItem(LAST_CHECK_KEY) || '0', 10);
        if (Date.now() - lastCheck < CHECK_INTERVAL) return;

        fetch('/api/auth/me', {
            headers: { 'Authorization': 'Bearer ' + token }
        })
        .then(function(res) {
            if (!res.ok) {
                if (res.status === 401) {
                    localStorage.removeItem(TOKEN_KEY);
                    localStorage.removeItem(USER_KEY);
                }
                return null;
            }
            return res.json();
        })
        .then(function(data) {
            if (!data || !data.success || !data.user) return;
            var serverPlan = (data.user.plan || 'free').toLowerCase();
            var cached = {};
            try { cached = JSON.parse(localStorage.getItem(USER_KEY) || '{}'); } catch(e) {}
            var cachedPlan = (cached.plan || 'free').toLowerCase();

            if (serverPlan !== cachedPlan) {
                console.log('[PlanSync] Plan changed: ' + cachedPlan + ' -> ' + serverPlan);
                cached.plan = data.user.plan;
                cached.role = data.user.role;
                cached.plan_updated_at = data.user.plan_updated_at;
                localStorage.setItem(USER_KEY, JSON.stringify(cached));
                try {
                    var session = JSON.parse(localStorage.getItem('dchub_session') || '{}');
                    if (session && session.user) {
                        session.user.plan = data.user.plan;
                        localStorage.setItem('dchub_session', JSON.stringify(session));
                    }
                } catch(e) {}
                window.dispatchEvent(new CustomEvent('dchub-plan-updated', {
                    detail: { oldPlan: cachedPlan, newPlan: serverPlan }
                }));
            }
            localStorage.setItem(LAST_CHECK_KEY, Date.now().toString());
        })
        .catch(function() {});
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', run);
    } else {
        run();
    }
})();

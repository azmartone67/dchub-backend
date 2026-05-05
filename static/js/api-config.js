(function() {
    var host = window.location.hostname;

    var API_URLS = [
        'https://dc-hub-replit-fixedzip--azmartone1.replit.app',
        'https://dchub.cloud'
    ];

    var backendUrl = '';
    if (host === 'dchub.cloud' || host === 'www.dchub.cloud') {
        backendUrl = window.DCHUB_BACKEND_URL || API_URLS[0];
    }

    window.DCHUB_API_BASE = backendUrl;
    window.DCHUB_API_URLS = API_URLS;

    window.dchubFetch = async function(path, options) {
        options = options || {};
        var urls = (host === 'dchub.cloud' || host === 'www.dchub.cloud') ? API_URLS : [''];
        for (var i = 0; i < urls.length; i++) {
            try {
                var url = urls[i] + path;
                var fetchOpts = Object.assign({}, options);
                if (urls[i]) {
                    fetchOpts.mode = fetchOpts.mode || 'cors';
                }
                fetchOpts.signal = fetchOpts.signal || AbortSignal.timeout(5000);
                var resp = await fetch(url, fetchOpts);
                if (resp.ok) {
                    window.DCHUB_API_BASE = urls[i];
                    return resp;
                }
            } catch (e) {
                if (i === urls.length - 1) throw e;
            }
        }
        return fetch(urls[0] + path, options);
    };
})();

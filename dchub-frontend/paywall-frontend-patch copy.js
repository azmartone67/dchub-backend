/**
 * paywall-frontend-patch.js — DC Hub Client-Side Tier Enforcement
 *
 * Drop-in script for dchub-frontend. Zero dependencies.
 * Prevents client-side tier spoofing (e.g. DCHubGate.setTier('pro', 30))
 * by validating tier server-side and freezing the gate object.
 *
 * USAGE:
 *   <script src="/paywall-frontend-patch.js"></script>
 *
 * CONFIGURATION (optional, set before script loads):
 *   window.DCHUB_API_BASE = 'https://api.dchub.cloud';  // defaults to ''
 *
 * CUSTOM EVENTS:
 *   dchub:tier-verified   — { detail: { tier, features, limits, expires_at } }
 *   dchub:upgrade-required — { detail: { required_tier, current_tier, feature } }
 *
 * HTML ATTRIBUTES:
 *   data-requires-tier="pro"         — element hidden unless user has >= pro
 *   data-feature="marketIntel"       — element hidden unless feature is available
 *
 * FETCH WRAPPER:
 *   window.DCHub.fetch(url, opts)    — drop-in fetch() with auto Authorization
 *                                      header and 401/403 handling
 */
(function () {
  "use strict";

  // -----------------------------------------------------------------------
  // Configuration
  // -----------------------------------------------------------------------
  var API_BASE = window.DCHUB_API_BASE || "";
  var UPGRADE_URL = "https://dchub.cloud/pricing";
  var STRIPE_CHECKOUT = "https://buy.stripe.com/7sY5kE8F4fs13ml0PEaZi0c";
  var VERIFY_INTERVAL_MS = 5 * 60 * 1000; // 5 minutes
  var TIER_HIERARCHY = ["free", "registered", "developer", "pro", "enterprise"];

  // -----------------------------------------------------------------------
  // State
  // -----------------------------------------------------------------------
  var currentSession = {
    authenticated: false,
    tier: "free",
    features: [],
    limits: {},
    expires_at: null,
  };

  var _verifyTimer = null;

  // -----------------------------------------------------------------------
  // Helpers
  // -----------------------------------------------------------------------
  function tierRank(tier) {
    var idx = TIER_HIERARCHY.indexOf((tier || "free").toLowerCase());
    return idx === -1 ? 0 : idx;
  }

  function tierGte(userTier, requiredTier) {
    return tierRank(userTier) >= tierRank(requiredTier);
  }

  function dispatch(name, detail) {
    try {
      window.dispatchEvent(
        new CustomEvent(name, { detail: detail, bubbles: true })
      );
    } catch (_) {
      // IE11 fallback — not critical
    }
  }

  function getToken() {
    // Check cookie first, then localStorage fallback for reading only
    var match = document.cookie.match(/(?:^|;\s*)dchub_token=([^;]*)/);
    if (match) return decodeURIComponent(match[1]);
    try {
      return localStorage.getItem("dchub_token") || null;
    } catch (_) {
      return null;
    }
  }

  // -----------------------------------------------------------------------
  // Freeze DCHubGate.setTier — prevent client-side spoofing
  // -----------------------------------------------------------------------
  function freezeGate() {
    // Ensure the namespace exists
    if (!window.DCHubGate) window.DCHubGate = {};

    var noop = function () {
      console.warn(
        "[DC Hub] DCHubGate.setTier() is disabled. Tier is validated server-side."
      );
    };

    // Freeze setTier
    try {
      Object.defineProperty(window.DCHubGate, "setTier", {
        value: noop,
        writable: false,
        configurable: false,
        enumerable: true,
      });
    } catch (_) {
      // If defineProperty fails (already defined), overwrite and re-freeze
      window.DCHubGate.setTier = noop;
      try {
        Object.freeze(window.DCHubGate);
      } catch (__) {}
    }

    // Also prevent reassignment of DCHubGate itself
    try {
      Object.defineProperty(window, "DCHubGate", {
        value: window.DCHubGate,
        writable: false,
        configurable: false,
      });
    } catch (_) {}
  }

  // -----------------------------------------------------------------------
  // Server session verification
  // -----------------------------------------------------------------------
  function verifySession(callback) {
    var url = API_BASE + "/api/verify-session";
    var token = getToken();
    var headers = { "Content-Type": "application/json" };
    if (token) headers["Authorization"] = "Bearer " + token;

    fetch(url, {
      method: "GET",
      headers: headers,
      credentials: "include",
    })
      .then(function (res) {
        return res.json().then(function (data) {
          return { status: res.status, data: data };
        });
      })
      .then(function (result) {
        var data = result.data;

        currentSession.authenticated = !!data.authenticated;
        currentSession.tier = data.tier || "free";
        currentSession.features = data.features || [];
        currentSession.limits = data.limits || {};
        currentSession.expires_at = data.expires_at || null;

        dispatch("dchub:tier-verified", {
          tier: currentSession.tier,
          features: currentSession.features,
          limits: currentSession.limits,
          expires_at: currentSession.expires_at,
        });

        applyTierVisibility();

        if (typeof callback === "function") callback(null, currentSession);
      })
      .catch(function (err) {
        console.error("[DC Hub] Session verification failed:", err);
        // Default to free on network errors — fail secure
        currentSession.authenticated = false;
        currentSession.tier = "free";
        currentSession.features = [];
        applyTierVisibility();
        if (typeof callback === "function") callback(err, currentSession);
      });
  }

  // -----------------------------------------------------------------------
  // Tier-based visibility
  // -----------------------------------------------------------------------
  function applyTierVisibility() {
    // data-requires-tier="pro"
    var tierEls = document.querySelectorAll("[data-requires-tier]");
    for (var i = 0; i < tierEls.length; i++) {
      var el = tierEls[i];
      var required = el.getAttribute("data-requires-tier");
      if (tierGte(currentSession.tier, required)) {
        el.style.display = "";
        removeOverlay(el);
      } else {
        applyLockedOverlay(el, required);
      }
    }

    // data-feature="marketIntel"
    var featureEls = document.querySelectorAll("[data-feature]");
    for (var j = 0; j < featureEls.length; j++) {
      var fel = featureEls[j];
      var feature = fel.getAttribute("data-feature");
      // Convert camelCase to kebab-case for matching
      var kebab = feature.replace(/([A-Z])/g, "-$1").toLowerCase();
      var hasFeature =
        currentSession.features.indexOf(feature) !== -1 ||
        currentSession.features.indexOf(kebab) !== -1;

      if (hasFeature) {
        fel.style.display = "";
        removeOverlay(fel);
      } else {
        applyLockedOverlay(fel, null, feature);
      }
    }
  }

  // -----------------------------------------------------------------------
  // Blur overlay for locked content
  // -----------------------------------------------------------------------
  var OVERLAY_ATTR = "data-dchub-overlay";

  function applyLockedOverlay(el, requiredTier, featureName) {
    // Don't add duplicate overlays
    if (el.getAttribute(OVERLAY_ATTR) === "true") return;

    // Make container relative for overlay positioning
    var pos = window.getComputedStyle(el).position;
    if (pos === "static") el.style.position = "relative";

    // Blur the content
    el.style.filter = "blur(4px)";
    el.style.pointerEvents = "none";
    el.style.userSelect = "none";

    // Create overlay
    var overlay = document.createElement("div");
    overlay.className = "dchub-upgrade-overlay";
    overlay.setAttribute("data-dchub-overlay-el", "true");
    overlay.style.cssText = [
      "position: absolute",
      "inset: 0",
      "display: flex",
      "flex-direction: column",
      "align-items: center",
      "justify-content: center",
      "background: rgba(15, 23, 42, 0.75)",
      "backdrop-filter: blur(2px)",
      "border-radius: 8px",
      "z-index: 1000",
      "pointer-events: auto",
    ].join(";");

    // Lock icon
    var icon = document.createElement("div");
    icon.style.cssText = "font-size:32px;margin-bottom:8px;";
    icon.textContent = "\uD83D\uDD12"; // lock emoji

    // Message
    var msg = document.createElement("p");
    msg.style.cssText =
      "color:#e2e8f0;font-size:14px;text-align:center;margin:0 0 12px;max-width:280px;font-family:system-ui,sans-serif;";
    if (requiredTier) {
      msg.textContent =
        "This content requires a " +
        requiredTier.charAt(0).toUpperCase() +
        requiredTier.slice(1) +
        " plan or higher.";
    } else {
      msg.textContent = "Upgrade your plan to access this feature.";
    }

    // Upgrade button
    var btn = document.createElement("a");
    btn.href = STRIPE_CHECKOUT;
    btn.target = "_blank";
    btn.rel = "noopener noreferrer";
    btn.textContent = "Upgrade Now";
    btn.style.cssText = [
      "display: inline-block",
      "padding: 10px 24px",
      "background: linear-gradient(135deg, #6366f1, #8b5cf6)",
      "color: #fff",
      "font-size: 14px",
      "font-weight: 600",
      "font-family: system-ui, sans-serif",
      "border-radius: 6px",
      "text-decoration: none",
      "cursor: pointer",
      "transition: transform 0.15s, box-shadow 0.15s",
      "box-shadow: 0 2px 8px rgba(99,102,241,0.4)",
    ].join(";");
    btn.onmouseenter = function () {
      btn.style.transform = "translateY(-1px)";
      btn.style.boxShadow = "0 4px 12px rgba(99,102,241,0.5)";
    };
    btn.onmouseleave = function () {
      btn.style.transform = "";
      btn.style.boxShadow = "0 2px 8px rgba(99,102,241,0.4)";
    };
    btn.onclick = function () {
      dispatch("dchub:upgrade-required", {
        required_tier: requiredTier || "pro",
        current_tier: currentSession.tier,
        feature: featureName || null,
      });
    };

    overlay.appendChild(icon);
    overlay.appendChild(msg);
    overlay.appendChild(btn);
    el.appendChild(overlay);
    el.setAttribute(OVERLAY_ATTR, "true");
  }

  function removeOverlay(el) {
    if (el.getAttribute(OVERLAY_ATTR) !== "true") return;
    el.style.filter = "";
    el.style.pointerEvents = "";
    el.style.userSelect = "";
    var overlays = el.querySelectorAll("[data-dchub-overlay-el]");
    for (var i = 0; i < overlays.length; i++) {
      overlays[i].parentNode.removeChild(overlays[i]);
    }
    el.removeAttribute(OVERLAY_ATTR);
  }

  // -----------------------------------------------------------------------
  // DCHub.fetch — drop-in fetch() replacement
  // -----------------------------------------------------------------------
  function dchubFetch(url, opts) {
    opts = opts || {};
    opts.headers = opts.headers || {};
    opts.credentials = opts.credentials || "include";

    var token = getToken();
    if (token && !opts.headers["Authorization"]) {
      opts.headers["Authorization"] = "Bearer " + token;
    }

    // Prefix with API_BASE if relative URL
    var fullUrl = url;
    if (url.charAt(0) === "/") {
      fullUrl = API_BASE + url;
    }

    return fetch(fullUrl, opts).then(function (res) {
      if (res.status === 401) {
        // Token expired or invalid — re-verify
        verifySession();
        dispatch("dchub:upgrade-required", {
          required_tier: "registered",
          current_tier: currentSession.tier,
          feature: null,
        });
        return Promise.reject(
          new Error("[DC Hub] Authentication failed (401). Please log in again.")
        );
      }
      if (res.status === 403) {
        // Insufficient tier
        return res
          .clone()
          .json()
          .then(function (body) {
            dispatch("dchub:upgrade-required", {
              required_tier: body.required_tier || "pro",
              current_tier: body.current_tier || currentSession.tier,
              feature: null,
            });
            return Promise.reject(
              new Error(
                "[DC Hub] Upgrade required. Your " +
                  (body.current_tier || currentSession.tier) +
                  " plan does not include this feature."
              )
            );
          })
          .catch(function (jsonErr) {
            // Non-JSON 403 response — still reject gracefully
            if (jsonErr.message && jsonErr.message.indexOf("[DC Hub]") === 0) {
              return Promise.reject(jsonErr);
            }
            dispatch("dchub:upgrade-required", {
              required_tier: "pro",
              current_tier: currentSession.tier,
              feature: null,
            });
            return Promise.reject(
              new Error("[DC Hub] Access denied (403). Upgrade may be required.")
            );
          });
      }
      return res;
    });
  }

  // -----------------------------------------------------------------------
  // Public API — window.DCHub
  // -----------------------------------------------------------------------
  var dchubAPI = {
    fetch: dchubFetch,
    getSession: function () {
      return JSON.parse(JSON.stringify(currentSession));
    },
    verifyNow: function (cb) {
      verifySession(cb);
    },
    tierGte: tierGte,
    upgradeUrl: UPGRADE_URL,
    checkoutUrl: STRIPE_CHECKOUT,
  };

  // Define DCHub as non-writable, non-configurable to prevent tampering
  try {
    Object.defineProperty(window, "DCHub", {
      value: Object.freeze(dchubAPI),
      writable: false,
      configurable: false,
      enumerable: true,
    });
  } catch (_) {
    // Fallback if window.DCHub already exists
    window.DCHub = Object.freeze(dchubAPI);
  }

  // -----------------------------------------------------------------------
  // Initialization
  // -----------------------------------------------------------------------
  function init() {
    freezeGate();
    verifySession();

    // Re-verify every 5 minutes
    if (_verifyTimer) clearInterval(_verifyTimer);
    _verifyTimer = setInterval(function () {
      verifySession();
    }, VERIFY_INTERVAL_MS);

    // Re-apply visibility when DOM changes (SPA navigation), debounced
    if (typeof MutationObserver !== "undefined") {
      var _visibilityTimer = null;
      var observer = new MutationObserver(function () {
        if (_visibilityTimer) clearTimeout(_visibilityTimer);
        _visibilityTimer = setTimeout(applyTierVisibility, 100);
      });
      observer.observe(document.body, { childList: true, subtree: true });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();

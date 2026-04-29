/**
 * DC Hub Energy Infrastructure v1.0
 * ==================================
 * Provides window.DCHubEnergy namespace used by energy-enhancement-v3.js
 * Exposes energy infrastructure data helpers and state management.
 * 
 * Must load AFTER land-power-app.js, BEFORE energy-enhancement-v3.js
 */

(function() {
    'use strict';

    // ============================================
    // STATE ELECTRICITY RATES (EIA 2024 avg ¢/kWh industrial)
    // ============================================
    var STATE_RATES = {
        'AZ': 7.52, 'TX': 6.63, 'VA': 7.82, 'GA': 6.83, 'NV': 7.59,
        'UT': 6.10, 'OH': 7.45, 'IA': 6.36, 'IL': 8.21, 'CA': 17.80,
        'NJ': 11.42, 'WA': 5.50, 'OR': 6.80, 'CO': 8.10, 'FL': 9.20,
        'MN': 8.50, 'MO': 7.90, 'TN': 7.10, 'NC': 7.30, 'NY': 12.50,
        'MA': 16.20, 'PA': 8.60, 'IN': 7.80, 'MI': 8.90, 'WI': 8.40,
        'NE': 7.20, 'KS': 8.30, 'OK': 6.50, 'AR': 6.90, 'MS': 7.40,
        'AL': 7.00, 'SC': 6.80, 'KY': 6.40, 'WV': 7.10, 'ID': 6.30,
        'MT': 7.00, 'WY': 6.80, 'NM': 7.60, 'ND': 7.50, 'SD': 7.80
    };

    // ============================================
    // ISO/RTO GRID DATA
    // ============================================
    var ISO_DATA = {
        'PJM':   { name: 'PJM Interconnection', capacity_gw: 190, load_gw: 150, lmp_avg: 35.50, states: ['VA','PA','NJ','MD','OH','WV','DE','NC','IL','IN','MI','KY'] },
        'ERCOT': { name: 'ERCOT', capacity_gw: 130, load_gw: 85, lmp_avg: 28.00, states: ['TX'] },
        'MISO':  { name: 'MISO', capacity_gw: 190, load_gw: 120, lmp_avg: 31.20, states: ['MN','WI','IA','IL','IN','MI','MO','AR','MS','LA','TX'] },
        'CAISO': { name: 'CAISO', capacity_gw: 82, load_gw: 48, lmp_avg: 45.60, states: ['CA'] },
        'SPP':   { name: 'SPP', capacity_gw: 105, load_gw: 55, lmp_avg: 29.80, states: ['OK','KS','NE','ND','SD','NM','AR','MO'] },
        'NYISO': { name: 'NYISO', capacity_gw: 42, load_gw: 32, lmp_avg: 42.30, states: ['NY'] },
        'ISONE': { name: 'ISO-NE', capacity_gw: 35, load_gw: 28, lmp_avg: 48.50, states: ['MA','CT','RI','NH','VT','ME'] },
        'WECC':  { name: 'WECC (Non-ISO)', capacity_gw: 95, load_gw: 60, lmp_avg: 38.00, states: ['AZ','NV','UT','CO','WY','WA','OR','ID','MT','NM'] },
        'SERC':  { name: 'SERC (Non-ISO)', capacity_gw: 120, load_gw: 90, lmp_avg: 34.00, states: ['GA','NC','SC','AL','TN','KY','FL'] },
        'FRCC':  { name: 'FRCC', capacity_gw: 70, load_gw: 50, lmp_avg: 36.00, states: ['FL'] },
        'BPA':   { name: 'Bonneville Power', capacity_gw: 30, load_gw: 12, lmp_avg: 22.00, states: ['WA','OR','ID','MT'] }
    };

    // ============================================
    // CORE API
    // ============================================

    window.DCHubEnergy = {
        version: '1.0.0',

        /**
         * Get electricity rate for a state
         * @param {string} state - Two-letter state code
         * @returns {number} Rate in cents/kWh
         */
        getStateRate: function(state) {
            return STATE_RATES[state] || 8.0;
        },

        /**
         * Get ISO/RTO data for a region
         * @param {string} iso - ISO code (e.g., 'PJM', 'ERCOT')
         * @returns {Object|null} ISO data object
         */
        getISO: function(iso) {
            return ISO_DATA[iso] || null;
        },

        /**
         * Determine ISO from state code
         * @param {string} state - Two-letter state code
         * @returns {string} ISO code
         */
        stateToISO: function(state) {
            for (var iso in ISO_DATA) {
                if (ISO_DATA[iso].states.indexOf(state) !== -1) {
                    return iso;
                }
            }
            return 'WECC';
        },

        /**
         * Get all state rates
         * @returns {Object} State code -> rate mapping
         */
        getAllRates: function() {
            return Object.assign({}, STATE_RATES);
        },

        /**
         * Get all ISO data
         * @returns {Object} ISO code -> data mapping
         */
        getAllISOs: function() {
            return Object.assign({}, ISO_DATA);
        },

        /**
         * Calculate annual power cost estimate
         * @param {number} mw - Megawatts
         * @param {string} state - Two-letter state code
         * @param {number} pue - Power Usage Effectiveness (default 1.3)
         * @returns {Object} Cost breakdown
         */
        estimatePowerCost: function(mw, state, pue) {
            pue = pue || 1.3;
            var rate = this.getStateRate(state);
            var annualKwh = mw * 1000 * 8760 * pue;
            var annualCost = annualKwh * (rate / 100);
            return {
                mw: mw,
                state: state,
                rate_cents_kwh: rate,
                pue: pue,
                annual_kwh: annualKwh,
                annual_cost_usd: Math.round(annualCost),
                monthly_cost_usd: Math.round(annualCost / 12),
                cost_per_kw_month: Math.round(annualCost / (mw * 1000) / 12 * 100) / 100
            };
        },

        /**
         * Get grid headroom for an ISO
         * @param {string} iso - ISO code
         * @returns {Object} Headroom data
         */
        getGridHeadroom: function(iso) {
            var data = ISO_DATA[iso];
            if (!data) return null;
            var spare = data.capacity_gw - data.load_gw;
            return {
                iso: iso,
                name: data.name,
                capacity_gw: data.capacity_gw,
                load_gw: data.load_gw,
                spare_gw: spare,
                utilization_pct: Math.round(data.load_gw / data.capacity_gw * 100),
                signal: spare > 30 ? 'green' : spare > 10 ? 'yellow' : 'red'
            };
        },

        // Data references for external use
        stateRates: STATE_RATES,
        isoData: ISO_DATA
    };

    console.log('⚡ DC Hub Energy Infrastructure v1.0 loaded — ' + Object.keys(STATE_RATES).length + ' states, ' + Object.keys(ISO_DATA).length + ' ISOs');
})();

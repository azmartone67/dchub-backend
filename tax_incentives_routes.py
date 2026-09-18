"""
DC Hub - Tax Incentives API Routes (Enhanced v2)
Add to your Replit main.py Flask backend

Usage in main.py:
    from tax_incentives_routes import setup_tax_incentive_routes
    setup_tax_incentive_routes(app, db)

Endpoints (original):
    GET  /api/v1/tax-incentives              - All states (filterable)
    GET  /api/v1/tax-incentives/stats         - Header counts
    GET  /api/v1/tax-incentives/<abbr>        - Single state
    PUT  /api/v1/tax-incentives/<abbr>        - Update state (admin)

Endpoints (new v2):
    POST /api/v1/tax-incentives/compare       - Side-by-side comparison
    POST /api/v1/tax-incentives/calculator    - Savings estimator
    GET  /api/v1/tax-incentives/presets       - Quick filter presets
    GET  /api/v1/tax-incentives/export        - CSV/JSON export (Pro-gated)
    GET  /api/v1/tax-incentives/map-layer     - GeoJSON for Land & Power overlay
    GET  /api/v1/tax-incentives/ratings       - Compact ratings for Market Intel cards
"""

from flask import Blueprint, jsonify, request, Response
import logging
import sys
import json
import csv
import io
from internal_auth import require_internal_or_admin
from utc_clock import utc_iso_z, utc_now

logger = logging.getLogger(__name__)

tax_incentives_bp = Blueprint('tax_incentives', __name__)

# ─── DEFAULT DATA (seed) ─────────────────────────────────────
# This seeds the database on first run. After that, update via admin API.
DEFAULT_INCENTIVES = [
    {"abbr":"AL","name":"Alabama","fips":"01","has_incentive":True,"rating":4,"duration":"Up to 30 years","min_investment":"$400M","jobs_required":"20 jobs @ $40K avg","sales_tax":True,"property_tax":True,"income_tax":False,"electricity_tax":False,"summary":"Up to 30 years of tax abatements for data centers investing $400M+ and creating 20+ jobs at $40K average compensation.","details":"One of the longest incentive durations in the country, targeting large-scale hyperscale deployments.","source":"Alabama Dept. of Revenue","source_url":"https://www.revenue.alabama.gov/tax-incentives/chapter-9b-abatements/"},
    {"abbr":"AK","name":"Alaska","fips":"02","has_incentive":False,"rating":2,"summary":"No specific data center tax incentive. However, Alaska has no state-wide sales tax — a natural advantage.","details":"Equipment purchases are tax-free by default due to absence of state sales tax."},
    {"abbr":"AZ","name":"Arizona","fips":"04","has_incentive":True,"rating":5,"duration":"10–20 years","min_investment":"$25M–$50M","jobs_required":"Varies by project","sales_tax":True,"summary":"Computer Data Center Program: TPT and Use Tax exemptions for qualifying equipment. 10 or 20 year certification.","details":"Administered by AZ Commerce Authority. One of the fastest-growing DC markets.","source":"Arizona Commerce Authority"},
    {"abbr":"AR","name":"Arkansas","fips":"05","has_incentive":True,"rating":3,"duration":"Varies","min_investment":"Varies","sales_tax":True,"summary":"Incentives under Act 819 (2023) and Act 548 (2025) for qualifying data center projects.","details":"Building out incentive framework with recent legislative actions."},
    {"abbr":"CA","name":"California","fips":"06","has_incentive":False,"rating":1,"summary":"No data center tax incentive. High energy costs and tax burden make CA one of the most expensive states.","details":"Despite being a tech hub, no targeted DC incentives exist."},
    {"abbr":"CO","name":"Colorado","fips":"08","has_incentive":False,"rating":1,"summary":"No known data center tax incentive legislation currently in place.","details":"No specific DC incentive program."},
    {"abbr":"CT","name":"Connecticut","fips":"09","has_incentive":True,"rating":3,"duration":"Varies","min_investment":"Varies","sales_tax":True,"summary":"Data Center Tax Incentive Program administered by the DECD.","details":"Provides tax incentives for qualified data center developers.","source":"CT DECD"},
    {"abbr":"DE","name":"Delaware","fips":"10","has_incentive":False,"rating":3,"summary":"No specific incentive, but no property or sales tax — a significant natural advantage.","details":"Absence of both taxes acts as a built-in incentive."},
    {"abbr":"FL","name":"Florida","fips":"12","has_incentive":True,"rating":4,"duration":"Varies","min_investment":"100 MW+ IT load","sales_tax":True,"summary":"Sales/use tax exemption. As of Aug 2025, limited to 100 MW+ IT load facilities (hyperscale focus).","details":"Recently modified to target larger deployments.","source":"Florida Dept. of Revenue"},
    {"abbr":"GA","name":"Georgia","fips":"13","has_incentive":True,"rating":5,"duration":"Varies by tier","min_investment":"$100M–$250M","jobs_required":"25 quality jobs","sales_tax":True,"summary":"Complete sales tax exemption on high-tech equipment. $296M in tax breaks expected by 2025.","details":"Thresholds vary by county population and facility type.","source":"Georgia Dept. of Econ Dev"},
    {"abbr":"HI","name":"Hawaii","fips":"15","has_incentive":False,"rating":1,"summary":"No data center tax incentive. High energy costs and geographic isolation.","details":"Remote location and high electricity costs."},
    {"abbr":"ID","name":"Idaho","fips":"16","has_incentive":True,"rating":3,"duration":"Varies","min_investment":"Varies","sales_tax":True,"summary":"State-level sales tax exemption for qualifying data center developments.","details":"Administered by Idaho Commerce.","source":"Idaho Commerce"},
    {"abbr":"IL","name":"Illinois","fips":"17","has_incentive":True,"rating":5,"duration":"Up to 20 years","min_investment":"$250M","jobs_required":"20 full-time jobs","sales_tax":True,"income_tax":True,"electricity_tax":True,"summary":"Data Center Investment Program: $370M+ in exemptions covering equipment and energy.","details":"One of few states itemizing by company. Requires $250M min.","source":"Illinois DCEO","source_url":"https://dceo.illinois.gov/expandrelocate/incentives/datacenters.html"},
    {"abbr":"IN","name":"Indiana","fips":"18","has_incentive":True,"rating":5,"duration":"Up to 25–50 years","min_investment":"$10M","jobs_required":"Varies","sales_tax":True,"property_tax":True,"electricity_tax":True,"summary":"100% sales tax exemption on power, plant, and equipment. Up to 50 years for $750M+.","details":"Most generous program nationally. Low $10M minimum.","source":"Indiana EDC"},
    {"abbr":"IA","name":"Iowa","fips":"19","has_incentive":True,"rating":5,"duration":"Varies","min_investment":"$1M+","jobs_required":"Varies","sales_tax":True,"property_tax":True,"summary":"100% sales/use tax abatement starting at $1M. No property tax on equipment. $151M total.","details":"Lowest entry threshold in the country.","source":"Iowa Dept. of Revenue"},
    {"abbr":"KS","name":"Kansas","fips":"20","has_incentive":True,"rating":4,"duration":"20 years","min_investment":"Varies","sales_tax":True,"property_tax":True,"summary":"20-year state and local sales/use tax exemption under SB 98. No property tax on new equipment.","details":"One of the longer durations available.","source":"Kansas Dept. of Commerce"},
    {"abbr":"KY","name":"Kentucky","fips":"21","has_incentive":True,"rating":4,"duration":"Varies","min_investment":"$100M (by county)","jobs_required":"Varies","sales_tax":True,"summary":"Sales/use tax exemption with thresholds based on county population. Expanded under HB 775 (2025).","details":"Recently expanded eligibility encourages rural development.","source":"Kentucky Dept. of Revenue"},
    {"abbr":"LA","name":"Louisiana","fips":"22","has_incentive":True,"rating":4,"duration":"Up to 20+10 yr","min_investment":"$200M","jobs_required":"50 permanent jobs","sales_tax":True,"summary":"Sales/use tax rebate on equipment and construction. Up to 30 years total.","details":"Meta chose LA for $10B AI DC.","source":"Louisiana Econ Dev"},
    {"abbr":"ME","name":"Maine","fips":"23","has_incentive":True,"rating":3,"duration":"Varies","min_investment":"Varies","sales_tax":True,"summary":"Sales tax refund/exemption for 20,000+ sq ft facilities primarily housing servers.","details":"Eligible for equipment installed after October 2018."},
    {"abbr":"MD","name":"Maryland","fips":"24","has_incentive":True,"rating":4,"duration":"10–20 years","min_investment":"$2M–$5M","jobs_required":"5+ jobs","sales_tax":True,"summary":"Data Center Maryland: 10-year exemption at $2M–$5M; up to 20 years for $250M+.","details":"Tiered thresholds for mid-sized and hyperscale.","source":"Maryland Dept. of Commerce"},
    {"abbr":"MA","name":"Massachusetts","fips":"25","has_incentive":True,"rating":4,"duration":"20 years","min_investment":"$50M","jobs_required":"100 jobs","sales_tax":True,"electricity_tax":True,"summary":"20-year exemption for 100K+ sq ft. Covers servers, networking, software, electricity, construction.","details":"Comprehensive — includes electricity exemption.","source":"Massachusetts OED"},
    {"abbr":"MI","name":"Michigan","fips":"26","has_incentive":True,"rating":4,"duration":"Through 2050/2065","min_investment":"$250M","jobs_required":"30 jobs @ 150% median","sales_tax":True,"summary":"Sales/use tax exemption through 2050. Requires clean energy & green building.","details":"Unique sustainability requirements.","source":"Michigan EDC"},
    {"abbr":"MN","name":"Minnesota","fips":"27","has_incentive":True,"rating":5,"duration":"20 yr + permanent","min_investment":"$30M","jobs_required":"Varies","sales_tax":True,"property_tax":True,"electricity_tax":True,"summary":"20-year sales tax + permanent property tax exemption on equipment. 25K+ sq ft, $30M+.","details":"Permanent property tax exemption is rare and highly valuable.","source":"Minnesota DEED"},
    {"abbr":"MS","name":"Mississippi","fips":"28","has_incentive":True,"rating":5,"duration":"Up to 10 years","min_investment":"$20M","jobs_required":"20 jobs @ 125% avg","sales_tax":True,"income_tax":True,"summary":"Sales + income + franchise tax exemptions. $20M min, 20 jobs.","details":"One of few with income tax exemptions.","source":"Mississippi Dev Auth"},
    {"abbr":"MO","name":"Missouri","fips":"29","has_incentive":True,"rating":4,"duration":"Varies","min_investment":"$25M/$5M exist","jobs_required":"10/5 jobs","sales_tax":True,"summary":"Dual-track: new ($25M) and existing ($5M). Itemizes by recipient.","details":"Encourages greenfield and brownfield.","source":"Missouri DED"},
    {"abbr":"MT","name":"Montana","fips":"30","has_incentive":True,"rating":3,"duration":"10 yr graduated","min_investment":"$50M","property_tax":True,"summary":"Property tax reduction (Class 17) for 25K+ sq ft / $50M. No state sales tax.","details":"No sales tax — focuses on property tax.","source":"Montana Dept. of Revenue"},
    {"abbr":"NE","name":"Nebraska","fips":"31","has_incentive":True,"rating":3,"duration":"Varies by tier","min_investment":"$3M+","jobs_required":"30+ employees","sales_tax":True,"property_tax":True,"summary":"Tiered sales/property tax breaks starting at $3M with 30 employees.","details":"Multiple entry points for different scales.","source":"Nebraska Dept. of Revenue"},
    {"abbr":"NV","name":"Nevada","fips":"32","has_incentive":True,"rating":5,"duration":"10–20 years","min_investment":"$25M/$100M","jobs_required":"10/50 jobs","sales_tax":True,"property_tax":True,"summary":"Sales tax as low as 2%. Up to 75% property tax abatement. No income tax. $140M in exemptions.","details":"Among the most attractive nationally.","source":"NV GOED"},
    {"abbr":"NH","name":"New Hampshire","fips":"33","has_incentive":False,"rating":2,"summary":"No specific incentive but no state sales tax — natural cost advantage.","details":"Tax-free equipment without a formal program."},
    {"abbr":"NJ","name":"New Jersey","fips":"34","has_incentive":True,"rating":5,"duration":"Varies","min_investment":"Varies","sales_tax":True,"income_tax":True,"summary":"Next NJ — AI: up to $250M in tax credits for AI/DC investments.","details":"NJEDA-administered. Targets AI projects.","source":"NJEDA","source_url":"https://www.njeda.gov/nextnjai/"},
    {"abbr":"NM","name":"New Mexico","fips":"35","has_incentive":False,"rating":1,"summary":"No known data center tax incentive legislation.","details":"No targeted program."},
    {"abbr":"NY","name":"New York","fips":"36","has_incentive":True,"rating":3,"duration":"Varies","min_investment":"Varies","sales_tax":True,"summary":"Sales tax exemption for equipment used by Internet data centers.","details":"Administered by Dept. of Tax & Finance.","source":"NY Tax & Finance"},
    {"abbr":"NC","name":"North Carolina","fips":"37","has_incentive":True,"rating":4,"duration":"Varies","min_investment":"$75M","jobs_required":"Wage & ins. req.","sales_tax":True,"electricity_tax":True,"summary":"Sales/use tax exemption for electricity and equipment. $75M+ within 5 years.","details":"Electricity exemption is major.","source":"EDPNC"},
    {"abbr":"ND","name":"North Dakota","fips":"38","has_incentive":True,"rating":3,"duration":"Varies","min_investment":"Varies","sales_tax":True,"summary":"Sales/use tax exemption for IT equipment in 15K+ sq ft facilities.","details":"Must be built after Dec 31, 2020.","source":"ND Tax Dept."},
    {"abbr":"OH","name":"Ohio","fips":"39","has_incentive":True,"rating":3,"status":"paused_new_applicants","status_as_of":"2026-05-27","status_note":"Gov. DeWine directed the Ohio Tax Credit Authority to pause consideration of all NEW data center sales/use tax exemption requests while the General Assembly's Joint Data Center Committee holds hearings. Already-executed agreements are unaffected and continue to run. No stated end date.","status_source":"Office of the Governor of Ohio, directive to the Ohio Tax Credit Authority, 2026-05-27","status_source_url":"https://signalohio.org/ohio-approves-last-data-center-exemption-before-moratorium/","duration":"Varies","min_investment":"$100M","jobs_required":"$1.5M payroll","sales_tax":True,"property_tax":True,"summary":"PAUSED to new applicants since 2026-05-27. The ORC 122.175 sales/use tax exemption ($100M+ capex / $1.5M+ payroll) is closed to new requests; already-executed agreements continue. Ohio's absence of personal property tax is unaffected and remains a structural benefit.","details":"Pause followed the Ohio LSC scoring FY2025 forgone revenue at $1.57B against a $135.8M forecast. HB 957 (136th GA) would end the program permanently and sits in House Ways & Means. Local tools (enterprise zones, CRAs, TIF) are unaffected by the pause. Rating lowered 4 to 3: a NEW project cannot obtain the exemption today.","source":"Ohio Dept. of Development / Ohio Revised Code 122.175","source_url":"https://codes.ohio.gov/ohio-revised-code/section-122.175"},
    {"abbr":"OK","name":"Oklahoma","fips":"40","has_incentive":True,"rating":3,"duration":"Varies","min_investment":"N/A","sales_tax":True,"summary":"Sales tax exemption for computer services with majority out-of-state revenue.","details":"Benefits national/global operators.","source":"Oklahoma Tax Commission"},
    {"abbr":"OR","name":"Oregon","fips":"41","has_incentive":True,"rating":4,"duration":"Varies","min_investment":"Varies","property_tax":True,"income_tax":True,"summary":"No sales tax. Enterprise Zone abatements, Strategic Investment Program, Oregon Investment Advantage.","details":"Stackable programs, especially rural.","source":"Business Oregon"},
    {"abbr":"PA","name":"Pennsylvania","fips":"42","has_incentive":True,"rating":4,"duration":"Varies","min_investment":"Varies","sales_tax":True,"property_tax":True,"income_tax":True,"summary":"DC Equipment Program + Keystone Opportunity Zones (KOZ) — near-complete tax elimination.","details":"KOZ is particularly powerful.","source":"PA Dept. of Revenue"},
    {"abbr":"RI","name":"Rhode Island","fips":"44","has_incentive":False,"rating":1,"summary":"No known data center tax incentive legislation.","details":"No targeted program."},
    {"abbr":"SC","name":"South Carolina","fips":"45","has_incentive":True,"rating":4,"duration":"Varies","min_investment":"$50M","jobs_required":"25 jobs @ 150% county","sales_tax":True,"electricity_tax":True,"summary":"Sales/use tax exemption on equipment, hardware, and electricity. $50M, 25 jobs.","details":"Electricity exemption is key.","source":"SC Dept. of Commerce"},
    {"abbr":"SD","name":"South Dakota","fips":"46","has_incentive":False,"rating":2,"summary":"No specific incentive but no state income tax. General programs may apply.","details":"No income tax = natural advantage."},
    {"abbr":"TN","name":"Tennessee","fips":"47","has_incentive":True,"rating":5,"duration":"Varies","min_investment":"$100M","jobs_required":"15 jobs @ 150% avg","sales_tax":True,"electricity_tax":True,"summary":"Sales/use tax exemption + reduced 1.5% electricity rate. FastTrack grants + credits.","details":"Comprehensive stackable package.","source":"Tennessee DECD"},
    {"abbr":"TX","name":"Texas","fips":"48","has_incentive":True,"rating":5,"duration":"10–15 years","min_investment":"$200M","jobs_required":"20 jobs","sales_tax":True,"electricity_tax":True,"summary":"Over $1B in subsidies for 2025 — largest nationally. Equipment + electricity. No income tax.","details":"100K+ sq ft, $200M/5yr, 20 jobs.","source":"Texas Comptroller"},
    {"abbr":"UT","name":"Utah","fips":"49","has_incentive":True,"rating":3,"duration":"Varies","min_investment":"150K sq ft min","sales_tax":True,"summary":"Sales/use tax exemption for 150K+ sq ft. EDTIF/Rural EDTIF + infrastructure credits.","details":"Targets larger facilities.","source":"Utah GOE"},
    {"abbr":"VT","name":"Vermont","fips":"50","has_incentive":False,"rating":1,"summary":"No known data center tax incentive legislation.","details":"No targeted program."},
    {"abbr":"VA","name":"Virginia","fips":"51","has_incentive":True,"rating":5,"duration":"Varies","min_investment":"Capital + employ.","jobs_required":"Employment threshold","sales_tax":True,"summary":"World's largest DC market. $732M subsidies (2024). Sales tax exemption.","details":"NoVA (Loudoun) = global epicenter.","source":"Virginia EDP"},
    {"abbr":"WA","name":"Washington","fips":"53","has_incentive":True,"rating":4,"duration":"Varies","min_investment":"Varies","sales_tax":True,"summary":"Sales/use tax exemption for servers, power, installation. Expanded to urban (HB 1846).","details":"Expanded eligibility to urban counties.","source":"WA DOR"},
    {"abbr":"WV","name":"West Virginia","fips":"54","has_incentive":True,"rating":4,"duration":"Varies","min_investment":"Varies","sales_tax":True,"property_tax":True,"summary":"New 2025: sales/use tax exemption + property tax at ~5% salvage value.","details":"5% salvage value is unique.","source":"WV Dept. of Econ Dev"},
    {"abbr":"WI","name":"Wisconsin","fips":"55","has_incentive":True,"rating":4,"duration":"Varies","min_investment":"$50M–$150M","sales_tax":True,"summary":"Sales/use tax exemption for equipment/materials. Thresholds vary by county pop.","details":"WEDC-certified. Statewide reach.","source":"WEDC"},
    {"abbr":"WY","name":"Wyoming","fips":"56","has_incentive":True,"rating":3,"duration":"Varies","min_investment":"$5M/$50M","sales_tax":True,"summary":"Two-tier: equipment at $5M, full at $50M. No state income tax.","details":"Outcompeting California.","source":"Wyoming Excise Tax Div"},
]


# Base values by abbr, for recording what an override was written against.
_DEFAULTS_BY_ABBR = {s['abbr']: s for s in DEFAULT_INCENTIVES}


def setup_tax_incentive_routes(app, db=None):
    """
    Register tax incentive API routes on the Flask app.
    
    DEFAULT_INCENTIVES is the source of truth. If `db` is provided, admin
    overrides stored there are layered on top of it at boot; without a db the
    module's own values are served and admin PUTs live only in this process.
    See "WHICH STORE IS AUTHORITATIVE" above _init_db.

    NOTE (2026-09-18): main.py:10103 calls this as setup_tax_incentive_routes(app)
    with NO db, so no override store is attached in production today.
    
    Endpoints:
        GET  /api/v1/tax-incentives          - Get all state incentives
        GET  /api/v1/tax-incentives/:abbr    - Get single state
        GET  /api/v1/tax-incentives/stats    - Summary statistics
        PUT  /api/v1/tax-incentives/:abbr    - Update a state (admin)
    """
    
    # ── Store layering ────────────────────────────────────────
    # DEFAULT_INCENTIVES is the source of truth; the DB holds admin overrides
    # only. See "WHICH STORE IS AUTHORITATIVE" above _init_db.
    #
    # dict(s) copies deliberately: the previous `{s['abbr']: s for s in ...}`
    # aliased the module-level dicts, so a PUT mutated DEFAULT_INCENTIVES in
    # place for the life of the process.
    incentives_data = {s['abbr']: dict(s) for s in DEFAULT_INCENTIVES}

    if db:
        try:
            _init_db(db)
            overrides = _load_overrides(db)
            incentives_data, notes = _layer_overrides(DEFAULT_INCENTIVES, overrides)
            for note in notes:
                logger.warning("[tax-incentives] %s", note)
            logger.info(
                "[tax-incentives] %d states; %d carry an admin override",
                len(incentives_data), len(overrides),
            )
        except Exception:
            # logger.exception, not print(e): the defect this replaces was a
            # NameError reduced to a one-line message nobody read.
            logger.exception(
                "[tax-incentives] override load failed — serving DEFAULT_INCENTIVES unmodified"
            )
            incentives_data = {s['abbr']: dict(s) for s in DEFAULT_INCENTIVES}
    
    @app.route('/api/v1/tax-incentives', methods=['GET', 'OPTIONS'])
    def get_all_incentives():
        if request.method == 'OPTIONS':
            return '', 204
        
        # Query params for filtering
        has_incentive = request.args.get('has_incentive')
        min_rating = request.args.get('min_rating', type=int)
        tax_type = request.args.get('tax_type')  # sales, property, income, electricity
        search = request.args.get('search', '').lower()
        sort = request.args.get('sort', 'name')  # name, rating
        
        results = list(incentives_data.values())
        
        if has_incentive is not None:
            val = has_incentive.lower() in ('true', '1', 'yes')
            results = [s for s in results if s.get('has_incentive') == val]
        
        if min_rating:
            results = [s for s in results if s.get('rating', 0) >= min_rating]
        
        if tax_type:
            key_map = {'sales': 'sales_tax', 'property': 'property_tax', 
                       'income': 'income_tax', 'electricity': 'electricity_tax'}
            key = key_map.get(tax_type)
            if key:
                results = [s for s in results if s.get(key)]
        
        if search:
            results = [s for s in results if 
                       search in s.get('name', '').lower() or 
                       search in s.get('abbr', '').lower() or
                       search in s.get('summary', '').lower()]
        
        if sort == 'rating':
            results.sort(key=lambda s: s.get('rating', 0), reverse=True)
        else:
            results.sort(key=lambda s: s.get('name', ''))
        
        # Phase WW (2026-05-17) — soft-paywall the bulk 50-state dump.
        # r52 (2026-05-25) — REWRITTEN. The old version capped anon to
        # 10 states which broke /tax-incentives map (user asked 3x).
        # Now: anon ALWAYS gets all 50 states with STRUCTURAL fields
        # (abbr, name, fips, has_incentive, rating) so the map renders.
        # Rich DETAIL fields (summary, details, source, min_investment,
        # jobs_required, duration) are gated to IDENTIFIED+ — the map
        # still colors every state, anon clicks reveal the upgrade CTA.
        # status/status_as_of/status_note are STRUCTURAL on purpose: a
        # program that is suspended or repealed must not read as available
        # to an anonymous caller. Withholding a pause behind the paywall
        # publishes a wrong answer, not a thinner one. status_source /
        # status_source_url stay gated alongside source / source_url.
        _STRUCTURAL_FIELDS = {
            'abbr', 'name', 'fips', 'has_incentive', 'rating',
            'sales_tax', 'property_tax', 'income_tax', 'electricity_tax',
            'status', 'status_as_of', 'status_note',
        }
        _gated = False
        _total = len(results)
        try:
            from util.tier_gate import resolve_tier, Tier as _T
            _tier, _ = resolve_tier()
            if _tier < _T.IDENTIFIED:
                results = [
                    {k: v for k, v in s.items() if k in _STRUCTURAL_FIELDS}
                    for s in results
                ]
                _gated = True
        except Exception:
            pass

        out = {
            'status':       'success',
            'count':        len(results),
            'data':         results,
            'last_updated': utc_iso_z(),
        }
        if _gated:
            out['_gated'] = True
            out['_detail_gated'] = True
            out['_total_states'] = _total
            out['_required_tier'] = "IDENTIFIED"
            out['_upgrade_cta'] = (
                f"All {_total} US states shown with structural fields "
                f"(rating, has_incentive, tax-type flags). Get full "
                f"detail (summary, duration, min investment, jobs "
                f"required, source) free — POST /api/v1/keys/claim "
                f"for a key in 30s."
            )
            out['_signup_url'] = "https://dchub.cloud/signup"
        return jsonify(out)

    @app.route('/api/v1/tax-incentives/stats', methods=['GET', 'OPTIONS'])
    def get_incentive_stats():
        if request.method == 'OPTIONS':
            return '', 204
        
        all_states = list(incentives_data.values())
        with_incentive = [s for s in all_states if s.get('has_incentive')]
        
        return jsonify({
            'status': 'success',
            'data': {
                'total_states': len(all_states),
                'with_incentives': len(with_incentive),
                'sales_tax_programs': len([s for s in all_states if s.get('sales_tax')]),
                'property_tax_programs': len([s for s in all_states if s.get('property_tax')]),
                'income_tax_programs': len([s for s in all_states if s.get('income_tax')]),
                'electricity_exemptions': len([s for s in all_states if s.get('electricity_tax')]),
                'by_rating': {
                    'excellent': len([s for s in all_states if s.get('rating', 0) >= 5]),
                    'strong': len([s for s in all_states if s.get('rating', 0) == 4]),
                    'moderate': len([s for s in all_states if s.get('rating', 0) == 3]),
                    'limited': len([s for s in all_states if s.get('rating', 0) == 2]),
                    'none': len([s for s in all_states if s.get('rating', 0) <= 1]),
                },
                'last_updated': utc_iso_z()
            }
        })
    
    @app.route('/api/v1/tax-incentives/<abbr>', methods=['GET', 'OPTIONS'])
    def get_state_incentive(abbr):
        if request.method == 'OPTIONS':
            return '', 204
        
        state = incentives_data.get(abbr.upper())
        if not state:
            return jsonify({'status': 'error', 'message': f'State {abbr} not found'}), 404
        
        return jsonify({'status': 'success', 'data': state})
    
    @app.route('/api/v1/tax-incentives/<abbr>', methods=['PUT', 'OPTIONS'])
    def update_state_incentive(abbr):
        """Admin endpoint to update a state's incentive data"""
        if request.method == 'OPTIONS':
            return '', 204

        # Fail-closed gate: the docstring calls this 'admin' but nothing enforced
        # it — any anonymous PUT could rewrite a state's rating/summary/source.
        if not require_internal_or_admin(request):
            return jsonify({'status': 'error', 'message': 'unauthorized'}), 401

        abbr = abbr.upper()
        if abbr not in incentives_data:
            return jsonify({'status': 'error', 'message': f'State {abbr} not found'}), 404
        
        updates = request.get_json()
        if not updates:
            return jsonify({'status': 'error', 'message': 'No data provided'}), 400
        
        # Merge updates in memory, and persist ONLY the changed fields as an
        # admin override — never a full snapshot. A snapshot would pin every
        # other field of this state to today's module and block later
        # statutory corrections from reaching it.
        incentives_data[abbr].update(updates)
        incentives_data[abbr]['last_modified'] = utc_iso_z()

        if db:
            try:
                _save_override(db, abbr, updates, _DEFAULTS_BY_ABBR.get(abbr, {}))
            except Exception:
                logger.exception("[tax-incentives] override write failed for %s", abbr)
        
        return jsonify({'status': 'success', 'data': incentives_data[abbr]})
    
    # Register v2 enhancement routes
    _setup_v2_routes(app, incentives_data, db)
    
    print("[tax-incentives] ✅ Routes registered: /api/v1/tax-incentives (v2 enhanced)")


# ─── SAVINGS CALCULATOR RATES ────────────────────────────────
STATE_TAX_RATES = {
    "AL": {"sales_rate": 0.04, "property_rate": 0.0042, "income_rate": 0.065, "electricity_rate": 0.04},
    "AK": {"sales_rate": 0.00, "property_rate": 0.0105, "income_rate": 0.00, "electricity_rate": 0.00},
    "AZ": {"sales_rate": 0.056, "property_rate": 0.0062, "income_rate": 0.025, "electricity_rate": 0.00},
    "AR": {"sales_rate": 0.065, "property_rate": 0.0063, "income_rate": 0.044, "electricity_rate": 0.00},
    "CA": {"sales_rate": 0.0725, "property_rate": 0.0073, "income_rate": 0.088, "electricity_rate": 0.00},
    "CO": {"sales_rate": 0.029, "property_rate": 0.005, "income_rate": 0.044, "electricity_rate": 0.00},
    "CT": {"sales_rate": 0.0635, "property_rate": 0.0206, "income_rate": 0.065, "electricity_rate": 0.00},
    "DE": {"sales_rate": 0.00, "property_rate": 0.0056, "income_rate": 0.066, "electricity_rate": 0.00},
    "FL": {"sales_rate": 0.06, "property_rate": 0.0089, "income_rate": 0.055, "electricity_rate": 0.00},
    "GA": {"sales_rate": 0.04, "property_rate": 0.0092, "income_rate": 0.0549, "electricity_rate": 0.00},
    "HI": {"sales_rate": 0.04, "property_rate": 0.0028, "income_rate": 0.064, "electricity_rate": 0.00},
    "ID": {"sales_rate": 0.06, "property_rate": 0.0063, "income_rate": 0.058, "electricity_rate": 0.00},
    "IL": {"sales_rate": 0.0625, "property_rate": 0.0197, "income_rate": 0.099, "electricity_rate": 0.032},
    "IN": {"sales_rate": 0.07, "property_rate": 0.0085, "income_rate": 0.0315, "electricity_rate": 0.03},
    "IA": {"sales_rate": 0.06, "property_rate": 0.0154, "income_rate": 0.06, "electricity_rate": 0.00},
    "KS": {"sales_rate": 0.065, "property_rate": 0.0138, "income_rate": 0.057, "electricity_rate": 0.00},
    "KY": {"sales_rate": 0.06, "property_rate": 0.0086, "income_rate": 0.04, "electricity_rate": 0.00},
    "LA": {"sales_rate": 0.0445, "property_rate": 0.0055, "income_rate": 0.045, "electricity_rate": 0.00},
    "ME": {"sales_rate": 0.055, "property_rate": 0.0136, "income_rate": 0.0715, "electricity_rate": 0.00},
    "MD": {"sales_rate": 0.06, "property_rate": 0.0104, "income_rate": 0.0825, "electricity_rate": 0.00},
    "MA": {"sales_rate": 0.0625, "property_rate": 0.012, "income_rate": 0.05, "electricity_rate": 0.025},
    "MI": {"sales_rate": 0.06, "property_rate": 0.0154, "income_rate": 0.06, "electricity_rate": 0.00},
    "MN": {"sales_rate": 0.0688, "property_rate": 0.0109, "income_rate": 0.098, "electricity_rate": 0.02},
    "MS": {"sales_rate": 0.07, "property_rate": 0.0081, "income_rate": 0.05, "electricity_rate": 0.00},
    "MO": {"sales_rate": 0.04225, "property_rate": 0.0097, "income_rate": 0.048, "electricity_rate": 0.00},
    "MT": {"sales_rate": 0.00, "property_rate": 0.0083, "income_rate": 0.0675, "electricity_rate": 0.00},
    "NE": {"sales_rate": 0.055, "property_rate": 0.016, "income_rate": 0.0664, "electricity_rate": 0.00},
    "NV": {"sales_rate": 0.0685, "property_rate": 0.0055, "income_rate": 0.00, "electricity_rate": 0.00},
    "NH": {"sales_rate": 0.00, "property_rate": 0.0186, "income_rate": 0.05, "electricity_rate": 0.00},
    "NJ": {"sales_rate": 0.06625, "property_rate": 0.0225, "income_rate": 0.115, "electricity_rate": 0.00},
    "NM": {"sales_rate": 0.05, "property_rate": 0.008, "income_rate": 0.059, "electricity_rate": 0.00},
    "NY": {"sales_rate": 0.04, "property_rate": 0.0146, "income_rate": 0.0685, "electricity_rate": 0.00},
    "NC": {"sales_rate": 0.0475, "property_rate": 0.0084, "income_rate": 0.025, "electricity_rate": 0.02},
    "ND": {"sales_rate": 0.05, "property_rate": 0.0098, "income_rate": 0.0195, "electricity_rate": 0.00},
    "OH": {"sales_rate": 0.0575, "property_rate": 0.015, "income_rate": 0.00, "electricity_rate": 0.00},
    "OK": {"sales_rate": 0.045, "property_rate": 0.009, "income_rate": 0.04, "electricity_rate": 0.00},
    "OR": {"sales_rate": 0.00, "property_rate": 0.0097, "income_rate": 0.066, "electricity_rate": 0.00},
    "PA": {"sales_rate": 0.06, "property_rate": 0.0153, "income_rate": 0.0899, "electricity_rate": 0.00},
    "RI": {"sales_rate": 0.07, "property_rate": 0.016, "income_rate": 0.0599, "electricity_rate": 0.00},
    "SC": {"sales_rate": 0.06, "property_rate": 0.0057, "income_rate": 0.05, "electricity_rate": 0.025},
    "SD": {"sales_rate": 0.042, "property_rate": 0.012, "income_rate": 0.00, "electricity_rate": 0.00},
    "TN": {"sales_rate": 0.07, "property_rate": 0.0064, "income_rate": 0.065, "electricity_rate": 0.03},
    "TX": {"sales_rate": 0.0625, "property_rate": 0.016, "income_rate": 0.00, "electricity_rate": 0.025},
    "UT": {"sales_rate": 0.061, "property_rate": 0.0058, "income_rate": 0.0465, "electricity_rate": 0.00},
    "VT": {"sales_rate": 0.06, "property_rate": 0.018, "income_rate": 0.076, "electricity_rate": 0.00},
    "VA": {"sales_rate": 0.053, "property_rate": 0.0082, "income_rate": 0.06, "electricity_rate": 0.00},
    "WA": {"sales_rate": 0.065, "property_rate": 0.0093, "income_rate": 0.00, "electricity_rate": 0.00},
    "WV": {"sales_rate": 0.06, "property_rate": 0.006, "income_rate": 0.065, "electricity_rate": 0.00},
    "WI": {"sales_rate": 0.05, "property_rate": 0.017, "income_rate": 0.075, "electricity_rate": 0.00},
    "WY": {"sales_rate": 0.04, "property_rate": 0.006, "income_rate": 0.00, "electricity_rate": 0.00},
}

# ─── FILTER PRESETS ──────────────────────────────────────────
FILTER_PRESETS = [
    {"id": "hyperscale", "name": "Best for Hyperscale", "description": "Top-rated states for large-scale ($200M+) deployments", "icon": "🏗️", "filters": {"min_rating": 5, "has_incentive": True}, "highlight_states": ["TX", "VA", "IN", "IL", "GA", "NV", "TN"]},
    {"id": "sales_tax_free", "name": "Best Sales Tax Exemption", "description": "Strong sales/use tax exemptions on DC equipment", "icon": "🏷️", "filters": {"tax_type": "sales", "min_rating": 4}, "highlight_states": ["IN", "IA", "TX", "VA", "GA", "IL", "NV"]},
    {"id": "electricity", "name": "Electricity Exemptions", "description": "States exempting electricity taxes — #1 operating cost", "icon": "⚡", "filters": {"tax_type": "electricity"}, "highlight_states": ["IL", "IN", "MA", "MN", "NC", "SC", "TN", "TX"]},
    {"id": "low_barrier", "name": "Lowest Entry Barrier", "description": "Min investment under $25M — ideal for edge/colo", "icon": "🚀", "filters": {"has_incentive": True}, "highlight_states": ["IA", "IN", "MD", "NE", "WY", "MO"]},
    {"id": "longest_duration", "name": "Longest Duration", "description": "Programs offering 20+ years of tax relief", "icon": "📅", "filters": {"has_incentive": True, "min_rating": 4}, "highlight_states": ["AL", "IN", "KS", "LA", "MA", "MN", "IL"]},
    {"id": "property_tax", "name": "Property Tax Relief", "description": "Property tax abatements on DC facilities", "icon": "🏠", "filters": {"tax_type": "property"}, "highlight_states": ["IN", "IA", "KS", "MN", "NE", "NV", "OH", "PA", "WV"]},
    {"id": "income_tax_free", "name": "Income Tax Exemptions", "description": "Corporate income tax credits for DCs", "icon": "💼", "filters": {"tax_type": "income"}, "highlight_states": ["IL", "MS", "NJ", "OR", "PA"]},
    {"id": "no_income_tax", "name": "No State Income Tax", "description": "Permanent structural advantage — no corporate income tax", "icon": "🎯", "filters": {"has_incentive": True}, "highlight_states": ["TX", "NV", "WA", "WY", "SD", "AK", "NH"]},
]

# ─── STATE CENTROIDS (for GeoJSON) ──────────────────────────
STATE_CENTROIDS = {
    "AL": [32.81, -86.79], "AK": [61.37, -152.40], "AZ": [33.73, -111.43],
    "AR": [34.97, -92.37], "CA": [36.12, -119.68], "CO": [39.06, -105.31],
    "CT": [41.60, -72.76], "DE": [39.32, -75.51], "FL": [27.77, -81.69],
    "GA": [33.04, -83.64], "HI": [21.09, -157.50], "ID": [44.24, -114.48],
    "IL": [40.35, -88.99], "IN": [39.85, -86.26], "IA": [42.01, -93.21],
    "KS": [38.53, -96.73], "KY": [37.67, -84.67], "LA": [31.17, -91.87],
    "ME": [44.69, -69.38], "MD": [39.06, -76.80], "MA": [42.23, -71.53],
    "MI": [43.33, -84.54], "MN": [45.69, -93.90], "MS": [32.74, -89.68],
    "MO": [38.46, -92.29], "MT": [46.92, -110.45], "NE": [41.13, -98.27],
    "NV": [38.31, -117.06], "NH": [43.45, -71.56], "NJ": [40.30, -74.52],
    "NM": [34.84, -106.25], "NY": [42.17, -74.95], "NC": [35.63, -79.81],
    "ND": [47.53, -99.78], "OH": [40.39, -82.76], "OK": [35.57, -96.93],
    "OR": [44.57, -122.07], "PA": [40.59, -77.21], "RI": [41.68, -71.51],
    "SC": [33.86, -80.95], "SD": [44.30, -99.44], "TN": [35.75, -86.69],
    "TX": [31.05, -97.56], "UT": [40.15, -111.86], "VT": [44.05, -72.71],
    "VA": [37.77, -78.17], "WA": [47.40, -121.49], "WV": [38.49, -80.95],
    "WI": [44.27, -89.62], "WY": [42.76, -107.30],
}


def _setup_v2_routes(app, incentives_data, db):
    """Register new v2 enhancement routes."""

    @app.route('/api/v1/tax-incentives/compare', methods=['POST', 'OPTIONS'])
    def compare_incentives():
        if request.method == 'OPTIONS':
            return '', 204
        body = request.get_json()
        if not body or 'states' not in body:
            return jsonify({'status': 'error', 'message': 'Provide {"states": ["TX","VA",...]}'}), 400
        abbrs = [a.upper() for a in body['states'][:5]]
        states = [incentives_data[a] for a in abbrs if a in incentives_data]
        if len(states) < 2:
            return jsonify({'status': 'error', 'message': 'Need at least 2 valid states'}), 400

        comparison = {'states': [], 'metrics': {}}
        for s in states:
            comparison['states'].append({'abbr': s['abbr'], 'name': s['name'], 'rating': s.get('rating', 0), 'has_incentive': s.get('has_incentive', False)})

        rated = sorted(states, key=lambda x: x.get('rating', 0), reverse=True)
        comparison['metrics']['rating'] = {'label': 'Overall Rating', 'values': {s['abbr']: s.get('rating', 0) for s in states}, 'best': rated[0]['abbr'], 'rankings': {s['abbr']: i+1 for i, s in enumerate(rated)}}

        tax_types = [('sales_tax','Sales Tax'),('property_tax','Property Tax'),('income_tax','Income Tax'),('electricity_tax','Electricity')]
        for ttype, tlabel in tax_types:
            comparison['metrics'][ttype] = {'label': tlabel, 'values': {s['abbr']: s.get(ttype, False) for s in states}}

        coverage = {s['abbr']: sum(1 for t, _ in tax_types if s.get(t, False)) for s in states}
        comparison['metrics']['coverage_score'] = {'label': 'Exemption Breadth (of 4)', 'values': coverage}

        for field, label in [('duration', 'Duration'), ('min_investment', 'Min Investment'), ('jobs_required', 'Job Requirements')]:
            comparison['metrics'][field] = {'label': label, 'values': {s['abbr']: s.get(field, '\u2014') for s in states}}

        investment = body.get('investment')
        if investment and isinstance(investment, (int, float)) and investment > 0:
            comparison['estimated_savings'] = {}
            for s in states:
                rates = STATE_TAX_RATES.get(s['abbr'], {})
                comparison['estimated_savings'][s['abbr']] = _calc_savings(s, rates, investment)

        return jsonify({'status': 'success', 'count': len(states), 'data': comparison})

    @app.route('/api/v1/tax-incentives/calculator', methods=['POST', 'OPTIONS'])
    def savings_calculator():
        if request.method == 'OPTIONS':
            return '', 204
        body = request.get_json()
        if not body or 'investment' not in body:
            return jsonify({'status': 'error', 'message': 'Provide {"investment": <amount>}'}), 400
        investment = float(body['investment'])
        if investment <= 0:
            return jsonify({'status': 'error', 'message': 'Investment must be positive'}), 400

        equipment_pct = float(body.get('equipment_pct', 0.60))
        annual_electricity = float(body.get('annual_electricity', investment * 0.02))
        duration_years = int(body.get('duration_years', 10))
        target_states = body.get('states')

        results = []
        for abbr, s in incentives_data.items():
            if target_states and abbr not in [a.upper() for a in target_states]:
                continue
            rates = STATE_TAX_RATES.get(abbr, {})
            savings = _calc_savings(s, rates, investment, equipment_pct, annual_electricity, duration_years)
            results.append({'abbr': abbr, 'name': s['name'], 'rating': s.get('rating', 0), 'has_incentive': s.get('has_incentive', False), **savings})

        results.sort(key=lambda x: x.get('total_estimated_savings', 0), reverse=True)
        return jsonify({
            'status': 'success', 'count': len(results),
            'input': {'investment': investment, 'equipment_pct': equipment_pct, 'annual_electricity': annual_electricity, 'duration_years': duration_years},
            'data': results,
            'disclaimer': 'Estimates only. Actual savings depend on project specifics, local rates, and program eligibility. Consult qualified tax counsel.',
        })

    @app.route('/api/v1/tax-incentives/presets', methods=['GET', 'OPTIONS'])
    def get_filter_presets():
        if request.method == 'OPTIONS':
            return '', 204
        enriched = []
        for preset in FILTER_PRESETS:
            f = preset['filters']
            matched = list(incentives_data.values())
            if f.get('has_incentive') is not None:
                matched = [s for s in matched if s.get('has_incentive') == f['has_incentive']]
            if f.get('min_rating'):
                matched = [s for s in matched if s.get('rating', 0) >= f['min_rating']]
            if f.get('tax_type'):
                key = {'sales': 'sales_tax', 'property': 'property_tax', 'income': 'income_tax', 'electricity': 'electricity_tax'}.get(f['tax_type'])
                if key:
                    matched = [s for s in matched if s.get(key)]
            enriched.append({**preset, 'matching_count': len(matched), 'matching_states': [s['abbr'] for s in matched]})
        return jsonify({'status': 'success', 'count': len(enriched), 'data': enriched})

    @app.route('/api/v1/tax-incentives/export', methods=['GET', 'OPTIONS'])
    def export_incentives():
        if request.method == 'OPTIONS':
            return '', 204
        api_key = request.headers.get('X-API-Key', '')
        if not _check_pro_access(api_key):
            return jsonify({'status': 'error', 'message': 'Export requires a Pro subscription', 'pro_required': True, 'upgrade_url': 'https://dchub.cloud/pricing'}), 403

        fmt = request.args.get('format', 'csv').lower()
        state_filter = request.args.get('states', '')
        results = list(incentives_data.values())
        if state_filter:
            abbrs = [a.strip().upper() for a in state_filter.split(',')]
            results = [s for s in results if s['abbr'] in abbrs]
        results.sort(key=lambda s: s.get('name', ''))

        if fmt == 'json':
            return jsonify({'status': 'success', 'count': len(results), 'data': results, 'exported_at': utc_iso_z()})

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['State','Abbreviation','FIPS','Has Incentive','Rating','Duration','Min Investment','Jobs Required','Sales Tax Exempt','Property Tax Exempt','Income Tax Exempt','Electricity Exempt','Summary','Details','Source','Source URL'])
        for s in results:
            writer.writerow([s.get('name'),s.get('abbr'),s.get('fips'),'Yes' if s.get('has_incentive') else 'No',s.get('rating',0),s.get('duration',''),s.get('min_investment',''),s.get('jobs_required',''),'Yes' if s.get('sales_tax') else 'No','Yes' if s.get('property_tax') else 'No','Yes' if s.get('income_tax') else 'No','Yes' if s.get('electricity_tax') else 'No',s.get('summary',''),s.get('details',''),s.get('source',''),s.get('source_url','')])
        return Response(output.getvalue(), mimetype='text/csv', headers={'Content-Disposition': f'attachment; filename=dc-hub-tax-incentives-{utc_now().strftime("%Y%m%d")}.csv'})

    @app.route('/api/v1/tax-incentives/map-layer', methods=['GET', 'OPTIONS'])
    def get_map_layer():
        if request.method == 'OPTIONS':
            return '', 204
        min_rating = request.args.get('min_rating', type=int)
        has_incentive = request.args.get('has_incentive')
        features = []
        for abbr, s in incentives_data.items():
            if has_incentive is not None:
                val = has_incentive.lower() in ('true', '1', 'yes')
                if s.get('has_incentive') != val:
                    continue
            if min_rating and s.get('rating', 0) < min_rating:
                continue
            coords = STATE_CENTROIDS.get(abbr)
            if not coords:
                continue
            features.append({'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [coords[1], coords[0]]}, 'properties': {'abbr': abbr, 'name': s['name'], 'fips': s.get('fips'), 'has_incentive': s.get('has_incentive', False), 'rating': s.get('rating', 0), 'sales_tax': s.get('sales_tax', False), 'property_tax': s.get('property_tax', False), 'income_tax': s.get('income_tax', False), 'electricity_tax': s.get('electricity_tax', False), 'duration': s.get('duration', ''), 'min_investment': s.get('min_investment', ''), 'summary': s.get('summary', ''), 'detail_url': f'https://dchub.cloud/tax-incentives#{abbr}'}})
        return jsonify({'type': 'FeatureCollection', 'features': features, 'metadata': {'source': 'DC Hub Tax Incentives', 'last_updated': utc_iso_z(), 'total_features': len(features)}})

    @app.route('/api/v1/tax-incentives/ratings', methods=['GET', 'OPTIONS'])
    def get_incentive_ratings():
        if request.method == 'OPTIONS':
            return '', 204
        ratings = {}
        for abbr, s in incentives_data.items():
            tax_types = []
            if s.get('sales_tax'): tax_types.append('sales')
            if s.get('property_tax'): tax_types.append('property')
            if s.get('income_tax'): tax_types.append('income')
            if s.get('electricity_tax'): tax_types.append('electricity')
            ratings[abbr] = {'rating': s.get('rating', 0), 'has_incentive': s.get('has_incentive', False), 'tax_types': tax_types, 'tax_type_count': len(tax_types), 'summary': s.get('summary', ''), 'duration': s.get('duration', '')}
        return jsonify({'status': 'success', 'data': ratings})


def _calc_savings(state_data, rates, investment, equipment_pct=0.60, annual_electricity=None, duration_years=10):
    """Estimate tax savings for a state given investment parameters."""
    if annual_electricity is None:
        annual_electricity = investment * 0.02
    equipment_cost = investment * equipment_pct
    has_incentive = state_data.get('has_incentive', False)
    savings = {'sales_tax_savings': 0, 'property_tax_savings': 0, 'income_tax_savings': 0, 'electricity_tax_savings': 0, 'total_estimated_savings': 0, 'savings_pct_of_investment': 0, 'annual_savings': 0}
    if not has_incentive:
        return savings
    if state_data.get('sales_tax') and rates.get('sales_rate', 0) > 0:
        savings['sales_tax_savings'] = round(equipment_cost * rates['sales_rate'])
    if state_data.get('property_tax') and rates.get('property_rate', 0) > 0:
        savings['property_tax_savings'] = round(investment * 0.70 * rates['property_rate'] * duration_years * 0.75)
    if state_data.get('income_tax') and rates.get('income_rate', 0) > 0:
        savings['income_tax_savings'] = round(investment * 0.05 * rates['income_rate'] * duration_years)
    if state_data.get('electricity_tax') and rates.get('electricity_rate', 0) > 0:
        savings['electricity_tax_savings'] = round(annual_electricity * rates['electricity_rate'] * duration_years)
    total = savings['sales_tax_savings'] + savings['property_tax_savings'] + savings['income_tax_savings'] + savings['electricity_tax_savings']
    savings['total_estimated_savings'] = total
    savings['savings_pct_of_investment'] = round((total / investment) * 100, 2) if investment > 0 else 0
    savings['annual_savings'] = round(total / duration_years) if duration_years > 0 else 0
    return savings


def _check_pro_access(api_key):
    """Check Pro-tier access. Integrate with Stripe/auth system."""
    pro_keys = ['dchub-pro-demo', 'dchub-enterprise-demo']
    return api_key in pro_keys


# ─── DB HELPERS ───────────────────────────────────────────────
#
# ★ WHICH STORE IS AUTHORITATIVE (decided 2026-09-18)
#
# DEFAULT_INCENTIVES — the list at the top of this module — is the source of
# truth. It is versioned in git, reviewed on a PR, and is where a statutory
# change gets recorded (Ohio ORC 122.175 in azmartone67/dchub-backend#4753 is
# the worked example).
#
# The database holds ADMIN OVERRIDES ONLY. For each state an admin has edited
# through PUT /api/v1/tax-incentives/<abbr> it stores THE FIELDS THAT EDIT
# CHANGED — never a full snapshot of the state. Boot layers those fields on
# top of DEFAULT_INCENTIVES (_layer_overrides).
#
# ★ Why not the other way round. The previous shape seeded the whole table
# from DEFAULT_INCENTIVES and then read the table back as truth. It never ran
# — _load_from_db referenced an undefined `cursor`, raised NameError on every
# call, and a bare `except:` returned [] — so the module always fell through
# to its own defaults and nothing looked wrong. Simply repairing the loader
# would have made the DB win using rows seeded from an OLDER module, and every
# statutory correction shipped since that seed would have reverted on the next
# boot, silently, behind a green deploy. Layering makes the direction explicit:
# a field nobody has overridden always tracks the module, and an override is a
# deliberate exception that _layer_overrides() REPORTS once the module value
# beneath it moves, instead of quietly masking it.
#
# The legacy `tax_incentives` table (full snapshots, written by the removed
# _seed_db/_update_db) is no longer read or written here. It is left in place
# rather than dropped, and its rows must never be layered: they are seed
# copies of an old module, not admin edits, and the two are indistinguishable
# once written.

_OVERRIDES_TABLE = 'tax_incentive_overrides'


def _placeholder(db):
    """The DB-API placeholder for this driver: '%s' for psycopg2, '?' for sqlite3.

    Read from the driver's declared paramstyle instead of hardcoded, so the real
    sqlite3 connection in tests/test_tax_incentive_overrides.py executes the same
    statements production executes against Postgres — a fake cursor that ignored
    the SQL would not have caught the NameError this module shipped for its whole
    life. Unknown driver falls back to '%s', the production case.
    """
    module = sys.modules.get(type(db).__module__.split('.')[0])
    return '?' if getattr(module, 'paramstyle', '') == 'qmark' else '%s'


def _db_error_types(db):
    """This driver's error base, as a tuple, for a NARROW `except`.

    Resolved from sys.modules rather than a module-scope `import psycopg2`: this
    file is imported by the pure-function suite in 0.12s with no driver present,
    and tests/_stub_sentinel.py pins driver identity across the session.

    Returns () when no driver error base is available — deliberately. An empty
    tuple catches NOTHING, so the error reaches the caller's boundary handler and
    is logged with a full traceback. That is strictly better than the bare
    `except:` this replaces, which is the reason the defect below was invisible.
    """
    module = sys.modules.get(type(db).__module__.split('.')[0])
    base = getattr(module, 'Error', None)
    if isinstance(base, type) and issubclass(base, Exception):
        return (base,)
    return ()


def _init_db(db):
    """Create the overrides table if it doesn't exist.

    No try/except: a DDL failure here means overrides cannot be stored, and the
    caller must hear about it. The old version wrapped this in `except: pass`.
    """
    cursor = db.cursor()
    cursor.execute(
        'CREATE TABLE IF NOT EXISTS ' + _OVERRIDES_TABLE + ' ('
        ' abbr TEXT PRIMARY KEY,'
        ' payload TEXT NOT NULL,'
        ' updated_at TEXT'
        ')'
    )
    db.commit()


def _load_overrides(db):
    """Admin overrides keyed by state abbr: {abbr: {fields, base, updated_at}}.

    ★ This function used to read a bare `cursor` that was never defined in its
    scope. Every call raised NameError; a bare `except:` turned that into [], and
    the caller read [] as "nothing stored" and reseeded. The narrow except below
    is the whole point of the repair: a NameError, AttributeError or TypeError in
    here is a BUG in this file and now propagates to a handler that logs a
    traceback. Only genuine driver errors are absorbed, and even those get a line.
    """
    cursor = db.cursor()
    try:
        cursor.execute('SELECT abbr, payload FROM ' + _OVERRIDES_TABLE)
        rows = cursor.fetchall() or []
    except _db_error_types(db) as e:
        logger.warning(
            "[tax-incentives] could not read %s (%s: %s) — serving DEFAULT_INCENTIVES unmodified",
            _OVERRIDES_TABLE, type(e).__name__, e,
        )
        return {}

    overrides = {}
    for abbr, payload in rows:
        try:
            entry = json.loads(payload)
        except (TypeError, ValueError) as e:
            logger.warning("[tax-incentives] override row %s is not valid JSON (%s) — ignored", abbr, e)
            continue
        if isinstance(entry, dict):
            overrides[abbr] = entry
        else:
            logger.warning(
                "[tax-incentives] override row %s holds %s, not an object — ignored",
                abbr, type(entry).__name__,
            )
    return overrides


def _layer_overrides(defaults, overrides):
    """Layer admin overrides onto DEFAULT_INCENTIVES. Returns (data_by_abbr, notes).

    Pure — no DB, no Flask — so the layering rule can be tested directly.

    `notes` names each override that has drifted: one whose stored `base` value
    for a field no longer equals the module's current value, meaning an admin
    edit is now masking a newer statutory correction. That is the one real risk
    of letting overrides exist at all, so it is reported rather than left silent.
    """
    data = {s['abbr']: dict(s) for s in defaults}
    notes = []
    for abbr in sorted(overrides):
        entry = overrides[abbr] or {}
        if abbr not in data:
            notes.append("%s: override for a state DEFAULT_INCENTIVES no longer ships — ignored" % abbr)
            continue
        fields = entry.get('fields') or {}
        base = entry.get('base') or {}
        for key, was in base.items():
            now = data[abbr].get(key)
            if now != was:
                notes.append(
                    "%s.%s: admin override is masking a newer module value (%r -> %r)" % (abbr, key, was, now)
                )
        data[abbr].update(fields)
        if entry.get('updated_at'):
            data[abbr]['last_modified'] = entry['updated_at']
    return data, notes


def _save_override(db, abbr, fields, base_now):
    """Record `fields` as the admin override for `abbr`.

    Merged onto any existing override so successive edits accumulate, and stored
    with the module value of each overridden key at write time so
    _layer_overrides() can later report drift.

    The read below has no `except`: if it fails we must NOT write, or an override
    that already carried three fields would be silently replaced by one.
    """
    ph = _placeholder(db)
    cursor = db.cursor()
    cursor.execute('SELECT payload FROM ' + _OVERRIDES_TABLE + ' WHERE abbr = ' + ph, (abbr,))
    row = cursor.fetchone()

    existing = {}
    if row and row[0]:
        try:
            parsed = json.loads(row[0])
            if isinstance(parsed, dict):
                existing = parsed
        except (TypeError, ValueError):
            logger.warning("[tax-incentives] override row %s was unparseable — replacing it", abbr)

    merged_fields = dict(existing.get('fields') or {})
    merged_fields.update(fields)
    merged_base = dict(existing.get('base') or {})
    for key in fields:
        merged_base[key] = base_now.get(key)

    now = utc_iso_z()
    payload = json.dumps({'fields': merged_fields, 'base': merged_base, 'updated_at': now})
    cursor.execute(
        'INSERT INTO ' + _OVERRIDES_TABLE + ' (abbr, payload, updated_at)'
        ' VALUES (' + ph + ', ' + ph + ', ' + ph + ')'
        ' ON CONFLICT (abbr) DO UPDATE SET'
        ' payload = EXCLUDED.payload, updated_at = EXCLUDED.updated_at',
        (abbr, payload, now),
    )
    db.commit()
    return {'fields': merged_fields, 'base': merged_base, 'updated_at': now}


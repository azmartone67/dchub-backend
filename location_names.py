"""
DC Hub — Location Name Resolution
==================================
Drop into your Replit backend alongside main.py.
Import with: from location_names import resolve_location_name, get_state_name, get_country_name

Fixes the "Usa Il" / "Br" / "Us Ny" problem across all location pages,
facility pages, meta descriptions, and sitemap entries.
"""

# ============================================================
# US STATE CODES → FULL NAMES
# ============================================================
US_STATES = {
    'AL': 'Alabama', 'AK': 'Alaska', 'AZ': 'Arizona', 'AR': 'Arkansas',
    'CA': 'California', 'CO': 'Colorado', 'CT': 'Connecticut', 'DE': 'Delaware',
    'FL': 'Florida', 'GA': 'Georgia', 'HI': 'Hawaii', 'ID': 'Idaho',
    'IL': 'Illinois', 'IN': 'Indiana', 'IA': 'Iowa', 'KS': 'Kansas',
    'KY': 'Kentucky', 'LA': 'Louisiana', 'ME': 'Maine', 'MD': 'Maryland',
    'MA': 'Massachusetts', 'MI': 'Michigan', 'MN': 'Minnesota', 'MS': 'Mississippi',
    'MO': 'Missouri', 'MT': 'Montana', 'NE': 'Nebraska', 'NV': 'Nevada',
    'NH': 'New Hampshire', 'NJ': 'New Jersey', 'NM': 'New Mexico', 'NY': 'New York',
    'NC': 'North Carolina', 'ND': 'North Dakota', 'OH': 'Ohio', 'OK': 'Oklahoma',
    'OR': 'Oregon', 'PA': 'Pennsylvania', 'RI': 'Rhode Island', 'SC': 'South Carolina',
    'SD': 'South Dakota', 'TN': 'Tennessee', 'TX': 'Texas', 'UT': 'Utah',
    'VT': 'Vermont', 'VA': 'Virginia', 'WA': 'Washington', 'WV': 'West Virginia',
    'WI': 'Wisconsin', 'WY': 'Wyoming', 'DC': 'District of Columbia',
    'PR': 'Puerto Rico', 'GU': 'Guam', 'VI': 'U.S. Virgin Islands',
}

# ============================================================
# CANADIAN PROVINCES
# ============================================================
CA_PROVINCES = {
    'AB': 'Alberta', 'BC': 'British Columbia', 'MB': 'Manitoba',
    'NB': 'New Brunswick', 'NL': 'Newfoundland and Labrador',
    'NS': 'Nova Scotia', 'NT': 'Northwest Territories', 'NU': 'Nunavut',
    'ON': 'Ontario', 'PE': 'Prince Edward Island', 'QC': 'Quebec',
    'SK': 'Saskatchewan', 'YT': 'Yukon',
}

# ============================================================
# AUSTRALIAN STATES
# ============================================================
AU_STATES = {
    'NSW': 'New South Wales', 'VIC': 'Victoria', 'QLD': 'Queensland',
    'WA': 'Western Australia', 'SA': 'South Australia', 'TAS': 'Tasmania',
    'ACT': 'Australian Capital Territory', 'NT': 'Northern Territory',
}

# ============================================================
# BRAZILIAN STATES
# ============================================================
BR_STATES = {
    'SP': 'São Paulo', 'RJ': 'Rio de Janeiro', 'MG': 'Minas Gerais',
    'BA': 'Bahia', 'RS': 'Rio Grande do Sul', 'PR': 'Paraná',
    'PE': 'Pernambuco', 'CE': 'Ceará', 'PA': 'Pará', 'SC': 'Santa Catarina',
    'GO': 'Goiás', 'MA': 'Maranhão', 'AM': 'Amazonas', 'ES': 'Espírito Santo',
    'PB': 'Paraíba', 'RN': 'Rio Grande do Norte', 'MT': 'Mato Grosso',
    'AL': 'Alagoas', 'PI': 'Piauí', 'DF': 'Distrito Federal',
    'MS': 'Mato Grosso do Sul', 'SE': 'Sergipe', 'RO': 'Rondônia',
    'TO': 'Tocantins', 'AC': 'Acre', 'AP': 'Amapá', 'RR': 'Roraima',
}

# ============================================================
# INDIAN STATES
# ============================================================
IN_STATES = {
    'MH': 'Maharashtra', 'KA': 'Karnataka', 'TN': 'Tamil Nadu',
    'DL': 'Delhi', 'TG': 'Telangana', 'GJ': 'Gujarat',
    'UP': 'Uttar Pradesh', 'RJ': 'Rajasthan', 'WB': 'West Bengal',
    'HR': 'Haryana', 'KL': 'Kerala', 'PB': 'Punjab',
    'AP': 'Andhra Pradesh', 'MP': 'Madhya Pradesh',
}

# ============================================================
# GERMAN STATES
# ============================================================
DE_STATES = {
    'BW': 'Baden-Württemberg', 'BY': 'Bavaria', 'BE': 'Berlin',
    'BB': 'Brandenburg', 'HB': 'Bremen', 'HH': 'Hamburg',
    'HE': 'Hesse', 'MV': 'Mecklenburg-Vorpommern', 'NI': 'Lower Saxony',
    'NW': 'North Rhine-Westphalia', 'RP': 'Rhineland-Palatinate',
    'SL': 'Saarland', 'SN': 'Saxony', 'ST': 'Saxony-Anhalt',
    'SH': 'Schleswig-Holstein', 'TH': 'Thuringia',
}

# ============================================================
# ISO 3166-1 ALPHA-2 → COUNTRY NAMES
# ============================================================
COUNTRIES = {
    'US': 'United States', 'CA': 'Canada', 'GB': 'United Kingdom',
    'DE': 'Germany', 'FR': 'France', 'NL': 'Netherlands',
    'IE': 'Ireland', 'SE': 'Sweden', 'NO': 'Norway', 'DK': 'Denmark',
    'FI': 'Finland', 'CH': 'Switzerland', 'AT': 'Austria', 'BE': 'Belgium',
    'ES': 'Spain', 'IT': 'Italy', 'PT': 'Portugal', 'PL': 'Poland',
    'CZ': 'Czech Republic', 'RO': 'Romania', 'HU': 'Hungary',
    'GR': 'Greece', 'BG': 'Bulgaria', 'HR': 'Croatia', 'SK': 'Slovakia',
    'SI': 'Slovenia', 'EE': 'Estonia', 'LV': 'Latvia', 'LT': 'Lithuania',
    'LU': 'Luxembourg', 'IS': 'Iceland', 'RS': 'Serbia',
    'UA': 'Ukraine', 'RU': 'Russia', 'TR': 'Turkey',
    'JP': 'Japan', 'CN': 'China', 'HK': 'Hong Kong', 'SG': 'Singapore',
    'KR': 'South Korea', 'TW': 'Taiwan', 'IN': 'India', 'AU': 'Australia',
    'NZ': 'New Zealand', 'MY': 'Malaysia', 'TH': 'Thailand',
    'ID': 'Indonesia', 'PH': 'Philippines', 'VN': 'Vietnam',
    'PK': 'Pakistan', 'BD': 'Bangladesh', 'LK': 'Sri Lanka',
    'BR': 'Brazil', 'MX': 'Mexico', 'AR': 'Argentina', 'CL': 'Chile',
    'CO': 'Colombia', 'PE': 'Peru', 'EC': 'Ecuador', 'VE': 'Venezuela',
    'CR': 'Costa Rica', 'PA': 'Panama', 'DO': 'Dominican Republic',
    'GT': 'Guatemala', 'UY': 'Uruguay', 'PY': 'Paraguay', 'BO': 'Bolivia',
    'ZA': 'South Africa', 'KE': 'Kenya', 'NG': 'Nigeria', 'EG': 'Egypt',
    'MA': 'Morocco', 'GH': 'Ghana', 'TZ': 'Tanzania', 'ET': 'Ethiopia',
    'CI': "Côte d'Ivoire", 'SN': 'Senegal', 'MU': 'Mauritius',
    'AE': 'United Arab Emirates', 'SA': 'Saudi Arabia', 'QA': 'Qatar',
    'BH': 'Bahrain', 'KW': 'Kuwait', 'OM': 'Oman', 'JO': 'Jordan',
    'IL': 'Israel', 'IQ': 'Iraq',
    'JM': 'Jamaica', 'TT': 'Trinidad and Tobago', 'CW': 'Curaçao',
    'BM': 'Bermuda', 'KY': 'Cayman Islands', 'BS': 'Bahamas',
}

# State lookups by country
STATE_LOOKUPS = {
    'US': US_STATES,
    'CA': CA_PROVINCES,
    'AU': AU_STATES,
    'BR': BR_STATES,
    'IN': IN_STATES,
    'DE': DE_STATES,
}


def get_country_name(country_code):
    """
    Convert country code to full name.
    
    >>> get_country_name('US')
    'United States'
    >>> get_country_name('BR')
    'Brazil'
    >>> get_country_name('us')
    'United States'
    >>> get_country_name('United States')
    'United States'
    """
    if not country_code:
        return 'Unknown'
    
    code = country_code.strip().upper()
    
    # Already a full name?
    if len(code) > 3:
        return country_code.strip()
    
    return COUNTRIES.get(code, country_code.strip())


def get_state_name(state_code, country_code='US'):
    """
    Convert state/province code to full name.
    
    >>> get_state_name('IL', 'US')
    'Illinois'
    >>> get_state_name('ON', 'CA')
    'Ontario'
    >>> get_state_name('SP', 'BR')
    'São Paulo'
    >>> get_state_name('NSW', 'AU')
    'New South Wales'
    """
    if not state_code:
        return ''
    
    code = state_code.strip().upper()
    cc = (country_code or 'US').strip().upper()
    
    # Check country-specific state lookup
    lookup = STATE_LOOKUPS.get(cc, {})
    if code in lookup:
        return lookup[code]
    
    # Fallback: check US states (most common case)
    if code in US_STATES:
        return US_STATES[code]
    
    # Already a full name?
    if len(code) > 3:
        return state_code.strip()
    
    return state_code.strip()


def resolve_location_name(location_slug):
    """
    Convert a URL slug like 'us-ny' or 'usa-il' or 'br' into a proper display name.
    
    >>> resolve_location_name('us-ny')
    'New York, United States'
    >>> resolve_location_name('usa-il')
    'Illinois, United States'
    >>> resolve_location_name('br')
    'Brazil'
    >>> resolve_location_name('pl')
    'Poland'
    >>> resolve_location_name('de-be')
    'Berlin, Germany'
    >>> resolve_location_name('ca-on')
    'Ontario, Canada'
    """
    if not location_slug:
        return 'Unknown'
    
    slug = location_slug.strip().lower()
    
    # Pattern: country only (e.g., 'br', 'pl', 'de')
    if len(slug) == 2:
        return get_country_name(slug.upper())
    
    # Pattern: country-state (e.g., 'us-ny', 'usa-il', 'de-be', 'ca-on')
    parts = slug.split('-')
    
    if len(parts) == 2:
        country_part = parts[0].upper()
        state_part = parts[1].upper()
        
        # Handle 'USA' prefix (normalize to 'US')
        if country_part == 'USA':
            country_part = 'US'
        
        country_name = get_country_name(country_part)
        state_name = get_state_name(state_part, country_part)
        
        if state_name and state_name != state_part:
            return f"{state_name}, {country_name}"
        else:
            return f"{state_part}, {country_name}"
    
    # Pattern: longer slug (e.g., 'us-new-york') — title case it
    return slug.replace('-', ' ').title()


def format_location_for_title(city=None, state=None, country=None):
    """
    Build a proper location string for page titles and meta descriptions.
    
    >>> format_location_for_title('Ashburn', 'VA', 'US')
    'Ashburn, Virginia, United States'
    >>> format_location_for_title('São Paulo', 'SP', 'BR')
    'São Paulo, São Paulo, Brazil'
    >>> format_location_for_title(None, 'IL', 'US')
    'Illinois, United States'
    >>> format_location_for_title(None, None, 'PL')
    'Poland'
    """
    parts = []
    
    if city:
        parts.append(city.strip())
    
    if state:
        cc = (country or 'US').strip().upper()
        state_full = get_state_name(state, cc)
        # Don't duplicate if city == state name
        if state_full and state_full not in parts:
            parts.append(state_full)
    
    if country:
        country_full = get_country_name(country)
        if country_full not in parts:
            parts.append(country_full)
    
    return ', '.join(parts) if parts else 'Unknown Location'


def format_location_for_meta(city=None, state=None, country=None, facility_count=0, provider_count=0):
    """
    Build SEO meta description for location pages.
    
    >>> format_location_for_meta('Ashburn', 'VA', 'US', 156, 23)
    'Browse 156 data centers from 23 providers in Ashburn, Virginia. Compare colocation facilities, view power capacity, and explore infrastructure on DC Hub.'
    """
    location = format_location_for_title(city, state, country)
    
    parts = []
    if facility_count:
        parts.append(f"Browse {facility_count} data center{'s' if facility_count != 1 else ''}")
    else:
        parts.append("Explore data centers")
    
    if provider_count:
        parts.append(f"from {provider_count} providers")
    
    parts.append(f"in {location}.")
    
    desc = ' '.join(parts)
    desc += " Compare colocation facilities, view power capacity, and explore infrastructure on DC Hub."
    
    return desc


def format_facility_meta(name, provider=None, city=None, state=None, country=None, power_mw=None):
    """
    Build SEO meta description for individual facility pages.
    
    >>> format_facility_meta('DC1', 'Equinix', 'Ashburn', 'VA', 'US', 30.0)
    'Equinix DC1 data center in Ashburn, Virginia. 30.0 MW power capacity. View specs, satellite imagery, nearby infrastructure, and connectivity data on DC Hub.'
    """
    location = format_location_for_title(city, state, country)
    
    parts = []
    if provider and provider not in name:
        parts.append(f"{provider} {name}")
    else:
        parts.append(name or 'Data Center')
    
    parts.append(f"data center in {location}.")
    
    if power_mw and float(power_mw) > 0:
        parts.append(f"{power_mw} MW power capacity.")
    
    parts.append("View specs, satellite imagery, nearby infrastructure, and connectivity data on DC Hub.")
    
    return ' '.join(parts)


# ============================================================
# INTEGRATION HELPERS — drop these into your Flask routes
# ============================================================

def patch_location_route_title(slug):
    """
    Use in your /locations/<slug> route to fix the page title.
    
    Before: "Data Centers in Usa Il"
    After:  "Data Centers in Illinois, United States"
    
    Example usage in main.py:
        from location_names import patch_location_route_title
        
        @app.route('/locations/<slug>')
        def location_page(slug):
            location_display = patch_location_route_title(slug)
            # Use location_display in your template
    """
    return resolve_location_name(slug)


def patch_facility_location(facility_dict):
    """
    Enrich a facility dict with resolved location names.
    Adds 'state_name', 'country_name', and 'location_display' keys.
    
    Example usage:
        facility = get_facility_from_db(id)
        facility = patch_facility_location(facility)
        # facility['location_display'] = "Ashburn, Virginia, United States"
    """
    f = facility_dict.copy()
    
    f['country_name'] = get_country_name(f.get('country', ''))
    f['state_name'] = get_state_name(f.get('state', ''), f.get('country', 'US'))
    f['location_display'] = format_location_for_title(
        f.get('city'), f.get('state'), f.get('country')
    )
    
    return f


# ============================================================
# QUICK TEST
# ============================================================
if __name__ == '__main__':
    print("=== Location Name Resolution Tests ===\n")
    
    test_slugs = [
        'us-ny', 'usa-il', 'br', 'pl', 'de-be', 'ca-on',
        'us-va', 'us-tx', 'us-az', 'us-ga', 'au-nsw',
        'in-mh', 'br-sp', 'de-nw', 'gb', 'sg', 'jp',
    ]
    
    for slug in test_slugs:
        print(f"  '{slug}' → '{resolve_location_name(slug)}'")
    
    print("\n=== Facility Meta Tests ===\n")
    
    print(format_facility_meta('DC1', 'Equinix', 'Ashburn', 'VA', 'US', 30.0))
    print()
    print(format_facility_meta('PHX10', 'Digital Realty', 'Phoenix', 'AZ', 'US', 15.0))
    print()
    print(format_location_for_meta('Ashburn', 'VA', 'US', 156, 23))
    print()
    print(format_location_for_meta(None, 'IL', 'US', 7, 5))
    print()
    print(format_location_for_meta(None, None, 'PL', 6, 4))

import requests
import json
import time
import re
from datetime import datetime

API_BASE = 'http://localhost:5000'
BATCH_SIZE = 50

def submit_batch(facilities):
    if not facilities:
        return {'added': 0, 'skipped': 0}
    try:
        import os
        admin_key = os.environ.get('DCHUB_ADMIN_KEY', '')
        resp = requests.post(f'{API_BASE}/api/facilities/bulk-import',
                           json={'facilities': facilities},
                           headers={'X-Admin-Key': admin_key},
                           timeout=60)
        if resp.status_code == 200:
            return resp.json()
        else:
            print(f"  API error: {resp.status_code} - {resp.text[:200]}")
            return {'added': 0, 'skipped': 0}
    except Exception as e:
        print(f"  Submit error: {e}")
        return {'added': 0, 'skipped': 0}

def submit_all(facilities, source_label=""):
    total_added = 0
    total_skipped = 0
    for i in range(0, len(facilities), BATCH_SIZE):
        batch = facilities[i:i+BATCH_SIZE]
        result = submit_batch(batch)
        total_added += result.get('added', 0)
        total_skipped += result.get('skipped', 0)
        if i > 0 and i % 200 == 0:
            print(f"    Progress: {i}/{len(facilities)} processed, {total_added} added")
        time.sleep(0.5)
    print(f"  {source_label}: {total_added} added, {total_skipped} duplicates")
    return total_added

def grab_wikidata_global():
    print("\n=== Wikidata Global Data Centers ===")
    
    query = """
    SELECT ?item ?itemLabel ?countryLabel ?cityLabel ?coord ?operatorLabel WHERE {
      { ?item wdt:P31 wd:Q1640127. }
      UNION
      { ?item wdt:P31 wd:Q20894835. }
      OPTIONAL { ?item wdt:P17 ?country. }
      OPTIONAL { ?item wdt:P131 ?city. }
      OPTIONAL { ?item wdt:P625 ?coord. }
      OPTIONAL { ?item wdt:P137 ?operator. }
      SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
    }
    LIMIT 5000
    """
    
    try:
        resp = requests.get("https://query.wikidata.org/sparql",
                          params={'query': query, 'format': 'json'},
                          headers={'User-Agent': 'DC Hub Discovery Bot/1.0 (contact@dchub.com)'},
                          timeout=120)
        if resp.status_code == 200:
            data = resp.json()
            results = data.get('results', {}).get('bindings', [])
            print(f"  Found {len(results)} Wikidata entries")
            
            facilities = []
            for r in results:
                name = r.get('itemLabel', {}).get('value', '')
                if not name or name.startswith('Q'):
                    continue
                    
                country = r.get('countryLabel', {}).get('value', '')
                city = r.get('cityLabel', {}).get('value', '')
                operator = r.get('operatorLabel', {}).get('value', '')
                
                lat, lng = None, None
                coord_data = r.get('coord', {}).get('value', '')
                if coord_data and 'Point(' in coord_data:
                    m = re.search(r'Point\(([-\d.]+)\s+([-\d.]+)\)', coord_data)
                    if m:
                        lng, lat = float(m.group(1)), float(m.group(2))

                country_code = country[:2].upper() if country else ''
                
                facilities.append({
                    'name': name,
                    'provider': operator if operator and not operator.startswith('Q') else '',
                    'city': city if city and not city.startswith('Q') else '',
                    'state': '',
                    'country': country_code or country,
                    'latitude': lat,
                    'longitude': lng,
                    'status': 'Operational',
                    'source': 'Wikidata',
                    'source_url': r.get('item', {}).get('value', ''),
                    'source_id': r.get('item', {}).get('value', '').split('/')[-1]
                })
            
            return submit_all(facilities, "Wikidata")
        else:
            print(f"  Wikidata query failed: {resp.status_code}")
    except Exception as e:
        print(f"  Wikidata error: {e}")
    return 0

def grab_peeringdb_global():
    print("\n=== PeeringDB Global Facilities ===")
    
    try:
        resp = requests.get('https://www.peeringdb.com/api/fac',
                          headers={'User-Agent': 'DC Hub Discovery Bot/1.0'},
                          timeout=90)
        
        if resp.status_code == 200:
            data = resp.json()
            fac_list = data.get('data', [])
            print(f"  Found {len(fac_list)} total PeeringDB facilities")
            
            facilities = []
            for fac in fac_list:
                name = fac.get('name', '')
                if not name:
                    continue
                
                facilities.append({
                    'name': name,
                    'provider': (fac.get('org_name', '') or name).split(' - ')[0],
                    'city': fac.get('city', ''),
                    'state': fac.get('state', ''),
                    'country': fac.get('country', ''),
                    'latitude': fac.get('latitude'),
                    'longitude': fac.get('longitude'),
                    'status': 'Operational',
                    'source': 'PeeringDB',
                    'source_url': f"https://www.peeringdb.com/fac/{fac.get('id', '')}",
                    'source_id': f"pdb_{fac.get('id', '')}"
                })
            
            return submit_all(facilities, "PeeringDB")
        else:
            print(f"  PeeringDB failed: {resp.status_code}")
    except Exception as e:
        print(f"  PeeringDB error: {e}")
    return 0

def grab_osm_regional():
    print("\n=== OpenStreetMap Regional Data Centers ===")
    
    overpass_url = "https://overpass-api.de/api/interpreter"
    
    regions = [
        ("North America", "24.0,-130.0,72.0,-60.0"),
        ("Europe", "35.0,-15.0,72.0,45.0"),
        ("East Asia", "15.0,100.0,55.0,150.0"),
        ("SE Asia & Oceania", "-50.0,90.0,15.0,180.0"),
        ("South America", "-56.0,-82.0,15.0,-34.0"),
        ("Middle East & Africa", "-35.0,-20.0,42.0,65.0"),
        ("South Asia", "5.0,60.0,40.0,100.0"),
    ]
    
    total_added = 0
    
    for region_name, bbox in regions:
        query = f"""
        [out:json][timeout:60][bbox:{bbox}];
        (
          node["man_made"="data_centre"];
          way["man_made"="data_centre"];
          node["building"="data_centre"];
          way["building"="data_centre"];
          node["building"="data_center"];
          way["building"="data_center"];
        );
        out center meta;
        """
        
        try:
            print(f"  Querying {region_name}...")
            resp = requests.post(overpass_url, data={'data': query},
                               headers={'User-Agent': 'DC Hub Discovery Bot/1.0'},
                               timeout=90)
            
            if resp.status_code == 200:
                data = resp.json()
                elements = data.get('elements', [])
                print(f"    Found {len(elements)} elements")
                
                facilities = []
                for elem in elements:
                    tags = elem.get('tags', {})
                    
                    if elem.get('type') in ('way', 'relation'):
                        lat = elem.get('center', {}).get('lat')
                        lng = elem.get('center', {}).get('lon')
                    else:
                        lat = elem.get('lat')
                        lng = elem.get('lon')
                    
                    if not lat or not lng:
                        continue
                    
                    name = tags.get('name') or tags.get('operator') or f"DC OSM-{elem.get('id')}"
                    
                    facilities.append({
                        'name': name,
                        'provider': tags.get('operator', ''),
                        'city': tags.get('addr:city', ''),
                        'state': tags.get('addr:state', ''),
                        'country': tags.get('addr:country', tags.get('ISO3166-1:alpha2', '')),
                        'latitude': lat,
                        'longitude': lng,
                        'status': 'Operational',
                        'source': 'OpenStreetMap',
                        'source_id': f"osm_{elem.get('id')}",
                        'source_url': f"https://www.openstreetmap.org/{elem.get('type')}/{elem.get('id')}"
                    })
                
                added = submit_all(facilities, f"OSM {region_name}")
                total_added += added
            else:
                print(f"    {region_name} failed: {resp.status_code}")
            
            time.sleep(5)
            
        except Exception as e:
            print(f"    {region_name} error: {e}")
    
    return total_added

GLOBAL_KNOWN_DCS = [
    {"name": "NTT Tokyo Otemachi", "provider": "NTT Communications", "city": "Tokyo", "country": "JP", "latitude": 35.6892, "longitude": 139.7634},
    {"name": "NTT Osaka Dojima", "provider": "NTT Communications", "city": "Osaka", "country": "JP", "latitude": 34.6937, "longitude": 135.5023},
    {"name": "Equinix TY11 Tokyo", "provider": "Equinix", "city": "Tokyo", "country": "JP", "latitude": 35.6295, "longitude": 139.7399},
    {"name": "KDDI Telehouse Tokyo CC1", "provider": "KDDI", "city": "Tokyo", "country": "JP", "latitude": 35.6838, "longitude": 139.7744},
    {"name": "Digital Realty Singapore SGP", "provider": "Digital Realty", "city": "Singapore", "country": "SG", "latitude": 1.3521, "longitude": 103.8198},
    {"name": "Equinix SG4 Singapore", "provider": "Equinix", "city": "Singapore", "country": "SG", "latitude": 1.3285, "longitude": 103.8444},
    {"name": "ST Telemedia Global Loyang", "provider": "ST Telemedia", "city": "Singapore", "country": "SG", "latitude": 1.3653, "longitude": 103.9631},
    {"name": "Keppel Data Centres Singapore", "provider": "Keppel DC", "city": "Singapore", "country": "SG", "latitude": 1.3080, "longitude": 103.7990},
    {"name": "BDx Shanghai Campus", "provider": "BDx", "city": "Shanghai", "country": "CN", "latitude": 31.2304, "longitude": 121.4737},
    {"name": "GDS Shanghai SH1", "provider": "GDS Holdings", "city": "Shanghai", "country": "CN", "latitude": 31.1689, "longitude": 121.4269},
    {"name": "GDS Beijing BJ1", "provider": "GDS Holdings", "city": "Beijing", "country": "CN", "latitude": 39.9042, "longitude": 116.4074},
    {"name": "Chindata Datong Hyperscale", "provider": "Chindata", "city": "Datong", "country": "CN", "latitude": 40.0757, "longitude": 113.2910},
    {"name": "VNET Group Hebei Campus", "provider": "VNET Group", "city": "Langfang", "state": "Hebei", "country": "CN", "latitude": 39.5380, "longitude": 116.6836},
    {"name": "Equinix LD8 London", "provider": "Equinix", "city": "London", "country": "GB", "latitude": 51.5228, "longitude": -0.0158},
    {"name": "Telehouse London Docklands North", "provider": "KDDI Telehouse", "city": "London", "country": "GB", "latitude": 51.5121, "longitude": -0.0032},
    {"name": "Digital Realty LHR10", "provider": "Digital Realty", "city": "London", "country": "GB", "latitude": 51.4924, "longitude": -0.0100},
    {"name": "Virtus London LONDON5", "provider": "Virtus Data Centres", "city": "London", "country": "GB", "latitude": 51.5566, "longitude": -0.0727},
    {"name": "Interxion Frankfurt FRA15", "provider": "Digital Realty", "city": "Frankfurt", "country": "DE", "latitude": 50.1109, "longitude": 8.6821},
    {"name": "Equinix FR6 Frankfurt", "provider": "Equinix", "city": "Frankfurt", "country": "DE", "latitude": 50.0890, "longitude": 8.6649},
    {"name": "e-shelter Frankfurt Campus", "provider": "NTT Global", "city": "Frankfurt", "country": "DE", "latitude": 50.1070, "longitude": 8.7180},
    {"name": "maincubes FRA01 Frankfurt", "provider": "maincubes", "city": "Frankfurt", "country": "DE", "latitude": 50.0646, "longitude": 8.5910},
    {"name": "Equinix AM7 Amsterdam", "provider": "Equinix", "city": "Amsterdam", "country": "NL", "latitude": 52.3030, "longitude": 4.9440},
    {"name": "Digital Realty AMS17", "provider": "Digital Realty", "city": "Amsterdam", "country": "NL", "latitude": 52.3464, "longitude": 4.8290},
    {"name": "NorthC Groningen", "provider": "NorthC", "city": "Groningen", "country": "NL", "latitude": 53.2194, "longitude": 6.5665},
    {"name": "Equinix PA8 Paris", "provider": "Equinix", "city": "Paris", "country": "FR", "latitude": 48.9284, "longitude": 2.3590},
    {"name": "Data4 Paris-Saclay Campus", "provider": "Data4", "city": "Marcoussis", "country": "FR", "latitude": 48.6306, "longitude": 2.2288},
    {"name": "Scaleway DC5 Paris", "provider": "Scaleway", "city": "Paris", "country": "FR", "latitude": 48.9200, "longitude": 2.3500},
    {"name": "Equinix ML5 Milan", "provider": "Equinix", "city": "Milan", "country": "IT", "latitude": 45.4642, "longitude": 9.1900},
    {"name": "Aruba IT3 Ponte San Pietro", "provider": "Aruba", "city": "Ponte San Pietro", "country": "IT", "latitude": 45.6960, "longitude": 9.5876},
    {"name": "Equinix SK1 Stockholm", "provider": "Equinix", "city": "Stockholm", "country": "SE", "latitude": 59.3293, "longitude": 18.0686},
    {"name": "Hydro66 Boden", "provider": "Hydro66", "city": "Boden", "country": "SE", "latitude": 66.2795, "longitude": 21.6887},
    {"name": "Meta Lulea Campus", "provider": "Meta", "city": "Lulea", "country": "SE", "latitude": 65.5848, "longitude": 22.1547},
    {"name": "Google Hamina Campus", "provider": "Google", "city": "Hamina", "country": "FI", "latitude": 60.5693, "longitude": 27.1878},
    {"name": "Hetzner Falkenstein", "provider": "Hetzner", "city": "Falkenstein", "country": "DE", "latitude": 50.4788, "longitude": 12.3591},
    {"name": "OVHcloud Roubaix RBX", "provider": "OVHcloud", "city": "Roubaix", "country": "FR", "latitude": 50.6922, "longitude": 3.1745},
    {"name": "OVHcloud Gravelines GRA", "provider": "OVHcloud", "city": "Gravelines", "country": "FR", "latitude": 50.9866, "longitude": 2.1278},
    {"name": "Ascenty Campinas SP4", "provider": "Ascenty", "city": "Campinas", "state": "SP", "country": "BR", "latitude": -22.9099, "longitude": -47.0626},
    {"name": "Equinix SP4 Sao Paulo", "provider": "Equinix", "city": "Sao Paulo", "state": "SP", "country": "BR", "latitude": -23.5505, "longitude": -46.6333},
    {"name": "Scala Data Centers Campinas", "provider": "Scala", "city": "Campinas", "state": "SP", "country": "BR", "latitude": -22.9099, "longitude": -47.0626},
    {"name": "Odata Sao Paulo Campus", "provider": "Odata", "city": "Sao Paulo", "state": "SP", "country": "BR", "latitude": -23.4700, "longitude": -46.6100},
    {"name": "Equinix QR1 Queretaro", "provider": "Equinix", "city": "Queretaro", "country": "MX", "latitude": 20.5888, "longitude": -100.3899},
    {"name": "KIO Networks Mexico City", "provider": "KIO Networks", "city": "Mexico City", "country": "MX", "latitude": 19.4326, "longitude": -99.1332},
    {"name": "Equinix ME1 Melbourne", "provider": "Equinix", "city": "Melbourne", "state": "VIC", "country": "AU", "latitude": -37.8136, "longitude": 144.9631},
    {"name": "Equinix SY6 Sydney", "provider": "Equinix", "city": "Sydney", "state": "NSW", "country": "AU", "latitude": -33.8688, "longitude": 151.2093},
    {"name": "AirTrunk SYD1 Sydney", "provider": "AirTrunk", "city": "Sydney", "state": "NSW", "country": "AU", "latitude": -33.8989, "longitude": 151.1908},
    {"name": "NEXTDC S3 Sydney", "provider": "NEXTDC", "city": "Sydney", "state": "NSW", "country": "AU", "latitude": -33.8557, "longitude": 151.1027},
    {"name": "AirTrunk MEL1 Melbourne", "provider": "AirTrunk", "city": "Melbourne", "state": "VIC", "country": "AU", "latitude": -37.7780, "longitude": 144.8060},
    {"name": "Microsoft Azure Johannesburg", "provider": "Microsoft", "city": "Johannesburg", "country": "ZA", "latitude": -26.2041, "longitude": 28.0473},
    {"name": "Teraco JB1 Johannesburg", "provider": "Teraco", "city": "Johannesburg", "country": "ZA", "latitude": -26.1445, "longitude": 28.2271},
    {"name": "Africa Data Centres Nairobi", "provider": "Africa Data Centres", "city": "Nairobi", "country": "KE", "latitude": -1.2921, "longitude": 36.8219},
    {"name": "Raxio Kampala", "provider": "Raxio", "city": "Kampala", "country": "UG", "latitude": 0.3476, "longitude": 32.5825},
    {"name": "Gulf Data Hub Dubai", "provider": "Gulf Data Hub", "city": "Dubai", "country": "AE", "latitude": 25.0711, "longitude": 55.1323},
    {"name": "Equinix DX1 Dubai", "provider": "Equinix", "city": "Dubai", "country": "AE", "latitude": 25.0657, "longitude": 55.1713},
    {"name": "Khazna Abu Dhabi", "provider": "Khazna", "city": "Abu Dhabi", "country": "AE", "latitude": 24.4539, "longitude": 54.3773},
    {"name": "STC Cloud Riyadh", "provider": "STC", "city": "Riyadh", "country": "SA", "latitude": 24.7136, "longitude": 46.6753},
    {"name": "stc Jeddah DC", "provider": "STC", "city": "Jeddah", "country": "SA", "latitude": 21.5433, "longitude": 39.1728},
    {"name": "CtrlS Mumbai Hyperscale", "provider": "CtrlS", "city": "Mumbai", "state": "Maharashtra", "country": "IN", "latitude": 19.0760, "longitude": 72.8777},
    {"name": "NTT Mumbai Campus", "provider": "NTT Global", "city": "Mumbai", "state": "Maharashtra", "country": "IN", "latitude": 19.1136, "longitude": 72.8697},
    {"name": "Yotta NM1 Mumbai", "provider": "Yotta", "city": "Navi Mumbai", "state": "Maharashtra", "country": "IN", "latitude": 19.0330, "longitude": 73.0297},
    {"name": "STT GDC Chennai Campus", "provider": "ST Telemedia", "city": "Chennai", "state": "Tamil Nadu", "country": "IN", "latitude": 13.0827, "longitude": 80.2707},
    {"name": "AdaniConneX Hyderabad", "provider": "AdaniConneX", "city": "Hyderabad", "state": "Telangana", "country": "IN", "latitude": 17.3850, "longitude": 78.4867},
    {"name": "Bridge Data Centres Jakarta", "provider": "Bridge DC", "city": "Jakarta", "country": "ID", "latitude": -6.2088, "longitude": 106.8456},
    {"name": "DCI Indonesia Jakarta", "provider": "DCI Indonesia", "city": "Jakarta", "country": "ID", "latitude": -6.3500, "longitude": 106.6700},
    {"name": "AIMS Cyberjaya", "provider": "AIMS", "city": "Cyberjaya", "country": "MY", "latitude": 2.9213, "longitude": 101.6559},
    {"name": "YTL Data Centre Johor", "provider": "YTL", "city": "Johor", "country": "MY", "latitude": 1.4927, "longitude": 103.7414},
    {"name": "SUNeVision MEGA-i Hong Kong", "provider": "SUNeVision", "city": "Hong Kong", "country": "HK", "latitude": 22.3680, "longitude": 114.1130},
    {"name": "Equinix HK5 Hong Kong", "provider": "Equinix", "city": "Hong Kong", "country": "HK", "latitude": 22.3512, "longitude": 114.1298},
    {"name": "OneAsia Hong Kong TKO", "provider": "OneAsia", "city": "Hong Kong", "country": "HK", "latitude": 22.3072, "longitude": 114.2595},
    {"name": "LG Uplus Pyeongchon", "provider": "LG Uplus", "city": "Anyang", "country": "KR", "latitude": 37.3945, "longitude": 126.9511},
    {"name": "KT Mokdong IDC Seoul", "provider": "KT Corporation", "city": "Seoul", "country": "KR", "latitude": 37.5282, "longitude": 126.8720},
    {"name": "Samsung SDS Suwon DC", "provider": "Samsung SDS", "city": "Suwon", "country": "KR", "latitude": 37.2636, "longitude": 127.0286},
    {"name": "QTS Richmond NAP", "provider": "QTS Realty", "city": "Richmond", "state": "VA", "country": "US", "latitude": 37.5538, "longitude": -77.4603},
    {"name": "DataBank DFW3 Dallas", "provider": "DataBank", "city": "Dallas", "state": "TX", "country": "US", "latitude": 32.8965, "longitude": -96.8680},
    {"name": "DataBank MSP1 Minneapolis", "provider": "DataBank", "city": "Minneapolis", "state": "MN", "country": "US", "latitude": 44.9778, "longitude": -93.2650},
    {"name": "DataBank SLC1 Salt Lake City", "provider": "DataBank", "city": "Salt Lake City", "state": "UT", "country": "US", "latitude": 40.7608, "longitude": -111.8910},
    {"name": "Flexential Portland Campus", "provider": "Flexential", "city": "Portland", "state": "OR", "country": "US", "latitude": 45.5152, "longitude": -122.6784},
    {"name": "Flexential Denver Campus", "provider": "Flexential", "city": "Denver", "state": "CO", "country": "US", "latitude": 39.7392, "longitude": -104.9903},
    {"name": "Stack Infrastructure San Jose SV1", "provider": "Stack Infrastructure", "city": "San Jose", "state": "CA", "country": "US", "latitude": 37.3382, "longitude": -121.8863},
    {"name": "TierPoint St Louis DC", "provider": "TierPoint", "city": "St. Louis", "state": "MO", "country": "US", "latitude": 38.6270, "longitude": -90.1994},
    {"name": "TierPoint Seattle WA1", "provider": "TierPoint", "city": "Seattle", "state": "WA", "country": "US", "latitude": 47.6062, "longitude": -122.3321},
    {"name": "H5 Data Centers Phoenix AZ1", "provider": "H5 Data Centers", "city": "Phoenix", "state": "AZ", "country": "US", "latitude": 33.4484, "longitude": -112.0740},
    {"name": "Compass Datacenters Goodyear AZ", "provider": "Compass Datacenters", "city": "Goodyear", "state": "AZ", "country": "US", "latitude": 33.4353, "longitude": -112.3587},
    {"name": "Compass Datacenters Red Oak TX", "provider": "Compass Datacenters", "city": "Red Oak", "state": "TX", "country": "US", "latitude": 32.5185, "longitude": -96.7975},
    {"name": "CloudHQ Manassas Campus", "provider": "CloudHQ", "city": "Manassas", "state": "VA", "country": "US", "latitude": 38.7509, "longitude": -77.4753},
    {"name": "CloudHQ Ashburn VA1", "provider": "CloudHQ", "city": "Ashburn", "state": "VA", "country": "US", "latitude": 39.0438, "longitude": -77.4874},
    {"name": "Prime Data Centers Santa Clara", "provider": "Prime Data Centers", "city": "Santa Clara", "state": "CA", "country": "US", "latitude": 37.3541, "longitude": -121.9552},
    {"name": "Aligned Energy Plano TX", "provider": "Aligned", "city": "Plano", "state": "TX", "country": "US", "latitude": 33.0198, "longitude": -96.6989},
    {"name": "Aligned Energy Phoenix AZ", "provider": "Aligned", "city": "Phoenix", "state": "AZ", "country": "US", "latitude": 33.4942, "longitude": -112.0260},
    {"name": "Aligned Energy Ashburn VA", "provider": "Aligned", "city": "Ashburn", "state": "VA", "country": "US", "latitude": 39.0438, "longitude": -77.4874},
    {"name": "EdgeCore Austin TX", "provider": "EdgeCore", "city": "Austin", "state": "TX", "country": "US", "latitude": 30.2672, "longitude": -97.7431},
    {"name": "EdgeCore Mesa AZ", "provider": "EdgeCore", "city": "Mesa", "state": "AZ", "country": "US", "latitude": 33.4152, "longitude": -111.8315},
    {"name": "Lincoln Rackhouse Chicago", "provider": "Lincoln Rackhouse", "city": "Chicago", "state": "IL", "country": "US", "latitude": 41.8781, "longitude": -87.6298},
    {"name": "T5 Data Centers Atlanta GA", "provider": "T5 Data Centers", "city": "Atlanta", "state": "GA", "country": "US", "latitude": 33.7490, "longitude": -84.3880},
    {"name": "Sabey Data Centers Ashburn VA", "provider": "Sabey", "city": "Ashburn", "state": "VA", "country": "US", "latitude": 39.0438, "longitude": -77.4874},
    {"name": "Sabey Data Centers NYC", "provider": "Sabey", "city": "New York", "state": "NY", "country": "US", "latitude": 40.7128, "longitude": -74.0060},
    {"name": "Iron Mountain DC Boston", "provider": "Iron Mountain", "city": "Boston", "state": "MA", "country": "US", "latitude": 42.3601, "longitude": -71.0589},
    {"name": "Iron Mountain DC Manassas", "provider": "Iron Mountain", "city": "Manassas", "state": "VA", "country": "US", "latitude": 38.7509, "longitude": -77.4753},
    {"name": "Iron Mountain DC Phoenix", "provider": "Iron Mountain", "city": "Phoenix", "state": "AZ", "country": "US", "latitude": 33.4484, "longitude": -112.0740},
    {"name": "Involta Grand Rapids MI", "provider": "Involta", "city": "Grand Rapids", "state": "MI", "country": "US", "latitude": 42.9634, "longitude": -85.6681},
    {"name": "COPT Defense Properties DC", "provider": "COPT", "city": "Annapolis Junction", "state": "MD", "country": "US", "latitude": 39.1168, "longitude": -76.7755},
    {"name": "Evoque Boca Raton FL", "provider": "Evoque", "city": "Boca Raton", "state": "FL", "country": "US", "latitude": 26.3587, "longitude": -80.0831},
]

def grab_known_facilities():
    print("\n=== Adding Known Global & US Facilities ===")
    for fac in GLOBAL_KNOWN_DCS:
        fac['source'] = 'curated_global'
        fac['source_id'] = f"curated_{fac['name'][:30]}"
        fac['status'] = 'Operational'
    return submit_all(GLOBAL_KNOWN_DCS, "Curated Global + US")

def grab_news_sync():
    print("\n=== Triggering News Sync ===")
    try:
        resp = requests.post(f'{API_BASE}/api/news/sync',
                           headers={'X-API-Key': 'admin'}, timeout=120)
        if resp.status_code == 200:
            data = resp.json()
            print(f"  News sync complete: {data.get('articles_added', 0)} new articles")
            return True
    except Exception as e:
        print(f"  News sync error: {e}")
    return False

def run_all():
    print("=" * 60)
    print(f"DC Hub Data Grabber - {datetime.utcnow().isoformat()}")
    print("=" * 60)
    
    try:
        resp = requests.get(f'{API_BASE}/api/v1/facilities?limit=1')
        before = resp.json().get('total', 0)
    except:
        before = 0
    print(f"\nStarting facility count: {before}")
    
    total_added = 0
    
    total_added += grab_known_facilities()
    
    total_added += grab_wikidata_global()
    
    time.sleep(2)
    total_added += grab_peeringdb_global()
    
    time.sleep(2)
    total_added += grab_osm_regional()
    
    grab_news_sync()
    
    try:
        resp = requests.get(f'{API_BASE}/api/v1/facilities?limit=1')
        after = resp.json().get('total', 0)
    except:
        after = 0
    
    print("\n" + "=" * 60)
    print(f"RESULTS: {before} -> {after} facilities (+{after - before} new)")
    print(f"Total added this run: {total_added}")
    print("=" * 60)
    
    return total_added

if __name__ == '__main__':
    run_all()

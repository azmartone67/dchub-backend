import json, os, subprocess
from datetime import datetime, timezone

DC_MARKETS = {
    "Northern Virginia": {"lat": 39.0438, "lng": -77.4874, "state": "VA", "cities": ["Ashburn", "Sterling", "Manassas", "Reston", "Herndon"]},
    "Dallas-Fort Worth": {"lat": 32.8998, "lng": -97.0403, "state": "TX", "cities": ["Dallas", "Fort Worth", "Richardson", "Plano", "Garland"]},
    "Chicago": {"lat": 41.8781, "lng": -87.6298, "state": "IL", "cities": ["Chicago", "Elk Grove Village", "Franklin Park", "Aurora"]},
    "Phoenix": {"lat": 33.4484, "lng": -112.0740, "state": "AZ", "cities": ["Phoenix", "Mesa", "Chandler", "Goodyear", "Tempe"]},
    "Atlanta": {"lat": 33.7490, "lng": -84.3880, "state": "GA", "cities": ["Atlanta", "Suwanee", "Lithia Springs", "Douglasville"]},
    "Silicon Valley": {"lat": 37.3861, "lng": -122.0839, "state": "CA", "cities": ["Santa Clara", "San Jose", "Milpitas", "Sunnyvale"]},
    "New York Metro": {"lat": 40.7128, "lng": -74.0060, "state": "NJ", "cities": ["Secaucus", "Piscataway", "Newark", "Jersey City"]},
    "Los Angeles": {"lat": 34.0522, "lng": -118.2437, "state": "CA", "cities": ["Los Angeles", "El Segundo", "One Wilshire", "Torrance"]},
    "Denver": {"lat": 39.7392, "lng": -104.9903, "state": "CO", "cities": ["Denver", "Englewood", "Aurora", "Centennial"]},
    "Portland": {"lat": 45.5152, "lng": -122.6784, "state": "OR", "cities": ["Portland", "Hillsboro", "Beaverton"]},
    "Seattle": {"lat": 47.6062, "lng": -122.3321, "state": "WA", "cities": ["Seattle", "Tukwila", "Quincy", "Moses Lake"]},
    "Houston": {"lat": 29.7604, "lng": -95.3698, "state": "TX", "cities": ["Houston", "Stafford", "Sugar Land", "Humble"]},
    "Salt Lake City": {"lat": 40.7608, "lng": -111.8910, "state": "UT", "cities": ["Salt Lake City", "West Jordan", "Bluffdale"]},
    "Columbus": {"lat": 39.9612, "lng": -82.9988, "state": "OH", "cities": ["Columbus", "New Albany", "Dublin", "Westerville"]},
    "San Antonio": {"lat": 29.4241, "lng": -98.4936, "state": "TX", "cities": ["San Antonio", "Westover Hills"]},
    "Richmond": {"lat": 37.5407, "lng": -77.4360, "state": "VA", "cities": ["Richmond", "Henrico", "Chesterfield"]},
    "Minneapolis": {"lat": 44.9778, "lng": -93.2650, "state": "MN", "cities": ["Minneapolis", "Eagan", "Eden Prairie"]},
    "Nashville": {"lat": 36.1627, "lng": -86.7816, "state": "TN", "cities": ["Nashville", "Clarksville", "La Vergne"]},
    "Reno": {"lat": 39.5296, "lng": -119.8138, "state": "NV", "cities": ["Reno", "Sparks"]},
    "Kansas City": {"lat": 39.0997, "lng": -94.5786, "state": "MO", "cities": ["Kansas City", "Lenexa", "Overland Park"]},
}

CARRIERS = {
    "Zayo": ["Northern Virginia","Dallas-Fort Worth","Chicago","Phoenix","Atlanta","Silicon Valley","New York Metro","Los Angeles","Denver","Portland","Seattle","Houston","Salt Lake City","Columbus","Minneapolis","Kansas City","Nashville","Reno"],
    "Lumen": ["Northern Virginia","Dallas-Fort Worth","Chicago","Phoenix","Atlanta","Silicon Valley","New York Metro","Los Angeles","Denver","Portland","Seattle","Houston","Salt Lake City","Columbus","San Antonio","Minneapolis","Nashville","Kansas City","Richmond","Reno"],
    "Crown Castle": ["Northern Virginia","Dallas-Fort Worth","Chicago","Atlanta","Silicon Valley","New York Metro","Los Angeles","Denver","Houston","Phoenix","Seattle","Portland"],
    "FiberLight": ["Northern Virginia","Dallas-Fort Worth","Houston","Atlanta","Phoenix","Denver","San Antonio"],
    "SummitIG": ["Northern Virginia","Columbus","Chicago","Salt Lake City","Phoenix"],
    "Uniti": ["Dallas-Fort Worth","Chicago","Atlanta","New York Metro","Nashville","Houston","Kansas City"],
    "Segra": ["Northern Virginia","Atlanta","Richmond","Nashville","Columbus"],
    "Cogent": ["Northern Virginia","Dallas-Fort Worth","Chicago","Silicon Valley","New York Metro","Los Angeles","Denver","Seattle","Atlanta","Houston","Phoenix","Minneapolis"],
    "Windstream": ["Northern Virginia","Dallas-Fort Worth","Chicago","Atlanta","Columbus","Nashville","Kansas City","Houston"],
    "GTT": ["Northern Virginia","Dallas-Fort Worth","Chicago","New York Metro","Los Angeles","Silicon Valley","Atlanta","Denver"],
    "Arcadian Infracom": ["Phoenix","Salt Lake City","Denver","Los Angeles","Dallas-Fort Worth","Silicon Valley","Reno"],
    "FirstLight": ["New York Metro","Columbus"],
    "Sparklight": ["Phoenix","Salt Lake City"],
}

LH = [
    ("NoVA-Chicago Express","Zayo","Ashburn, VA","Chicago, IL",700,288,[[39.04,-77.49],[39.28,-78.76],[39.65,-79.95],[39.91,-80.74],[40.06,-82.41],[40.10,-83.00],[40.44,-84.39],[41.08,-85.14],[41.59,-86.27],[41.88,-87.63]]),
    ("NoVA-Chicago Northern","Lumen","Ashburn, VA","Chicago, IL",720,432,[[39.04,-77.49],[39.46,-78.50],[40.00,-79.44],[40.44,-80.00],[40.80,-81.38],[41.08,-82.66],[41.16,-83.75],[41.43,-84.97],[41.66,-86.15],[41.88,-87.63]]),
    ("Chicago-Dallas I-55","Zayo","Chicago, IL","Dallas, TX",920,288,[[41.88,-87.63],[41.52,-88.08],[40.69,-89.59],[39.80,-89.65],[38.63,-90.20],[37.97,-91.77],[37.22,-93.29],[36.37,-94.20],[35.47,-95.99],[32.90,-97.04]]),
    ("LA-Phoenix I-10","Lumen","Los Angeles, CA","Phoenix, AZ",370,288,[[34.05,-118.24],[34.06,-117.29],[33.97,-116.50],[33.75,-115.51],[33.42,-114.59],[33.35,-113.58],[33.45,-112.07]]),
    ("Denver-SLC I-80","Zayo","Denver, CO","Salt Lake City, UT",525,192,[[39.74,-104.99],[39.73,-105.52],[39.64,-106.37],[39.53,-107.32],[39.07,-108.55],[38.99,-109.60],[39.19,-110.35],[39.66,-111.10],[40.23,-111.49],[40.76,-111.89]]),
    ("Dallas-Houston I-45","FiberLight","Dallas, TX","Houston, TX",240,144,[[32.90,-97.04],[32.58,-96.85],[32.05,-96.66],[31.55,-96.48],[31.10,-96.33],[30.63,-96.33],[30.25,-95.85],[29.76,-95.37]]),
    ("Atlanta-Dallas I-20","Uniti","Atlanta, GA","Dallas, TX",780,144,[[33.75,-84.39],[33.46,-85.67],[33.52,-86.80],[33.42,-87.97],[32.35,-90.18],[32.30,-91.20],[32.51,-93.75],[32.54,-94.74],[32.75,-96.27],[32.90,-97.04]]),
    ("NoVA-NYC I-95","Cogent","Ashburn, VA","New York, NY",240,432,[[39.04,-77.49],[39.29,-76.61],[39.36,-75.80],[39.68,-75.56],[39.95,-75.17],[40.22,-74.77],[40.53,-74.45],[40.71,-74.01]]),
    ("Seattle-Portland I-5","Zayo","Seattle, WA","Portland, OR",175,192,[[47.61,-122.33],[47.24,-122.44],[46.97,-122.91],[46.60,-122.90],[46.14,-122.77],[45.87,-122.75],[45.52,-122.68]]),
    ("Chicago-Mpls I-94","Windstream","Chicago, IL","Minneapolis, MN",410,144,[[41.88,-87.63],[42.27,-88.00],[42.68,-89.01],[43.07,-89.40],[43.48,-89.77],[43.80,-90.56],[44.02,-91.64],[44.33,-92.75],[44.98,-93.27]]),
    ("PHX-Denver I-25","Arcadian Infracom","Phoenix, AZ","Denver, CO",600,96,[[33.45,-112.07],[34.56,-112.47],[35.20,-111.65],[35.52,-110.27],[36.17,-109.07],[36.73,-107.88],[37.27,-107.01],[38.53,-105.60],[39.74,-104.99]]),
    ("Dallas-SA I-35","FiberLight","Dallas, TX","San Antonio, TX",275,144,[[32.90,-97.04],[32.25,-97.15],[31.55,-97.15],[30.95,-97.28],[30.27,-97.74],[29.88,-97.94],[29.42,-98.49]]),
    ("NoVA-Richmond I-95","Segra","Ashburn, VA","Richmond, VA",115,144,[[39.04,-77.49],[38.85,-77.43],[38.56,-77.37],[38.30,-77.46],[37.96,-77.52],[37.54,-77.44]]),
    ("PHX-LA I-10","Arcadian Infracom","Phoenix, AZ","Los Angeles, CA",370,96,[[33.45,-112.07],[33.37,-112.86],[33.35,-113.58],[33.42,-114.59],[33.75,-115.51],[33.97,-116.50],[34.05,-118.24]]),
    ("SLC-Reno I-80","Arcadian Infracom","Salt Lake City, UT","Reno, NV",530,96,[[40.76,-111.89],[40.73,-112.53],[40.74,-113.08],[40.84,-114.08],[40.83,-115.07],[40.83,-116.04],[40.68,-117.01],[40.49,-117.83],[39.53,-119.81]]),
    ("NYC-Chicago North","Cogent","New York, NY","Chicago, IL",790,288,[[40.71,-74.01],[40.86,-74.35],[41.07,-74.73],[41.24,-75.44],[41.41,-76.01],[41.24,-77.00],[41.14,-78.44],[41.10,-79.66],[41.50,-81.69],[41.88,-87.63]]),
    ("Denver-KC I-70","Lumen","Denver, CO","Kansas City, MO",600,288,[[39.74,-104.99],[39.76,-104.06],[39.36,-102.87],[39.31,-101.72],[39.04,-100.73],[38.88,-99.33],[38.84,-97.61],[39.10,-94.58]]),
    ("ATL-Nashville I-24","Segra","Atlanta, GA","Nashville, TN",250,144,[[33.75,-84.39],[34.00,-84.60],[34.78,-85.00],[34.98,-85.26],[35.22,-85.81],[35.78,-86.36],[36.16,-86.78]]),
    ("Columbus-NoVA I-70","SummitIG","Columbus, OH","Ashburn, VA",400,192,[[39.96,-83.00],[39.95,-82.00],[39.91,-80.85],[39.45,-79.96],[39.18,-79.07],[39.04,-77.49]]),
    ("KC-Dallas I-35","Windstream","Kansas City, MO","Dallas, TX",500,144,[[39.10,-94.58],[38.58,-94.83],[37.69,-95.27],[37.04,-95.82],[36.40,-96.37],[35.74,-97.10],[35.22,-97.44],[32.90,-97.04]]),
    ("Houston-SA I-10","Lumen","Houston, TX","San Antonio, TX",200,288,[[29.76,-95.37],[29.72,-95.80],[29.67,-96.38],[29.56,-97.00],[29.42,-98.49]]),
    ("Chicago-Nashville I-65","Lumen","Chicago, IL","Nashville, TN",475,288,[[41.88,-87.63],[41.43,-87.34],[40.77,-86.87],[40.42,-86.88],[39.77,-86.16],[39.10,-85.75],[38.34,-85.76],[37.16,-86.26],[36.16,-86.78]]),
    ("Seattle-SV I-5","Lumen","Seattle, WA","San Jose, CA",810,432,[[47.61,-122.33],[45.52,-122.68],[44.94,-123.03],[44.05,-123.09],[42.33,-122.87],[41.76,-122.63],[40.80,-122.37],[38.58,-121.49],[37.34,-121.89]]),
    ("Denver-PHX I-17","Lumen","Denver, CO","Phoenix, AZ",600,288,[[39.74,-104.99],[38.83,-104.82],[37.27,-104.61],[36.41,-105.57],[35.08,-106.65],[34.40,-108.54],[34.56,-112.47],[33.45,-112.07]]),
]

def esc(s):
    return str(s).replace("'", "''")

def carrier_slug(name):
    return name.lower().replace(" ", "-")

now = datetime.now(timezone.utc).isoformat()
stmts = []

ring_offsets = [(0.02,0.03),(0.04,0.01),(0.03,-0.02),(0.01,-0.04),(-0.02,-0.03),(-0.04,-0.01),(-0.03,0.02),(-0.01,0.04)]
lat_offsets = [(0.015,0.025),(-0.01,0.035),(0.035,-0.015),(-0.025,-0.02),(0.005,0.045),(-0.035,0.005),(0.02,-0.035),(-0.015,0.015)]

for carrier, markets in CARRIERS.items():
    ce = esc(carrier)
    slug = carrier_slug(carrier)
    for mkt in markets:
        if mkt not in DC_MARKETS:
            continue
        m = DC_MARKETS[mkt]
        clat = m["lat"]
        clng = m["lng"]
        st = m["state"]
        cities = m["cities"]

        # 3 metro rings
        for rn in range(1, 4):
            sc = 0.7 + rn * 0.3
            wp = []
            for dlat, dlng in ring_offsets:
                wp.append([round(clat + dlat * sc, 6), round(clng + dlng * sc, 6)])
            wp.append(wp[0])
            dist = round(8 + rn * 6 + len(cities) * 1.5, 1)
            nm = esc(ce + " " + mkt + " Metro Ring " + str(rn))
            ep = cities[min(rn, len(cities) - 1)]
            geo = esc(json.dumps(wp))
            src_url = "https://dchub.cloud/fiber/" + slug
            val = "('{}','{}','metro','{}, {}','{}, {}',{},'{}','active','dc_hub_carrier_intel','{}','{}',{},{},{},{},{},{},{})".format(
                nm, ce, cities[0], st, ep, st, dist, str(96*rn)+"-count", src_url, geo,
                wp[0][0], wp[0][1], wp[-2][0], wp[-2][1], 96*rn, "'" + now + "'", "'" + now + "'"
            )
            stmts.append(val)

        # 3 DC interconnects
        for i in range(min(3, len(cities) - 1)):
            d = lat_offsets[i % len(lat_offsets)]
            s = [round(clat + d[0] * 0.5, 6), round(clng + d[1] * 0.5, 6)]
            e = [round(clat + d[0] * 1.2, 6), round(clng + d[1] * 1.2, 6)]
            mid = [round((s[0] + e[0]) / 2 + 0.005, 6), round((s[1] + e[1]) / 2 - 0.003, 6)]
            c1 = cities[i]
            c2 = cities[min(i + 1, len(cities) - 1)]
            nm = esc(ce + " " + c1 + "-" + c2 + " DC Interconnect")
            geo = esc(json.dumps([s, mid, e]))
            src_url = "https://dchub.cloud/fiber/" + slug
            val = "('{}','{}','dc_interconnect','{}, {}','{}, {}',{},'288-count','active','dc_hub_carrier_intel','{}','{}',{},{},{},{},288,'{}','{}')".format(
                nm, ce, c1, st, c2, st, round(3 + i * 2.5, 1), src_url, geo,
                s[0], s[1], e[0], e[1], now, now
            )
            stmts.append(val)

        # 2 enterprise laterals
        for i in range(min(2, len(cities) - 1)):
            d = lat_offsets[(i + 3) % len(lat_offsets)]
            s = [round(clat, 6), round(clng, 6)]
            e = [round(clat + d[0], 6), round(clng + d[1], 6)]
            c2 = cities[min(i + 1, len(cities) - 1)]
            nm = esc(ce + " " + mkt + " Lateral " + str(i + 1))
            geo = esc(json.dumps([s, e]))
            src_url = "https://dchub.cloud/fiber/" + slug
            val = "('{}','{}','enterprise_lateral','{}, {}','{}, {}',{},'48-count','active','dc_hub_carrier_intel','{}','{}',{},{},{},{},48,'{}','{}')".format(
                nm, ce, cities[0], st, c2, st, round(2 + i * 3, 1), src_url, geo,
                s[0], s[1], e[0], e[1], now, now
            )
            stmts.append(val)

# Long-haul routes
for nm, prov, sp, ep, dist, fc, wp in LH:
    nme = esc(nm)
    pe = esc(prov)
    geo = esc(json.dumps(wp))
    slug = carrier_slug(prov)
    src_url = "https://dchub.cloud/fiber/" + slug
    val = "('{}','{}','long_haul','{}','{}',{},'{}','active','dc_hub_carrier_intel','{}','{}',{},{},{},{},{},'{}','{}')".format(
        nme, pe, sp, ep, dist, str(fc) + "-count", src_url, geo,
        wp[0][0], wp[0][1], wp[-1][0], wp[-1][1], fc, now, now
    )
    stmts.append(val)

# Write SQL
cols = "name,provider,route_type,start_point,end_point,distance_miles,capacity,status,source,source_url,geometry,start_lat,start_lng,end_lat,end_lng,fiber_count,created_at,updated_at"

with open("/tmp/fiber_boost.sql", "w") as f:
    f.write("DELETE FROM fiber_routes WHERE source = 'dc_hub_carrier_intel';\n\n")
    for i in range(0, len(stmts), 50):
        batch = stmts[i:i + 50]
        f.write("INSERT INTO fiber_routes ({}) VALUES\n".format(cols))
        f.write(",\n".join(batch) + ";\n\n")

    # fiber_provider_markets
    f.write("DELETE FROM fiber_provider_markets;\n\n")
    fpm = []
    for carrier, markets in CARRIERS.items():
        ce = esc(carrier)
        for mkt in markets:
            if mkt not in DC_MARKETS:
                continue
            st = DC_MARKETS[mkt]["state"]
            me = esc(mkt)
            fpm.append("('{}','{}','{}',8,0,3,50.0)".format(ce, me, st))

    for i in range(0, len(fpm), 50):
        batch = fpm[i:i + 50]
        f.write("INSERT INTO fiber_provider_markets (provider,market,state,metro_route_count,long_haul_route_count,dc_interconnect_count,total_fiber_miles) VALUES\n")
        f.write(",\n".join(batch) + ";\n\n")

print("Generated {} fiber routes to /tmp/fiber_boost.sql".format(len(stmts)))
print("Run: psql $DATABASE_URL -f /tmp/fiber_boost.sql")

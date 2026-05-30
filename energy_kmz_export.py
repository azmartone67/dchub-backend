"""
DC Hub Energy Discovery — KMZ/KML Export
==========================================
Exports discovered energy infrastructure as KMZ files for Google Earth.

Usage:
  - As Flask endpoint: Register with register_kmz_export_routes(app)
    GET /api/energy-discovery/export/kmz%stype=power-plants&market=chicago
    GET /api/energy-discovery/export/kmz%stype=all
    GET /api/energy-discovery/export/kmz%stype=pipelines
    GET /api/energy-discovery/export/kmz%stype=transmission-lines&market=northern_virginia

  - Standalone: DATABASE_URL=$NEON_DATABASE_URL python3 energy_kmz_export.py
    Generates files in /workspace/exports/
"""

import os
import io
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from flask import request, send_file, jsonify

try:
    from db_utils import get_db
except ImportError:
    import psycopg2
    def get_db():
        return psycopg2.connect(os.environ['DATABASE_URL'])


# =============================================================================
# FUEL TYPE COLORS (ABGR format for KML — alpha, blue, green, red)
# =============================================================================

FUEL_COLORS = {
    'Nuclear':      'ff0000ff',  # red
    'Natural Gas':  'ff00a5ff',  # orange
    'Coal':         'ff404040',  # dark gray
    'Wind':         'ffffcc00',  # cyan-ish
    'Solar':        'ff00ffff',  # yellow
    'Hydro':        'ffff6600',  # blue
    'Pumped Storage': 'ffff9933', # light blue
    'Petroleum':    'ff336699',  # brown
    'Biomass':      'ff009933',  # green
    'Geothermal':   'ff0066cc',  # dark orange
    'Other':        'ff999999',  # gray
}

FUEL_ICONS = {
    'Nuclear':      'http://maps.google.com/mapfiles/kml/shapes/electricity.png',
    'Natural Gas':  'http://maps.google.com/mapfiles/kml/shapes/firedept.png',
    'Coal':         'http://maps.google.com/mapfiles/kml/shapes/mining.png',
    'Wind':         'http://maps.google.com/mapfiles/kml/shapes/wind-1.png',
    'Solar':        'http://maps.google.com/mapfiles/kml/shapes/sunny.png',
    'Hydro':        'http://maps.google.com/mapfiles/kml/shapes/water.png',
    'Default':      'http://maps.google.com/mapfiles/kml/shapes/electricity.png',
}

PIPELINE_COLOR = 'ff0055ff'  # red-orange
TX_LINE_COLOR  = 'ff00ccff'  # yellow-orange


def get_fuel_color(fuel_type):
    """Get KML color for fuel type"""
    if not fuel_type:
        return FUEL_COLORS['Other']
    ft = fuel_type.strip()
    for key, color in FUEL_COLORS.items():
        if key.lower() in ft.lower():
            return color
    return FUEL_COLORS['Other']


def get_fuel_icon(fuel_type):
    """Get icon URL for fuel type"""
    if not fuel_type:
        return FUEL_ICONS['Default']
    ft = fuel_type.strip()
    for key, icon in FUEL_ICONS.items():
        if key.lower() in ft.lower():
            return icon
    return FUEL_ICONS['Default']


def _dict_rows(cursor):
    """Convert cursor results to list of dicts"""
    if not cursor.description:
        return []
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


# =============================================================================
# KML GENERATION
# =============================================================================

def generate_power_plants_kml(market=None, state=None, fuel_type=None, min_capacity=None):
    """Generate KML for power plants"""
    conn = get_db()
    try:
        c = conn.cursor()

        query = "SELECT * FROM discovered_power_plants WHERE lat IS NOT NULL AND lng IS NOT NULL"
        params = []
        if market:
            query += " AND market = %s"; params.append(market)
        if state:
            query += " AND state = %s"; params.append(state)
        if fuel_type:
            query += " AND fuel_type ILIKE %s"; params.append(f"%{fuel_type}%")
        if min_capacity:
            query += " AND capacity_mw >= %s"; params.append(float(min_capacity))
        query += " ORDER BY capacity_mw DESC"

        c.execute(query, params)
        plants = _dict_rows(c)
    finally:
        conn.close()

    title = "DC Hub — Power Plants"
    if market:
        title += f" ({market})"

    kml = _build_kml_header(title, f"Power plants from DC Hub Energy Discovery. Generated {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}. Total: {len(plants)} plants.")

    # Create style for each fuel type
    styles_added = set()
    for plant in plants:
        ft = (plant.get('fuel_type') or 'Other').strip()
        style_id = f"style-{ft.replace(' ', '_').lower()}"
        if style_id not in styles_added:
            styles_added.add(style_id)
            color = get_fuel_color(ft)
            icon = get_fuel_icon(ft)
            kml += f"""  <Style id="{style_id}">
    <IconStyle>
      <color>{color}</color>
      <scale>0.8</scale>
      <Icon><href>{icon}</href></Icon>
    </IconStyle>
    <LabelStyle><scale>0.7</scale></LabelStyle>
  </Style>
"""

    # Group by fuel type into folders
    by_fuel = {}
    for plant in plants:
        ft = (plant.get('fuel_type') or 'Other').strip()
        if ft not in by_fuel:
            by_fuel[ft] = []
        by_fuel[ft].append(plant)

    for ft, ft_plants in sorted(by_fuel.items(), key=lambda x: -sum(p.get('capacity_mw', 0) or 0 for p in x[1])):
        total_mw = sum(p.get('capacity_mw', 0) or 0 for p in ft_plants)
        style_id = f"style-{ft.replace(' ', '_').lower()}"
        kml += f"""  <Folder>
    <name>{ft} ({len(ft_plants)} plants, {total_mw:,.0f} MW)</name>
    <open>0</open>
"""
        for plant in ft_plants:
            name = _xml_escape(plant.get('name', 'Unknown'))
            cap = plant.get('capacity_mw', 0) or 0
            operator = _xml_escape(plant.get('operator', 'Unknown'))
            state_code = plant.get('state', '')
            mkt = plant.get('market', '')
            source = plant.get('source', '')
            lat = plant['lat']
            lng = plant['lng']

            desc = f"""<![CDATA[
<b>Capacity:</b> {cap:,.1f} MW<br/>
<b>Fuel Type:</b> {ft}<br/>
<b>Operator:</b> {operator}<br/>
<b>State:</b> {state_code}<br/>
<b>Market:</b> {mkt}<br/>
<b>Source:</b> {source}<br/>
<b>Data:</b> <a href="https://dchub.cloud">DC Hub</a>
]]>"""

            kml += f"""    <Placemark>
      <name>{name} ({cap:,.0f} MW)</name>
      <description>{desc}</description>
      <styleUrl>#{style_id}</styleUrl>
      <Point><coordinates>{lng},{lat},0</coordinates></Point>
    </Placemark>
"""
        kml += "  </Folder>\n"

    kml += _build_kml_footer()
    return kml, len(plants)


def generate_pipelines_kml(state=None):
    """Generate KML for gas pipelines"""
    conn = get_db()
    try:
        c = conn.cursor()

        query = "SELECT * FROM discovered_pipelines WHERE 1=1"
        params = []
        if state:
            query += " AND (state = %s OR states_served LIKE %s)"
            params.extend([state, f"%{state}%"])
        query += " ORDER BY capacity_mdth DESC"

        c.execute(query, params)
        pipelines = _dict_rows(c)
    finally:
        conn.close()

    title = "DC Hub — Gas Pipelines"
    if state:
        title += f" ({state})"

    kml = _build_kml_header(title, f"Interstate gas pipelines. Generated {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}. Total: {len(pipelines)} pipelines.")

    kml += f"""  <Style id="pipeline-style">
    <IconStyle>
      <color>{PIPELINE_COLOR}</color>
      <scale>1.0</scale>
      <Icon><href>http://maps.google.com/mapfiles/kml/shapes/gas_stations.png</href></Icon>
    </IconStyle>
    <LabelStyle><scale>0.8</scale></LabelStyle>
  </Style>
"""

    for pipe in pipelines:
        name = _xml_escape(pipe.get('name', 'Unknown'))
        operator = _xml_escape(pipe.get('operator', 'Unknown'))
        cap = pipe.get('capacity_mdth', 0) or 0
        diameter = pipe.get('diameter_inches', 0) or 0
        states = pipe.get('states_served', '')
        lat = pipe.get('lat', 0)
        lng = pipe.get('lng', 0)

        if not lat or not lng:
            continue

        desc = f"""<![CDATA[
<b>Operator:</b> {operator}<br/>
<b>Capacity:</b> {cap:,.0f} MDth/day<br/>
<b>Diameter:</b> {diameter}" <br/>
<b>States:</b> {states}<br/>
<b>Status:</b> {pipe.get('status', 'Active')}<br/>
<b>Data:</b> <a href="https://dchub.cloud">DC Hub</a>
]]>"""

        kml += f"""  <Placemark>
    <name>{name}</name>
    <description>{desc}</description>
    <styleUrl>#pipeline-style</styleUrl>
    <Point><coordinates>{lng},{lat},0</coordinates></Point>
  </Placemark>
"""

    kml += _build_kml_footer()
    return kml, len(pipelines)


def generate_transmission_kml(market=None, min_voltage=None):
    """Generate KML for transmission lines"""
    conn = get_db()
    try:
        c = conn.cursor()

        query = "SELECT * FROM discovered_transmission_lines WHERE 1=1"
        params = []
        if market:
            query += " AND market = %s"; params.append(market)
        if min_voltage:
            query += " AND voltage_kv >= %s"; params.append(float(min_voltage))
        query += " ORDER BY voltage_kv DESC LIMIT 1000"

        c.execute(query, params)
        lines = _dict_rows(c)
    finally:
        conn.close()

    title = "DC Hub — Transmission Lines"
    if market:
        title += f" ({market})"

    kml = _build_kml_header(title, f"Transmission lines. Generated {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}. Total: {len(lines)} lines. Note: Point markers only (line geometry not stored).")

    kml += f"""  <Style id="tx-style">
    <IconStyle>
      <color>{TX_LINE_COLOR}</color>
      <scale>0.6</scale>
      <Icon><href>http://maps.google.com/mapfiles/kml/shapes/target.png</href></Icon>
    </IconStyle>
    <LabelStyle><scale>0.6</scale></LabelStyle>
  </Style>
"""

    for line in lines:
        owner = _xml_escape(line.get('owner', 'Unknown'))
        voltage = line.get('voltage_kv', 0) or 0
        volt_class = line.get('volt_class', '')
        sub1 = _xml_escape(line.get('sub_1', ''))
        sub2 = _xml_escape(line.get('sub_2', ''))
        mkt = line.get('market', '')

        desc = f"""<![CDATA[
<b>Owner:</b> {owner}<br/>
<b>Voltage:</b> {voltage:,.0f} kV ({volt_class})<br/>
<b>Substations:</b> {sub1} → {sub2}<br/>
<b>Market:</b> {mkt}<br/>
<b>Data:</b> <a href="https://dchub.cloud">DC Hub</a>
]]>"""

        kml += f"""  <Placemark>
    <name>{owner} ({voltage:,.0f} kV)</name>
    <description>{desc}</description>
    <styleUrl>#tx-style</styleUrl>
  </Placemark>
"""

    kml += _build_kml_footer()
    return kml, len(lines)


def generate_all_kml(market=None):
    """Generate combined KML with all energy infrastructure"""
    conn = get_db()
    try:
        c = conn.cursor()

        # Get power plants
        query = "SELECT * FROM discovered_power_plants WHERE lat IS NOT NULL AND lng IS NOT NULL"
        params = []
        if market:
            query += " AND market = %s"; params.append(market)
        query += " ORDER BY capacity_mw DESC"
        c.execute(query, params)
        plants = _dict_rows(c)

        # Get pipelines
        c.execute("SELECT * FROM discovered_pipelines ORDER BY capacity_mdth DESC")
        pipelines = _dict_rows(c)

        # Get transmission lines
        tx_query = "SELECT * FROM discovered_transmission_lines WHERE 1=1 ORDER BY voltage_kv DESC LIMIT 1000"
        tx_params = []
        if market:
            tx_query += " AND market = %s"; tx_params.append(market)
        tx_query += " ORDER BY voltage_kv DESC LIMIT 500"
        c.execute(tx_query, tx_params)
        tx_lines = _dict_rows(c)

        # Get stats
        c.execute("SELECT COALESCE(SUM(capacity_mw), 0) FROM discovered_power_plants WHERE lat IS NOT NULL")
        total_mw = float(c.fetchone()[0])

    finally:
        conn.close()

    title = "DC Hub — Energy Infrastructure"
    if market:
        title += f" ({market})"

    desc = f"Complete energy infrastructure from DC Hub. Generated {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}."
    desc += f" {len(plants):,} power plants ({total_mw:,.0f} MW), {len(pipelines)} pipelines, {len(tx_lines):,} transmission lines."

    kml = _build_kml_header(title, desc)

    # Styles
    styles_added = set()
    for plant in plants:
        ft = (plant.get('fuel_type') or 'Other').strip()
        style_id = f"style-{ft.replace(' ', '_').lower()}"
        if style_id not in styles_added:
            styles_added.add(style_id)
            kml += f"""  <Style id="{style_id}">
    <IconStyle><color>{get_fuel_color(ft)}</color><scale>0.7</scale><Icon><href>{get_fuel_icon(ft)}</href></Icon></IconStyle>
    <LabelStyle><scale>0.6</scale></LabelStyle>
  </Style>
"""

    kml += f"""  <Style id="pipeline-style">
    <IconStyle><color>{PIPELINE_COLOR}</color><scale>1.0</scale><Icon><href>http://maps.google.com/mapfiles/kml/shapes/gas_stations.png</href></Icon></IconStyle>
    <LabelStyle><scale>0.8</scale></LabelStyle>
  </Style>
  <Style id="tx-style">
    <IconStyle><color>{TX_LINE_COLOR}</color><scale>0.5</scale><Icon><href>http://maps.google.com/mapfiles/kml/shapes/target.png</href></Icon></IconStyle>
    <LabelStyle><scale>0</scale></LabelStyle>
  </Style>
"""

    # Power Plants folder (grouped by fuel)
    kml += f"""  <Folder>
    <name>Power Plants ({len(plants):,} — {total_mw:,.0f} MW)</name>
    <open>1</open>
"""
    by_fuel = {}
    for plant in plants:
        ft = (plant.get('fuel_type') or 'Other').strip()
        by_fuel.setdefault(ft, []).append(plant)

    for ft, ft_plants in sorted(by_fuel.items(), key=lambda x: -sum(p.get('capacity_mw', 0) or 0 for p in x[1])):
        ft_mw = sum(p.get('capacity_mw', 0) or 0 for p in ft_plants)
        style_id = f"style-{ft.replace(' ', '_').lower()}"
        kml += f"""    <Folder>
      <name>{ft} ({len(ft_plants)}, {ft_mw:,.0f} MW)</name>
      <open>0</open>
"""
        for p in ft_plants:
            name = _xml_escape(p.get('name', 'Unknown'))
            cap = p.get('capacity_mw', 0) or 0
            kml += f"""      <Placemark>
        <name>{name} ({cap:,.0f} MW)</name>
        <description><![CDATA[<b>Fuel:</b> {ft}<br/><b>Capacity:</b> {cap:,.1f} MW<br/><b>Operator:</b> {_xml_escape(p.get('operator',''))}<br/><b>State:</b> {p.get('state','')}<br/><b>Market:</b> {p.get('market','')}]]></description>
        <styleUrl>#{style_id}</styleUrl>
        <Point><coordinates>{p['lng']},{p['lat']},0</coordinates></Point>
      </Placemark>
"""
        kml += "    </Folder>\n"
    kml += "  </Folder>\n"

    # Pipelines folder
    kml += f"""  <Folder>
    <name>Gas Pipelines ({len(pipelines)})</name>
    <open>0</open>
"""
    for pipe in pipelines:
        lat, lng = pipe.get('lat', 0), pipe.get('lng', 0)
        if not lat or not lng:
            continue
        name = _xml_escape(pipe.get('name', 'Unknown'))
        cap = pipe.get('capacity_mdth', 0) or 0
        kml += f"""    <Placemark>
      <name>{name} ({cap:,.0f} MDth/d)</name>
      <description><![CDATA[<b>Operator:</b> {_xml_escape(pipe.get('operator',''))}<br/><b>Capacity:</b> {cap:,.0f} MDth/day<br/><b>States:</b> {pipe.get('states_served','')}]]></description>
      <styleUrl>#pipeline-style</styleUrl>
      <Point><coordinates>{lng},{lat},0</coordinates></Point>
    </Placemark>
"""
    kml += "  </Folder>\n"

    # Transmission folder
    if tx_lines:
        kml += f"""  <Folder>
    <name>Transmission Lines ({len(tx_lines):,})</name>
    <open>0</open>
    <description>Point markers — line geometry not stored</description>
"""
        for line in tx_lines:
            owner = _xml_escape(line.get('owner', 'Unknown'))
            voltage = line.get('voltage_kv', 0) or 0
            kml += f"""    <Placemark>
      <name>{owner} ({voltage:,.0f} kV)</name>
      <styleUrl>#tx-style</styleUrl>
    </Placemark>
"""
        kml += "  </Folder>\n"

    kml += _build_kml_footer()
    total = len(plants) + len(pipelines) + len(tx_lines)
    return kml, total


# =============================================================================
# KML HELPERS
# =============================================================================

def _build_kml_header(title, description):
    # NOTE: keep the XML declaration as a plain string literal — the same
    # %s-in-XML-header regression that broke /sitemap.xml (main.py:18888)
    # had also corrupted this KML header. Use string concatenation so '?' survives.
    xml_decl = '<?xml version="1.0" encoding="UTF-8"?>'
    return xml_decl + f"""
<kml xmlns="http://www.opengis.net/kml/2.2">
<Document>
  <name>{_xml_escape(title)}</name>
  <description>{_xml_escape(description)}</description>
  <open>1</open>
"""


def _build_kml_footer():
    return """</Document>
</kml>
"""


def _xml_escape(text):
    if not text:
        return ''
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;').replace("'", '&apos;')


def kml_to_kmz(kml_string, filename='doc.kml'):
    """Compress KML into KMZ (zip) format"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(filename, kml_string)
    buffer.seek(0)
    return buffer


# =============================================================================
# FLASK ROUTES
# =============================================================================

def register_kmz_export_routes(app):
    """Register KMZ export API routes"""

    @app.route('/api/energy-discovery/export/kmz', methods=['GET'])
    def energy_export_kmz():
        export_type = request.args.get('type', 'all')
        market = request.args.get('market')
        state = request.args.get('state')
        fuel_type = request.args.get('fuel_type')
        min_capacity = request.args.get('min_capacity')
        format_type = request.args.get('format', 'kmz')  # kmz or kml

        try:
            if export_type == 'power-plants':
                kml, count = generate_power_plants_kml(market, state, fuel_type, min_capacity)
                fname = f"dchub_power_plants{'_' + market if market else ''}"
            elif export_type == 'pipelines':
                kml, count = generate_pipelines_kml(state)
                fname = f"dchub_pipelines{'_' + state if state else ''}"
            elif export_type == 'transmission-lines':
                kml, count = generate_transmission_kml(market)
                fname = f"dchub_transmission{'_' + market if market else ''}"
            else:
                kml, count = generate_all_kml(market)
                fname = f"dchub_energy_infrastructure{'_' + market if market else ''}"

            if format_type == 'kml':
                return app.response_class(
                    kml, mimetype='application/vnd.google-earth.kml+xml',
                    headers={'Content-Disposition': f'attachment; filename={fname}.kml'}
                )
            else:
                kmz_buffer = kml_to_kmz(kml)
                return send_file(
                    kmz_buffer, mimetype='application/vnd.google-earth.kmz',
                    as_attachment=True, download_name=f'{fname}.kmz'
                )

        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/energy-discovery/export/summary', methods=['GET'])
    def energy_export_summary():
        """Get export stats without downloading"""
        conn = get_db()
        c = conn.cursor()
        try:
            c.execute("SELECT COUNT(*) FROM discovered_power_plants WHERE lat IS NOT NULL AND lng IS NOT NULL")
            plants_with_coords = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM discovered_power_plants")
            total_plants = c.fetchone()[0]
            c.execute("SELECT COALESCE(SUM(capacity_mw), 0) FROM discovered_power_plants WHERE lat IS NOT NULL")
            total_mw = float(c.fetchone()[0])
            c.execute("SELECT COUNT(*) FROM discovered_pipelines")
            total_pipelines = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM discovered_transmission_lines")
            total_tx = c.fetchone()[0]
            c.execute("SELECT COUNT(DISTINCT market) FROM discovered_power_plants")
            total_markets = c.fetchone()[0]

            return jsonify({
                'success': True,
                'export_available': True,
                'data': {
                    'power_plants': plants_with_coords,
                    'power_plants_total': total_plants,
                    'coordinate_coverage_pct': round(plants_with_coords / total_plants * 100, 1) if total_plants > 0 else 0,
                    'total_capacity_mw': round(total_mw, 1),
                    'total_capacity_gw': round(total_mw / 1000, 1),
                    'pipelines': total_pipelines,
                    'transmission_lines': total_tx,
                    'markets': total_markets,
                },
                'endpoints': {
                    'all': '/api/energy-discovery/export/kmz?type=all',
                    'power_plants': '/api/energy-discovery/export/kmz?type=power-plants',
                    'pipelines': '/api/energy-discovery/export/kmz?type=pipelines',
                    'transmission': '/api/energy-discovery/export/kmz?type=transmission-lines',
                    'by_market': '/api/energy-discovery/export/kmz?type=all&market=chicago',
                    'kml_format': '/api/energy-discovery/export/kmz?type=all&format=kml',
                },
                'generated_at': datetime.utcnow().isoformat(),
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
        finally:
            conn.close()

    print("📦 KMZ Export routes registered")
    print("   ✅ GET /api/energy-discovery/export/kmz")
    print("   ✅ GET /api/energy-discovery/export/summary")


# =============================================================================
# STANDALONE MODE — Generate files locally
# =============================================================================

if __name__ == '__main__':
    import sys

    print("=" * 60)
    print("📦 DC Hub KMZ Export — Standalone Mode")
    print("=" * 60)

    export_dir = os.path.join(os.path.dirname(__file__), 'exports')
    os.makedirs(export_dir, exist_ok=True)

    # Generate all-in-one
    print("\n🔄 Generating combined energy infrastructure KMZ...")
    kml, count = generate_all_kml()
    kmz = kml_to_kmz(kml)
    path = os.path.join(export_dir, 'dchub_energy_infrastructure.kmz')
    with open(path, 'wb') as f:
        f.write(kmz.read())
    print(f"   ✅ {path} ({count:,} features)")

    # Generate per-type
    for export_type, gen_func, fname in [
        ('Power Plants', lambda: generate_power_plants_kml(), 'dchub_power_plants'),
        ('Pipelines', lambda: generate_pipelines_kml(), 'dchub_pipelines'),
    ]:
        print(f"\n🔄 Generating {export_type} KMZ...")
        kml, count = gen_func()
        kmz = kml_to_kmz(kml)
        path = os.path.join(export_dir, f'{fname}.kmz')
        with open(path, 'wb') as f:
            f.write(kmz.read())
        print(f"   ✅ {path} ({count:,} features)")

    print(f"\n✅ All exports saved to {export_dir}/")
    print("   Open in Google Earth or import into Google Maps.")

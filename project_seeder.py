"""
DC Hub - Major Data Center Project Seeder
==========================================
Seeds all known major data center construction projects (2025-2026)
into the facilities database. Runs on startup, skips existing entries.
"""

import sqlite3
import hashlib
import json
import os
import time
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), 'dc_nexus.db')

MAJOR_PROJECTS = [
    {
        'name': 'Stargate Abilene Phase 1',
        'provider': 'Stargate (OpenAI/SoftBank/Oracle)',
        'city': 'Abilene',
        'state': 'TX',
        'country': 'US',
        'power_mw': 1200,
        'status': 'Under Construction',
        'latitude': 32.4487,
        'longitude': -99.7331,
        'raw_data': {
            'investment': '$500B over 4 years total program',
            'sqft': '4,000,000',
            'target_online': 'Mid-2026',
            'total_program_gw': 7.0,
            'partners': 'OpenAI, SoftBank, Oracle, MGX',
            'source_url': 'https://openai.com/index/announcing-the-stargate-project/'
        }
    },
    {
        'name': 'Stargate New Mexico Campus',
        'provider': 'Stargate (OpenAI/SoftBank/Oracle)',
        'city': 'Albuquerque',
        'state': 'NM',
        'country': 'US',
        'power_mw': 500,
        'status': 'Planning',
        'latitude': 35.0844,
        'longitude': -106.6504,
        'raw_data': {
            'investment': 'Part of $500B Stargate program',
            'total_program_gw': 7.0,
            'partners': 'OpenAI, SoftBank, Oracle, MGX',
            'source_url': 'https://openai.com/index/announcing-the-stargate-project/'
        }
    },
    {
        'name': 'Stargate Ohio Campus',
        'provider': 'Stargate (OpenAI/SoftBank/Oracle)',
        'city': 'Columbus',
        'state': 'OH',
        'country': 'US',
        'power_mw': 500,
        'status': 'Planning',
        'latitude': 39.9612,
        'longitude': -82.9988,
        'raw_data': {
            'investment': 'Part of $500B Stargate program',
            'total_program_gw': 7.0,
            'partners': 'OpenAI, SoftBank, Oracle, MGX',
            'source_url': 'https://openai.com/index/announcing-the-stargate-project/'
        }
    },
    {
        'name': 'Vantage Frontier Campus',
        'provider': 'Vantage Data Centers',
        'city': 'Albany',
        'state': 'TX',
        'country': 'US',
        'power_mw': 1400,
        'status': 'Under Construction',
        'latitude': 32.7234,
        'longitude': -99.2973,
        'raw_data': {
            'investment': '$25B',
            'sqft': '3,700,000',
            'land_acres': 1200,
            'buildings': 10,
            'contractor': 'Kiewit',
            'construction_jobs': 5000,
            'announced': 'August 2025',
            'source_url': 'https://www.datacenterdynamics.com'
        }
    },
    {
        'name': 'Vantage Port Washington Campus',
        'provider': 'Vantage Data Centers',
        'city': 'Port Washington',
        'state': 'WI',
        'country': 'US',
        'power_mw': 500,
        'status': 'Under Construction',
        'latitude': 43.3878,
        'longitude': -87.8712,
        'raw_data': {
            'investment': '$15B',
            'land_acres': 672,
            'buildings': 4,
            'broke_ground': '2025',
            'source_url': 'https://www.datacenterdynamics.com'
        }
    },
    {
        'name': 'Anthropic Texas AI Campus',
        'provider': 'Anthropic / Fluidstack',
        'city': 'Dallas',
        'state': 'TX',
        'country': 'US',
        'power_mw': 300,
        'status': 'Planning',
        'latitude': 32.7767,
        'longitude': -96.7970,
        'raw_data': {
            'investment': 'Part of $50B program',
            'permanent_jobs': 800,
            'construction_jobs': 2000,
            'announced': 'December 2025',
            'partners': 'Fluidstack',
            'source_url': 'https://www.anthropic.com'
        }
    },
    {
        'name': 'Anthropic New York AI Campus',
        'provider': 'Anthropic / Fluidstack',
        'city': 'New York',
        'state': 'NY',
        'country': 'US',
        'power_mw': 200,
        'status': 'Planning',
        'latitude': 40.7128,
        'longitude': -74.0060,
        'raw_data': {
            'investment': 'Part of $50B program',
            'announced': 'December 2025',
            'partners': 'Fluidstack',
            'source_url': 'https://www.anthropic.com'
        }
    },
    {
        'name': 'Microsoft Mount Pleasant WI Phase 2',
        'provider': 'Microsoft',
        'city': 'Mount Pleasant',
        'state': 'WI',
        'country': 'US',
        'power_mw': 300,
        'status': 'Planning',
        'latitude': 42.7261,
        'longitude': -87.8784,
        'raw_data': {
            'investment': '$4B',
            'sqft': '1,200,000',
            'announced': 'September 2025',
            'source_url': 'https://www.microsoft.com'
        }
    },
    {
        'name': 'AWS Pennsylvania Campus',
        'provider': 'Amazon Web Services',
        'city': 'Lehigh Valley',
        'state': 'PA',
        'country': 'US',
        'power_mw': 400,
        'status': 'Under Construction',
        'latitude': 40.6023,
        'longitude': -75.4714,
        'raw_data': {
            'investment': '$20B',
            'source_url': 'https://aws.amazon.com'
        }
    },
    {
        'name': 'AWS Indiana Campus',
        'provider': 'Amazon Web Services',
        'city': 'Indianapolis',
        'state': 'IN',
        'country': 'US',
        'power_mw': 300,
        'status': 'Under Construction',
        'latitude': 39.7684,
        'longitude': -86.1581,
        'raw_data': {
            'investment': '$15B',
            'source_url': 'https://aws.amazon.com'
        }
    },
    {
        'name': 'Meta Monroe NC Campus',
        'provider': 'Meta',
        'city': 'Monroe',
        'state': 'NC',
        'country': 'US',
        'power_mw': 250,
        'status': 'Under Construction',
        'latitude': 34.9854,
        'longitude': -80.5515,
        'raw_data': {
            'source_url': 'https://about.meta.com'
        }
    },
    {
        'name': 'Meta Beaver Dam WI',
        'provider': 'Meta',
        'city': 'Beaver Dam',
        'state': 'WI',
        'country': 'US',
        'power_mw': 200,
        'status': 'Under Construction',
        'latitude': 43.4578,
        'longitude': -88.8373,
        'raw_data': {
            'notes': 'Meta 30th data center',
            'began': '2025',
            'source_url': 'https://about.meta.com'
        }
    },
    {
        'name': 'Rocky Mount NC Data Center',
        'provider': 'Undisclosed Hyperscaler',
        'city': 'Rocky Mount',
        'state': 'NC',
        'country': 'US',
        'power_mw': 500,
        'status': 'Planning',
        'latitude': 35.9382,
        'longitude': -77.7905,
        'raw_data': {
            'investment': '$19.2B',
            'construction_start': 'Q1 2026',
            'twin_project': 'Fayetteville, NC',
            'source_url': 'https://www.constructiondive.com'
        }
    },
    {
        'name': 'Fayetteville NC Data Center',
        'provider': 'Undisclosed Hyperscaler',
        'city': 'Fayetteville',
        'state': 'NC',
        'country': 'US',
        'power_mw': 400,
        'status': 'Planning',
        'latitude': 35.0527,
        'longitude': -78.8784,
        'raw_data': {
            'twin_project': 'Rocky Mount, NC ($19.2B combined)',
            'construction_start': 'Q1 2026',
            'source_url': 'https://www.constructiondive.com'
        }
    },
    {
        'name': 'Novva Project Borealis Phase 1',
        'provider': 'Novva Data Centers',
        'city': 'Mesa',
        'state': 'AZ',
        'country': 'US',
        'power_mw': 96,
        'status': 'Under Construction',
        'latitude': 33.4152,
        'longitude': -111.8315,
        'raw_data': {
            'total_campus_mw': 300,
            'total_buildings': 5,
            'phase': '1 of 5',
            'energized': 'End 2026',
            'source_url': 'https://novva.com'
        }
    },
    {
        'name': 'TeraWulf Lake Mariner CB-5',
        'provider': 'TeraWulf / Fluidstack',
        'city': 'Seneca Falls',
        'state': 'NY',
        'country': 'US',
        'power_mw': 200,
        'status': 'Under Construction',
        'latitude': 42.9081,
        'longitude': -76.7886,
        'raw_data': {
            'lessee': 'Fluidstack',
            'online': 'H2 2026',
            'source_url': 'https://www.terawulf.com'
        }
    },
    {
        'name': 'NTT Global Data Centers Frankfurt',
        'provider': 'NTT Global Data Centers',
        'city': 'Frankfurt',
        'state': '',
        'country': 'DE',
        'power_mw': 482,
        'status': 'Planning',
        'latitude': 50.1109,
        'longitude': 8.6821,
        'raw_data': {
            'investment': '$482M+',
            'approved': 'December 2025',
            'construction_start': '2026',
            'source_url': 'https://www.ntt.com'
        }
    },
    {
        'name': 'Princeton Digital Jakarta Hyperscale',
        'provider': 'Princeton Digital Group',
        'city': 'Jakarta',
        'state': '',
        'country': 'ID',
        'power_mw': 120,
        'status': 'Under Construction',
        'latitude': -6.2088,
        'longitude': 106.8456,
        'raw_data': {
            'type': 'Hyperscale Campus',
            'source_url': 'https://www.princetondigitalgroup.com'
        }
    },
    {
        'name': 'Vantage Johor Malaysia',
        'provider': 'Vantage Data Centers',
        'city': 'Johor Bahru',
        'state': '',
        'country': 'MY',
        'power_mw': 300,
        'status': 'Under Construction',
        'latitude': 1.4927,
        'longitude': 103.7414,
        'raw_data': {
            'investment': '$1.6B acquisition',
            'acquired': 'December 2025',
            'type': 'Hyperscale',
            'source_url': 'https://www.vantage-dc.com'
        }
    },
    {
        'name': 'Microsoft Canada Cloud Campus',
        'provider': 'Microsoft',
        'city': 'Toronto',
        'state': 'ON',
        'country': 'CA',
        'power_mw': 200,
        'status': 'Under Construction',
        'latitude': 43.6532,
        'longitude': -79.3832,
        'raw_data': {
            'investment': '$5.4B',
            'online': 'H2 2026',
            'source_url': 'https://www.microsoft.com'
        }
    },
    {
        'name': 'Google Westinghouse Nuclear DC Program',
        'provider': 'Google',
        'city': 'Multiple',
        'state': '',
        'country': 'US',
        'power_mw': 500,
        'status': 'Planning',
        'latitude': 37.4220,
        'longitude': -122.0841,
        'raw_data': {
            'reactors_planned': 10,
            'partner': 'Westinghouse',
            'construction_start': '2030',
            'type': 'Nuclear-powered data centers',
            'source_url': 'https://blog.google'
        }
    },
    {
        'name': 'Meta Kansas City DC',
        'provider': 'Meta',
        'city': 'Kansas City',
        'state': 'MO',
        'country': 'US',
        'power_mw': 150,
        'status': 'active',
        'latitude': 39.0997,
        'longitude': -94.5786,
        'raw_data': {
            'investment': '$1B',
            'opened': '2025',
            'source_url': 'https://about.meta.com'
        }
    },
]

def seed_major_projects():
    """Insert all major projects into facilities table"""
    max_retries = 10
    for attempt in range(max_retries):
        try:
            time.sleep(40 + attempt * 10)
            conn = sqlite3.connect(DB_PATH, timeout=30)
            c = conn.cursor()

            added = 0
            skipped = 0
            for f in MAJOR_PROJECTS:
                source_id = 'proj_' + hashlib.sha256(f['name'].encode()).hexdigest()[:12]
                raw = json.dumps(f.get('raw_data', {}))
                source_url = f.get('raw_data', {}).get('source_url', '')

                c.execute("""
                    INSERT INTO facilities 
                    (id, name, provider, city, state, country, power_mw, status, 
                     latitude, longitude, source, source_id, source_url, raw_data, 
                     first_seen, last_updated, confidence)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'news_pipeline', %s, %s, %s, %s, %s, 0.85)
                """, (
                    source_id, f['name'], f['provider'], f['city'], f.get('state', ''),
                    f['country'], f['power_mw'], f['status'],
                    f.get('latitude'), f.get('longitude'),
                    source_id, source_url, raw,
                    datetime.now().isoformat(), datetime.now().isoformat()
                ))
                if c.rowcount > 0:
                    added += 1
                else:
                    skipped += 1

            conn.commit()
            conn.close()

            total_mw = sum(f['power_mw'] for f in MAJOR_PROJECTS)
            if added > 0:
                print(f"✅ Project Seeder: Added {added} major projects ({total_mw}MW total pipeline), {skipped} already existed")
            else:
                print(f"✅ Project Seeder: All {len(MAJOR_PROJECTS)} major projects already exist")
            return added
        except Exception as e:
            if 'locked' in str(e).lower() and attempt < max_retries - 1:
                continue
            print(f"⚠️ Project Seeder error: {e}")
            return 0

    print("⚠️ Project Seeder: Could not acquire DB lock after retries")
    return 0

if __name__ == '__main__':
    seed_major_projects()

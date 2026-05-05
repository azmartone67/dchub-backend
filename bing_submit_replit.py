"""
Bing URL Submission for DC Hub - Replit Version
Add this file to your Replit project and run it once
"""

import requests
import xml.etree.ElementTree as ET
import time
import os

# Configuration - SET YOUR API KEY HERE
BING_API_KEY = os.environ.get("BING_API_KEY", "YOUR_API_KEY_HERE")
SITE_URL = "https://dchub.cloud"
SITEMAP_URL = f"{SITE_URL}/sitemap.xml"

def submit_to_bing():
    print("=" * 50)
    print("Bing URL Submission - DC Hub")
    print("=" * 50)
    
    if BING_API_KEY == "YOUR_API_KEY_HERE":
        print("\n⚠️  Set your API key first!")
        print("Option 1: Replace YOUR_API_KEY_HERE in this file")
        print("Option 2: Add BING_API_KEY to Replit Secrets")
        return
    
    # Fetch sitemap
    print(f"\nFetching {SITEMAP_URL}...")
    response = requests.get(SITEMAP_URL)
    root = ET.fromstring(response.content)
    
    namespace = {'ns': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
    urls = [url.find('ns:loc', namespace).text for url in root.findall('ns:url', namespace)]
    print(f"Found {len(urls)} URLs")
    
    # Submit in batches of 500
    batch_size = 500
    total = 0
    
    for i in range(0, len(urls), batch_size):
        batch = urls[i:i + batch_size]
        batch_num = (i // batch_size) + 1
        
        response = requests.post(
            f"https://ssl.bing.com/webmaster/api.svc/json/SubmitUrlBatch?apikey={BING_API_KEY}",
            json={"siteUrl": SITE_URL, "urlList": batch},
            headers={'Content-Type': 'application/json'}
        )
        
        if response.status_code == 200:
            print(f"✓ Batch {batch_num}: {len(batch)} URLs submitted")
            total += len(batch)
        else:
            print(f"✗ Batch {batch_num} failed: {response.text}")
        
        time.sleep(1)
    
    print(f"\n{'=' * 50}")
    print(f"✓ Done! Submitted {total} URLs to Bing")
    print("=" * 50)

if __name__ == "__main__":
    submit_to_bing()

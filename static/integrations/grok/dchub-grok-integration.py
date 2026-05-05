import requests
import json

# DC Hub API client
def fetch_dc_hub(endpoint, params=None):
    base_url = "https://dchub.cloud/api/"
    try:
        response = requests.get(base_url + endpoint, params=params)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        return {"error": str(e)}

# Grok API client (replace with your xAI API key)
def query_grok(prompt, api_key):
    url = "https://api.x.ai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    data = {
        "model": "grok-beta",
        "messages": [{"role": "user", "content": prompt}]
    }
    try:
        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()
        return response.json()['choices'][0]['message']['content']
    except requests.exceptions.RequestException as e:
        return {"error": str(e)}

# Example: Fetch site score, analyze with Grok
def analyze_site(lat, lon, xai_api_key):
    data = fetch_dc_hub('site-score', {'lat': lat, 'lon': lon})
    if "error" in data:
        return data
    prompt = f"Analyze this DC Hub site score data: {json.dumps(data)}. Provide risk-adjusted recommendations."
    analysis = query_grok(prompt, xai_api_key)
    return {"data": data, "grok_analysis": analysis}

# Example: Compare energy costs across states
def compare_energy(states, xai_api_key):
    results = {}
    for state in states:
        results[state] = fetch_dc_hub(f'energy/prices/{state}')
    prompt = f"Compare these DC Hub energy pricing datasets and recommend the optimal state for a 50MW data center: {json.dumps(results)}"
    analysis = query_grok(prompt, xai_api_key)
    return {"data": results, "grok_analysis": analysis}

if __name__ == "__main__":
    api_key = "YOUR_XAI_API_KEY"

    # Analyze Ashburn VA
    print("=== Site Analysis: Ashburn VA ===")
    result = analyze_site(39.0437, -77.4875, api_key)
    print(json.dumps(result, indent=2))

    # Compare TX vs VA vs AZ energy
    print("\n=== Energy Comparison ===")
    result = compare_energy(["TX", "VA", "AZ"], api_key)
    print(json.dumps(result, indent=2))

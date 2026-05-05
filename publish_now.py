import requests, json

payload = json.loads(open("payload.json").read())

resp = requests.post(
    "https://dchub-backend-production.up.railway.app/publish/all",
    headers={
        "Authorization": "Bearer nXSHT2_kN6TiFmY5HipcM6wyLyw0398ZLowZn6b1Cm4",
        "Content-Type": "application/json"
    },
    json=payload,
    timeout=30
)
print(resp.status_code)
print(resp.text)

# Twitter / X — tier upgrade walkthrough (round 34, item 2)

Diagnosis from earlier: `/api/v1/marketing/twitter/whoami` returns 403.
The 403 is because your X app is on the **Free tier**, which explicitly
does NOT support `tweet.write` (no posting). You need the **Basic** tier
($100/mo) or higher.

## Steps

### 1. Upgrade to Basic tier ($100/mo)

1. Sign in at https://developer.x.com/portal/products
2. Find your DC Hub app
3. Click "Upgrade" → select **Basic** ($100/mo)
4. Confirm payment

### 2. Verify scopes

1. Go to your app → User authentication settings → Edit
2. App permissions: **Read and Write** (NOT just Read)
3. OAuth 2.0 scopes: `tweet.read tweet.write users.read offline.access`
4. Type of App: Web App, Automated App or Bot
5. Callback URL: `https://api.dchub.cloud/api/v1/marketing/twitter/callback`
6. Website: `https://dchub.cloud`
7. Save

### 3. Regenerate tokens (after permission change)

1. Go to Keys and tokens
2. **OAuth 1.0a Access Token & Secret** → Regenerate
3. Copy the new pair immediately (you can't view secret again)

### 4. Update Railway env vars

1. https://railway.app → resourceful-essence project → dchub-backend service → Variables
2. Update:
   - `TWITTER_ACCESS_TOKEN` ← new access token
   - `TWITTER_ACCESS_TOKEN_SECRET` ← new secret
3. Railway auto-redeploys in ~2 min

### 5. Verify whoami returns ok=true

```bash
curl -sS https://api.dchub.cloud/api/v1/marketing/twitter/whoami | python3 -m json.tool
```

Expected (success):
```json
{
  "ok": true,
  "screen_name": "yourdchub_handle",
  "published_7d": 0,
  ...
}
```

If still 403:
- Confirm tier upgrade went through (check developer.x.com/portal/products)
- Double-check tokens have no leading/trailing whitespace in Railway env vars
- Try regenerating the consumer key/secret too (sometimes permission changes invalidate them)

### 6. Trigger the first auto-publish

Once whoami returns ok=true:

```bash
# Test post (manual trigger)
curl -X POST https://api.dchub.cloud/api/v1/marketing/twitter/publish-next
```

You have 9 approved tweets queued waiting to go. They'll start flowing
on the next auto-publish cron (every 6h).

## Expected impact

Currently auto-publish does **7 LinkedIn posts/week, 0 X posts/week**.

After this fix:
- 7 LinkedIn + ~7 X posts/week (doubles social reach)
- Likely +30-50% more MCP signups/month from social
- Cost: $100/mo for X Basic tier, ROI break-even at ~2 paid signups

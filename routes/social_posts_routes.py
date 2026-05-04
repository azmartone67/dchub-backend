"""Phase 30C — social posts landing pages with OG meta for LinkedIn rich cards.

The auto-poster includes /posts/daily/<YYYY-MM-DD> in its post text.
LinkedIn fetches that URL, reads the OG meta tags, and renders the rich
preview card with a generated image. Dramatically lifts engagement vs.
plain text posts.
"""
from flask import Blueprint, Response, request, redirect
import datetime, json

social_posts_bp = Blueprint('social_posts', __name__)

# Rotation: which ISO is featured on which day of week (0=Mon, 6=Sun)
DAILY_ISO = ['PJM', 'MISO', 'ERCOT', 'CAISO', 'NYISO', 'ISONE', 'SPP']


def _iso_for_date(d):
    return DAILY_ISO[d.weekday() % len(DAILY_ISO)]


@social_posts_bp.route('/api/v1/social/posts/<date>', methods=['GET'])  # phase31_canonical_url — CF-allowlisted
@social_posts_bp.route('/posts/daily/<date>', methods=['GET'])
def daily_post_landing(date):
    """OG-rich landing page for daily LinkedIn auto-posts."""
    try:
        d = datetime.datetime.strptime(date, '%Y-%m-%d').date()
    except ValueError:
        d = datetime.date.today()
        return redirect(f'/posts/daily/{d.isoformat()}', code=302)

    iso = _iso_for_date(d)
    pretty_date = d.strftime('%b %d, %Y')
    card_url = f'https://dchub.cloud/api/v1/grid/{iso}/card.png?d={d.isoformat()}'
    title = f'DC Hub Industry Pulse — {pretty_date}'
    desc = f'{iso} live demand, generation mix, and headroom. Updated every 5 minutes from EIA.'

    schema = {
        '@context': 'https://schema.org',
        '@type': 'Article',
        'headline': title,
        'datePublished': d.isoformat(),
        'image': card_url,
        'publisher': {'@type': 'Organization', 'name': 'DC Hub', 'url': 'https://dchub.cloud'},
        'description': desc,
    }

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{title} | DC Hub</title>
  <meta name="description" content="{desc}">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta property="og:title" content="{title}">
  <meta property="og:description" content="{desc}">
  <meta property="og:image" content="{card_url}">
  <meta property="og:image:width" content="1200">
  <meta property="og:image:height" content="1200">
  <meta property="og:url" content="https://dchub.cloud/api/v1/social/posts/{d.isoformat()}">
  <meta property="og:type" content="article">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:image" content="{card_url}">
  <meta name="twitter:title" content="{title}">
  <meta name="twitter:description" content="{desc}">
  <link rel="canonical" href="https://dchub.cloud/api/v1/social/posts/{d.isoformat()}">
  <script type="application/ld+json">{json.dumps(schema)}</script>
  <style>
    body {{ font-family: -apple-system, system-ui, sans-serif; margin: 0; background: #0a0e1a; color: #e6e9f0; min-height: 100vh; }}
    .wrap {{ max-width: 800px; margin: 0 auto; padding: 4rem 2rem; }}
    h1 {{ font-size: 2.5rem; margin: 0 0 1rem; }}
    .iso-badge {{ display: inline-block; background: #ff6b35; color: #0a0e1a; padding: .35rem .75rem; border-radius: 6px; font-weight: 700; }}
    .card-img {{ width: 100%; max-width: 640px; height: auto; border-radius: 12px; margin: 2rem 0; box-shadow: 0 10px 40px rgba(0,0,0,.4); }}
    .cta {{ display: inline-block; background: #ff6b35; color: #0a0e1a; padding: .85rem 1.75rem; border-radius: 8px; text-decoration: none; font-weight: 600; margin-top: 1rem; }}
    .meta {{ color: #9aa5be; margin-bottom: 1rem; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="iso-badge">{iso}</div>
    <h1>{title}</h1>
    <p class="meta">Live grid intelligence for {iso} ({pretty_date})</p>
    <img src="{card_url}" alt="{iso} grid pulse {pretty_date}" class="card-img">
    <p>{desc}</p>
    <a class="cta" href="/grid/{iso.lower()}">View live {iso} dashboard →</a>
    <p style="margin-top:3rem;color:#6b7593;font-size:.9rem">
      <a href="/grid" style="color:#ff6b35">All ISOs</a> ·
      <a href="/" style="color:#ff6b35">DC Hub home</a>
    </p>
  </div>
</body>
</html>'''
    return Response(html, mimetype='text/html')


@social_posts_bp.route('/api/v1/social/daily-card.png', methods=['GET'])
def daily_card_image():
    """Convenience alias — delegates to the per-ISO card endpoint
    based on the date param's day-of-week."""
    date_str = request.args.get('d', '')
    try:
        d = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        d = datetime.date.today()
    iso = _iso_for_date(d)
    return redirect(f'/api/v1/grid/{iso}/card.png?d={d.isoformat()}', code=302)

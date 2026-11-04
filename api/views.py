import concurrent.futures
import json
from datetime import datetime, timedelta, timezone
import mimetypes
import os
from pathlib import Path
import time

import requests
from django.core.cache import cache
from django.shortcuts import render
from django.utils.safestring import mark_safe
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.views.decorators.http import require_GET

BASE_DIR = Path(__file__).resolve().parent.parent

# API Config
GOLD_API_KEY = os.environ.get('GOLD_API_KEY', 'f2db1aaaee18667e498f2a463de9795215b478a6a91abad885d14e6d5edc5b49')
# Using the exchange rate API you requested
EXCHANGE_RATE_URL = 'https://open.er-api.com/v6/latest/USD'

SITE_URL = 'https://www.sunchadiprice.com'
NPT = timezone(timedelta(hours=5, minutes=45))  # Nepal Time

# Nepal Market Premiums (Nepal retail price / international spot in NPR).
# Calibrated 2026-11-04 against FENEGOSIDA rates (via Hamro Patro), using the
# gold-api.com price at the daily Nepal fix time (10:45 NPT = 05:00 UTC), not the
# day's high. Implied premium vs NRB sell rate: gold 1.223-1.231, silver 1.294-1.298.
# Scaled by ~1.0033 because this app converts with open.er-api's USD/NPR rate,
# which runs ~0.33% below the NRB selling rate the dealers use.
# To recalibrate: premium = nepal_tola_price / (spot_usd_at_05:00_UTC * app_usd_npr * 11.6638 / 31.1035)
NEPAL_PREMIUMS = {
    'gold': 1.232,
    'silver': 1.301,
}
GOLD_PREMIUM_PCT = round((NEPAL_PREMIUMS['gold'] - 1) * 100, 2)
SILVER_PREMIUM_PCT = round((NEPAL_PREMIUMS['silver'] - 1) * 100, 2)

# Fallbacks used only when an upstream API is unreachable (approx. Nov 2026 levels)
FALLBACK_GOLD_USD = 4000
FALLBACK_SILVER_USD = 52
FALLBACK_USD_NPR = 152


@require_GET
def favicon(request):
    favicon_path = BASE_DIR / 'templates' / 'favicon.webp'
    if not favicon_path.exists():
        raise Http404('favicon not found')

    content_type, _ = mimetypes.guess_type(favicon_path.name)
    response = FileResponse(favicon_path.open('rb'), content_type=content_type or 'image/webp')
    response['Cache-Control'] = 'public, max-age=86400'
    return response

def fetch_gold_price():
    """Get gold price from Gold API"""
    try:
        response = requests.get(
            'https://api.gold-api.com/price/XAU',
            headers={'x-api-key': GOLD_API_KEY},
            timeout=5
        )
        print(f"🔧 Gold API Status: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            print(f"🔧 Gold API Response: {data}")
            
            if 'price' in data:
                gold_price = float(data['price'])
                print(f"✅ Got REAL gold price: ${gold_price}")
                return round(gold_price, 2)
            else:
                print(f"⚠️ Unexpected gold response format: {data}")
                if 'data' in data and 'amount' in data['data']:
                    gold_price = float(data['data']['amount'])
                    return round(gold_price, 2)
    except Exception as e:
        print(f"❌ Gold API error: {e}")
    
    print("❌ Gold API failed, using approximation")
    return FALLBACK_GOLD_USD

def fetch_silver_price():
    """Get silver price from Gold API"""
    try:
        response = requests.get(
            'https://api.gold-api.com/price/XAG',
            headers={'x-api-key': GOLD_API_KEY},
            timeout=5
        )
        print(f"🔧 Silver API Status: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            print(f"🔧 Silver API Response: {data}")
            
            if 'price' in data:
                silver_price = float(data['price'])
                print(f"✅ Got REAL silver price: ${silver_price}")
                return round(silver_price, 3)
            else:
                print(f"⚠️ Unexpected silver response format: {data}")
                if 'data' in data and 'amount' in data['data']:
                    silver_price = float(data['data']['amount'])
                    return round(silver_price, 3)
    except Exception as e:
        print(f"❌ Silver API error: {e}")
    
    print("❌ Silver API failed, using approximation")
    return FALLBACK_SILVER_USD

def fetch_exchange_rate():
    """Get USD to NPR rate from open.er-api.com"""
    try:
        response = requests.get(EXCHANGE_RATE_URL, timeout=3)
        if response.status_code == 200:
            data = response.json()
            
            # Check if API call was successful
            if data.get('result') == 'success':
                rate = float(data.get('rates', {}).get('NPR', FALLBACK_USD_NPR))
                print(f"✅ Got exchange rate from open.er-api.com: NPR {rate}")
                return rate
            else:
                print(f"⚠️ Exchange API returned error: {data.get('error-type', 'Unknown error')}")
                return FALLBACK_USD_NPR
    except Exception as e:
        print(f"❌ Exchange API error: {e}")
    
    # Fallback rate
    return FALLBACK_USD_NPR

# Conversion constants
GRAMS_PER_TROY_OUNCE = 31.1035
GRAMS_PER_TOLA = 11.6638
GRAMS_PER_KG = 1000
PRICE_CACHE_KEY = 'nepal_prices_v1'
PRICE_CACHE_SECONDS = 30


def _build_price_data(gold_usd, silver_usd, usd_npr):
    """Convert international spot prices to Nepal prices (with premium)."""
    tola_to_ounce = GRAMS_PER_TOLA / GRAMS_PER_TROY_OUNCE
    kg_to_ounce = GRAMS_PER_KG / GRAMS_PER_TROY_OUNCE
    gp = NEPAL_PREMIUMS['gold']
    sp = NEPAL_PREMIUMS['silver']

    gold_npr = gold_usd * usd_npr * gp
    silver_npr = silver_usd * usd_npr * sp
    gold_tola_npr = gold_npr * tola_to_ounce
    silver_tola_npr = silver_npr * tola_to_ounce
    gold_kg_npr = gold_npr * kg_to_ounce
    silver_kg_npr = silver_npr * kg_to_ounce

    return {
        'gold_usd': round(gold_usd, 2),
        'silver_usd': round(silver_usd, 3),
        'exchange_rate': round(usd_npr, 2),
        'gold_npr': round(gold_npr / 100) * 100,
        'silver_npr': round(silver_npr / 100) * 100,
        'gold_tola_npr': round(gold_tola_npr / 100) * 100,
        'silver_tola_npr': round(silver_tola_npr / 10) * 10,
        'gold_kg_npr': round(gold_kg_npr / 100) * 100,
        'silver_kg_npr': round(silver_kg_npr / 100) * 100,
        'timestamp': int(time.time()),
        'timestamp_human': time.strftime('%Y-%m-%d %H:%M:%S'),
        'source': 'Gold API + open.er-api.com',
        'unit': 'USD per troy ounce',
        'conversion_note': '1 tola = 11.6638g, 1 troy ounce = 31.1035g',
        'premium_applied': True,
        'gold_premium_pct': GOLD_PREMIUM_PCT,
        'silver_premium_pct': SILVER_PREMIUM_PCT,
    }


def compute_prices():
    """Return (data, error). Shared by the JSON API, the HTML page and llms.txt.

    Cached briefly so page renders and API polls don't hammer upstream APIs.
    """
    cached = cache.get(PRICE_CACHE_KEY)
    if cached:
        return cached, None

    print(f"\n📊 Fetching prices at {time.strftime('%Y-%m-%d %H:%M:%S')}")
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            gold_future = executor.submit(fetch_gold_price)
            silver_future = executor.submit(fetch_silver_price)
            exchange_future = executor.submit(fetch_exchange_rate)
            gold_usd = gold_future.result()
            silver_usd = silver_future.result()
            usd_npr = exchange_future.result()

        data = _build_price_data(gold_usd, silver_usd, usd_npr)
        print(f"✅ Gold ${gold_usd:.2f}, Silver ${silver_usd:.3f}, USD/NPR {usd_npr:.2f} "
              f"→ tola: gold {data['gold_tola_npr']:,}, silver {data['silver_tola_npr']:,} "
              f"(+{GOLD_PREMIUM_PCT}% / +{SILVER_PREMIUM_PCT}%)")
        cache.set(PRICE_CACHE_KEY, data, PRICE_CACHE_SECONDS)
        return data, None
    except Exception as e:
        print(f"❌ Error computing prices: {e}")
        data = _build_price_data(FALLBACK_GOLD_USD, FALLBACK_SILVER_USD, FALLBACK_USD_NPR)
        data['source'] = 'Fallback due to error'
        return data, str(e)


@require_GET
def get_prices(request):
    """Fetch all prices with NPR conversion (JSON API)"""
    data, error = compute_prices()
    if error:
        return JsonResponse({'status': 'error', 'message': error, 'data': data})
    json_response = JsonResponse({'status': 'success', 'data': data})
    json_response['Cache-Control'] = 'public, max-age=30, stale-while-revalidate=5'
    json_response['X-Robots-Tag'] = 'noindex'
    return json_response


def _fmt(value, decimals=0):
    return f"{value:,.{decimals}f}"


def _fmt_indian(value):
    """Format an integer with Indian digit grouping (e.g. 2,75,600)."""
    s = str(int(round(value)))
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ','.join(groups) + ',' + tail


@require_GET
def index(request):
    """Render the homepage with live prices already in the HTML.

    Search engines and LLM crawlers (GPTBot, ClaudeBot, PerplexityBot...)
    mostly don't run JavaScript, so prices must be server-rendered.
    """
    data, _ = compute_prices()
    iso_now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    valid_until = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    price_schema = {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "name": "Aajako Sunko Chandi Ko Bhav Nepal – Live Gold Silver Price",
        "description": "Gold and silver prices in Nepal in NPR per tola",
        "url": SITE_URL,
        "dateModified": iso_now,
        "itemListElement": [
            {"@type": "ListItem", "position": 1,
             "name": "सुनको भाउ प्रति तोला (Gold Price Per Tola Nepal)",
             "item": {"@type": "Product", "name": "Gold (XAU) – Nepal Price Per Tola",
                      "description": "24K gold price per tola in NPR – aajako sunko bhav Nepal",
                      "offers": {"@type": "Offer", "priceCurrency": "NPR",
                                 "price": str(data['gold_tola_npr']),
                                 "priceValidUntil": valid_until, "url": SITE_URL}}},
            {"@type": "ListItem", "position": 2,
             "name": "चाँदीको भाउ प्रति तोला (Silver Price Per Tola Nepal)",
             "item": {"@type": "Product", "name": "Silver (XAG) – Nepal Price Per Tola",
                      "description": "999 fine silver price per tola in NPR – chandi ko bhav Nepal",
                      "offers": {"@type": "Offer", "priceCurrency": "NPR",
                                 "price": str(data['silver_tola_npr']),
                                 "priceValidUntil": valid_until, "url": SITE_URL}}},
        ],
    }
    context = {
        'p': data,
        'iso_now': iso_now,
        'update_time': datetime.now(NPT).strftime('%I:%M %p NPT'),
        # Escape <, >, & so the JSON can't break out of the <script> tag
        'price_schema_json': mark_safe(
            json.dumps(price_schema, ensure_ascii=False)
            .replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')),
        'fmt': {
            'gold_usd': '$' + _fmt(data['gold_usd'], 2),
            'gold_npr': 'NPR ' + _fmt(data['gold_npr'], 2),
            'gold_tola': 'NPR ' + _fmt(data['gold_tola_npr']),
            'gold_kg': 'NPR ' + _fmt(data['gold_kg_npr']),
            'silver_usd': '$' + _fmt(data['silver_usd'], 3),
            'silver_npr': 'NPR ' + _fmt(data['silver_npr'], 2),
            'silver_tola': 'NPR ' + _fmt(data['silver_tola_npr'], 2),
            'silver_kg': 'NPR ' + _fmt(data['silver_kg_npr']),
            'exchange': f"{data['exchange_rate']:.2f}",
            'gold_tola_in': _fmt_indian(data['gold_tola_npr']),
            'silver_tola_in': _fmt_indian(data['silver_tola_npr']),
        },
    }
    response = render(request, 'index.html', context)
    # Short CDN cache: fresh prices for bots, without an upstream call per hit
    response['Cache-Control'] = 'public, max-age=0, s-maxage=30, stale-while-revalidate=30'
    return response


@require_GET
def health_check(request):
    """Test if APIs are working"""
    try:
        # Test Gold API
        gold_response = requests.get(
            'https://api.gold-api.com/price/XAU',
            headers={'x-api-key': GOLD_API_KEY},
            timeout=5
        )
        gold_status = 'working' if gold_response.status_code == 200 else f'failed: {gold_response.status_code}'
        
        # Test Exchange API
        exchange_response = requests.get(EXCHANGE_RATE_URL, timeout=3)
        exchange_status = 'working' if exchange_response.status_code == 200 else f'failed: {exchange_response.status_code}'
        
    except Exception as e:
        gold_status = f'connection failed: {str(e)}'
        exchange_status = f'connection failed: {str(e)}'
    
    return JsonResponse({
        'status': 'healthy',
        'service': 'Gold & Silver Price Tracker',
        'timestamp': int(time.time()),
        'apis': {
            'gold_api': gold_status,
            'exchange_api': exchange_status
        },
        'exchange_api_url': EXCHANGE_RATE_URL,
        'gold_api_url': 'https://api.gold-api.com/price/XAU',
        'premiums_applied': True,
        'gold_premium': f'{GOLD_PREMIUM_PCT}%',
        'silver_premium': f'{SILVER_PREMIUM_PCT}%'
    })


# AI / LLM crawlers explicitly welcomed (search + answer engines)
AI_CRAWLERS = [
    'GPTBot', 'OAI-SearchBot', 'ChatGPT-User',
    'ClaudeBot', 'Claude-User', 'Claude-SearchBot', 'anthropic-ai',
    'PerplexityBot', 'Perplexity-User',
    'Google-Extended', 'Applebot', 'Applebot-Extended',
    'Bingbot', 'DuckAssistBot', 'meta-externalagent', 'Amazonbot',
    'cohere-ai', 'MistralAI-User', 'YouBot', 'CCBot',
]


@require_GET
def robots_txt(request):
    """Serve robots.txt: open to search engines and AI crawlers"""
    lines = ["# SunchadiPrice – Live Gold Silver Price Nepal", "",
             "User-agent: *", "Allow: /", "Disallow: /api/health/", ""]
    for bot in AI_CRAWLERS:
        lines += [f"User-agent: {bot}", "Allow: /", "Disallow: /api/health/", ""]
    lines += [f"Sitemap: {SITE_URL}/sitemap.xml", "",
              f"# LLM-friendly summary with live prices: {SITE_URL}/llms.txt"]
    response = HttpResponse("\n".join(lines) + "\n", content_type="text/plain; charset=utf-8")
    response['Cache-Control'] = 'public, max-age=86400'
    return response


@require_GET
def sitemap_xml(request):
    """Serve dynamic sitemap.xml"""
    lastmod = datetime.now(timezone.utc).date().isoformat()
    urls = [('/', 'always', '1.0'), ('/llms.txt', 'hourly', '0.5')]
    entries = "".join(
        f"  <url>\n    <loc>{SITE_URL}{path}</loc>\n    <lastmod>{lastmod}</lastmod>\n"
        f"    <changefreq>{freq}</changefreq>\n    <priority>{prio}</priority>\n  </url>\n"
        for path, freq, prio in urls
    )
    content = ('<?xml version="1.0" encoding="UTF-8"?>\n'
               '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
               f'{entries}</urlset>\n')
    return HttpResponse(content, content_type="application/xml")


@require_GET
def llms_txt(request):
    """llms.txt (https://llmstxt.org): plain-markdown summary for LLMs, with live prices"""
    data, _ = compute_prices()
    now = datetime.now(NPT).strftime('%Y-%m-%d %H:%M NPT')
    content = f"""# SunchadiPrice

> Live gold and silver prices in Nepal (aajako sunko bhav / सुनको भाउ) per tola in NPR,
> derived from the international spot price (XAU/USD, XAG/USD), the USD/NPR exchange rate
> and a premium calibrated to the official FENEGOSIDA daily rate.

## Current prices (as of {now})

- Gold (24K fine) per tola: NPR {_fmt_indian(data['gold_tola_npr'])}
- Silver per tola: NPR {_fmt_indian(data['silver_tola_npr'])}
- Gold per kg: NPR {_fmt(data['gold_kg_npr'])}
- Silver per kg: NPR {_fmt(data['silver_kg_npr'])}
- Gold spot (XAU/USD): ${_fmt(data['gold_usd'], 2)} per troy ounce
- Silver spot (XAG/USD): ${_fmt(data['silver_usd'], 3)} per troy ounce
- USD/NPR: {data['exchange_rate']:.2f}

These are estimates tracking the market in real time. The official rate is set once daily
by FENEGOSIDA (Federation of Nepal Gold and Silver Dealers' Association) around 10:45 AM NPT;
always confirm with FENEGOSIDA or a dealer before buying or selling.

## How the price is calculated

Price per tola (NPR) = spot USD per troy ounce × USD/NPR × (11.6638 ÷ 31.1035) × Nepal premium

- 1 tola = 11.6638 g, 1 troy ounce = 31.1035 g
- Current Nepal premium: gold +{GOLD_PREMIUM_PCT}%, silver +{SILVER_PREMIUM_PCT}%
  (covers customs duty, taxes and dealer margin; recalibrated against FENEGOSIDA rates)

## Links

- [Live price page]({SITE_URL}/): real-time gold and silver price tracker (Nepali/English)
- [JSON price API]({SITE_URL}/api/prices/): current prices as JSON (gold_tola_npr, silver_tola_npr, …)
"""
    response = HttpResponse(content, content_type="text/markdown; charset=utf-8")
    response['Cache-Control'] = 'public, max-age=0, s-maxage=60'
    return response


@require_GET
def static_image(request, name):
    """Serve og-image.png / logo.png from templates/"""
    path = BASE_DIR / 'templates' / name
    if not path.exists():
        raise Http404()
    response = FileResponse(path.open('rb'), content_type='image/png')
    response['Cache-Control'] = 'public, max-age=604800'
    return response

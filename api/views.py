import concurrent.futures
import mimetypes
import os
from pathlib import Path
import time

import requests
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.views.decorators.cache import cache_page
from django.views.decorators.http import require_GET

BASE_DIR = Path(__file__).resolve().parent.parent

# API Config
GOLD_API_KEY = os.environ.get('GOLD_API_KEY', 'f2db1aaaee18667e498f2a463de9795215b478a6a91abad885d14e6d5edc5b49')
# Using the exchange rate API you requested
EXCHANGE_RATE_URL = 'https://open.er-api.com/v6/latest/USD'

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

@require_GET
@cache_page(30)
def get_prices(request):
    """Fetch all prices with NPR conversion"""
    print(f"\n📊 Fetching prices at {time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            gold_future = executor.submit(fetch_gold_price)
            silver_future = executor.submit(fetch_silver_price)
            exchange_future = executor.submit(fetch_exchange_rate)
            
            gold_usd = gold_future.result()
            silver_usd = silver_future.result()
            usd_npr = exchange_future.result()
        
        print(f"✅ FINAL: Gold: ${gold_usd:.2f}, Silver: ${silver_usd:.3f}, USD/NPR: {usd_npr:.2f}")
        
        # Conversion constants
        GRAMS_PER_TROY_OUNCE = 31.1035
        GRAMS_PER_TOLA = 11.6638
        GRAMS_PER_KG = 1000
        
        # Calculate conversion factors
        tola_to_ounce = GRAMS_PER_TOLA / GRAMS_PER_TROY_OUNCE
        kg_to_ounce = GRAMS_PER_KG / GRAMS_PER_TROY_OUNCE
        
        # Calculate BASE NPR prices (without premium)
        gold_npr_base = gold_usd * usd_npr
        silver_npr_base = silver_usd * usd_npr
        
        # Calculate BASE tola prices (without premium)
        gold_tola_npr_base = gold_usd * tola_to_ounce * usd_npr
        silver_tola_npr_base = silver_usd * tola_to_ounce * usd_npr
        
        # Calculate BASE kg prices (without premium)
        gold_kg_npr_base = gold_usd * kg_to_ounce * usd_npr 
        silver_kg_npr_base = silver_usd * kg_to_ounce * usd_npr
        
        # APPLY NEPAL PREMIUMS to NPR prices
        gold_npr = gold_npr_base * NEPAL_PREMIUMS['gold'] 
        silver_npr = silver_npr_base * NEPAL_PREMIUMS['silver']
        
        # APPLY NEPAL PREMIUMS to tola prices
        gold_tola_npr = gold_tola_npr_base * NEPAL_PREMIUMS['gold']
        silver_tola_npr = silver_tola_npr_base * NEPAL_PREMIUMS['silver']
        
        # APPLY NEPAL PREMIUMS to kg prices
        gold_kg_npr = gold_kg_npr_base * NEPAL_PREMIUMS['gold']
        silver_kg_npr = silver_kg_npr_base * NEPAL_PREMIUMS['silver']
        
        print(f"✅ WITH PREMIUMS: Gold NPR: {gold_npr:,.2f} (+{GOLD_PREMIUM_PCT}%), Silver NPR: {silver_npr:,.2f} (+{SILVER_PREMIUM_PCT}%)")
        
        # Create response
        response_data = {
            'status': 'success',
            'data': {
                'gold_usd': round(gold_usd, 2),
                'silver_usd': round(silver_usd, 3),
                'exchange_rate': round(usd_npr, 2),
                'gold_npr': round((gold_npr / 100)) * 100,  # Now with premium
                'silver_npr': round((silver_npr / 100)) * 100,  # Now with premium
                # Silver tola with 2 decimals, gold tola as integer
                'gold_tola_npr': round((gold_tola_npr / 100)) * 100,  # Now with premium
                'silver_tola_npr': round((silver_tola_npr / 10)) * 10,  # Now with premium
                'gold_kg_npr': round((gold_kg_npr / 100)) * 100,  # Now with premium
                'silver_kg_npr': round((silver_kg_npr / 100)) * 100,  # Now with premium
                'timestamp': int(time.time()),
                'timestamp_human': time.strftime('%Y-%m-%d %H:%M:%S'),
                'source': 'Gold API + open.er-api.com',
                'api_key': 'active',
                'unit': 'USD per troy ounce',
                'conversion_note': '1 tola = 11.6638g, 1 troy ounce = 31.1035g',
                'premium_applied': True,
                'gold_premium_pct': GOLD_PREMIUM_PCT,
                'silver_premium_pct': SILVER_PREMIUM_PCT
            }
        }
        
        print(f"📤 Sending response: {response_data['data']}")
        json_response = JsonResponse(response_data)
        json_response['Cache-Control'] = 'public, max-age=30, stale-while-revalidate=5'
        json_response['X-Robots-Tag'] = 'noindex'
        return json_response
        
    except Exception as e:
        print(f"❌ Error in get_prices: {e}")
        
        # Fallback with same API structure
        gold_usd = FALLBACK_GOLD_USD
        silver_usd = FALLBACK_SILVER_USD
        usd_npr = FALLBACK_USD_NPR
        
        # Reuse the same calculation for consistency
        GRAMS_PER_TROY_OUNCE = 31.1035
        GRAMS_PER_TOLA = 11.6638
        GRAMS_PER_KG = 1000
        
        tola_to_ounce = GRAMS_PER_TOLA / GRAMS_PER_TROY_OUNCE
        kg_to_ounce = GRAMS_PER_KG / GRAMS_PER_TROY_OUNCE
        
        # Calculate base prices
        gold_npr_base = gold_usd * usd_npr
        silver_npr_base = silver_usd * usd_npr
        gold_tola_npr_base = gold_usd * tola_to_ounce * usd_npr
        silver_tola_npr_base = silver_usd * tola_to_ounce * usd_npr
        gold_kg_npr_base = gold_usd * kg_to_ounce * usd_npr
        silver_kg_npr_base = silver_usd * kg_to_ounce * usd_npr
        
        # Apply premiums to fallback too
        gold_npr = gold_npr_base * NEPAL_PREMIUMS['gold']
        silver_npr = silver_npr_base * NEPAL_PREMIUMS['silver']
        gold_tola_npr = gold_tola_npr_base * NEPAL_PREMIUMS['gold']
        silver_tola_npr = silver_tola_npr_base * NEPAL_PREMIUMS['silver']
        gold_kg_npr = gold_kg_npr_base * NEPAL_PREMIUMS['gold']
        silver_kg_npr = silver_kg_npr_base * NEPAL_PREMIUMS['silver']
        
        return JsonResponse({
            'status': 'error',
            'message': str(e),
            'data': {
                'gold_usd': gold_usd,
                'silver_usd': silver_usd,
                'exchange_rate': usd_npr,
                'gold_npr': round(gold_npr, 2),
                'silver_npr': round(silver_npr, 2),
                'gold_tola_npr': round(gold_tola_npr),
                'silver_tola_npr': round(silver_tola_npr, 2),
                'gold_kg_npr': round(gold_kg_npr),
                'silver_kg_npr': round(silver_kg_npr),
                'timestamp': int(time.time()),
                'source': 'Fallback due to error',
                'premium_applied': True,
                'gold_premium_pct': GOLD_PREMIUM_PCT,
                'silver_premium_pct': SILVER_PREMIUM_PCT
            }
        })

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


def robots_txt(request):
    """Serve robots.txt for SEO crawl control"""
    content = "\n".join([
        "User-agent: *",
        "Allow: /",
        "Disallow: /api/health/",
        "",
        "Sitemap: https://sunchadiprice.com/sitemap.xml",
        "",
        "# SunchadiPrice – Live Gold Silver Price Nepal",
    ])
    return HttpResponse(content, content_type="text/plain")


def sitemap_xml(request):
    """Serve dynamic sitemap.xml"""
    lastmod = time.strftime('%Y-%m-%d')
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"\n'
        '        xmlns:xhtml="http://www.w3.org/1999/xhtml">\n'
        '  <url>\n'
        '    <loc>https://sunchadiprice.com/</loc>\n'
        f'    <lastmod>{lastmod}</lastmod>\n'
        '    <changefreq>always</changefreq>\n'
        '    <priority>1.0</priority>\n'
        '    <xhtml:link rel="alternate" hreflang="ne" href="https://sunchadiprice.com/"/>\n'
        '    <xhtml:link rel="alternate" hreflang="en" href="https://sunchadiprice.com/en/"/>\n'
        '    <xhtml:link rel="alternate" hreflang="x-default" href="https://sunchadiprice.com/"/>\n'
        '  </url>\n'
        '</urlset>'
    )
    return HttpResponse(content, content_type="application/xml")

#!/usr/bin/env python3
"""
French Stock Ranker — ranks Euronext Paris (.PA) stocks by market cap.
Usage:  python main.py [--port 8080]
Then open: http://localhost:8080
"""

import argparse
import os
import re
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Lock, Thread

import requests
import yfinance as yf

DEFAULT_PORT = int(os.environ.get("PORT", 8080))
EURONEXT_API = "https://live.euronext.com/en/pd/data/stocks"

_lock = Lock()
_results: list[dict] = []
_done = 0
_total = 0
_complete = False
_fetch_started = False


def fetch_euronext_stocks() -> list[dict]:
    """Return all French-listed stocks (ISIN starts with FR) from Euronext Paris."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json",
    })
    seen: set[str] = set()
    stocks: list[dict] = []
    start = 0
    page_size = 20
    total = None

    while total is None or start < total:
        resp = session.post(EURONEXT_API, data={
            "mics": "XPAR",
            "iDisplayStart": start,
            "iDisplayLength": page_size,
            "sEcho": start // page_size + 1,
        }, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if total is None:
            total = data["iTotalRecords"]
            print(f"Euronext Paris: {total} instruments found", flush=True)
        for row in data["aaData"]:
            symbol = (row[2] or "").strip()
            isin = (row[1] or "").strip()
            if not symbol or symbol in seen:
                continue
            if not isin.startswith("FR"):  # French companies only
                continue
            seen.add(symbol)
            # Extract name from HTML anchor in row[0]
            m = re.search(r">([^<]+)</a>", row[0] or "")
            name = m.group(1).strip() if m else symbol
            stocks.append({"symbol": symbol + ".PA", "name": name, "isin": isin})
        start += page_size

    print(f"French companies (FR ISIN): {len(stocks)}", flush=True)
    return stocks


def fetch_one(stock: dict) -> dict:
    """Get market cap + price. Uses fast_info first, falls back to info."""
    symbol = stock["symbol"]
    try:
        fi = yf.Ticker(symbol).fast_info
        mc = fi.market_cap or None
        price = fi.last_price or None
        currency = getattr(fi, "currency", "EUR") or "EUR"
        if mc:
            return {**stock, "market_cap": mc, "price": price,
                    "currency": currency, "sector": "—", "pe": None}
    except Exception:
        pass
    # fallback: try .info for market cap
    try:
        info = yf.Ticker(symbol).info
        mc = info.get("marketCap")
        return {
            **stock,
            "name": info.get("longName") or info.get("shortName") or stock["name"],
            "market_cap": mc,
            "price": info.get("currentPrice") or info.get("regularMarketPrice"),
            "currency": info.get("currency", "EUR"),
            "sector": info.get("sector") or "—",
            "pe": info.get("trailingPE"),
        }
    except Exception:
        return {**stock, "market_cap": None, "price": None,
                "currency": "EUR", "sector": "—", "pe": None}


def enrich_with_details(stocks: list[dict]) -> None:
    """Fetch sector/P/E via .info for all stocks that have a market cap."""
    targets = [s for s in stocks if s.get("market_cap") and s.get("sector") == "—"]
    print(f"Enriching {len(targets)} stocks with sector/P/E...", flush=True)

    def enrich(s):
        try:
            info = yf.Ticker(s["symbol"]).info
            s["name"] = info.get("longName") or info.get("shortName") or s["name"]
            s["sector"] = info.get("sector") or "—"
            s["pe"] = info.get("trailingPE")
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(enrich, s) for s in targets]
        done = 0
        for _ in as_completed(futures):
            done += 1
            print(f"\r  {done}/{len(targets)} enriched", end="", flush=True)
    print()


def fetch_all() -> None:
    global _done, _total, _complete, _results

    stocks = fetch_euronext_stocks()
    with _lock:
        _total = len(stocks)
    print(f"Fetching market cap for {len(stocks)} French stocks...", flush=True)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_one, s): s for s in stocks}
        for future in as_completed(futures):
            r = future.result()
            with _lock:
                _done += 1
                _results.append(r)
            print(f"\r  {_done}/{len(stocks)} fetched", end="", flush=True)
    print()

    with _lock:
        _results.sort(key=lambda x: x.get("market_cap") or 0, reverse=True)

    enrich_with_details(_results)

    with _lock:
        _complete = True
    print(f"Done. {len(_results)} French stocks ranked.", flush=True)


def fmt_cap(v: float) -> str:
    if v >= 1e12:
        return f"€{v/1e12:.2f}T"
    if v >= 1e9:
        return f"€{v/1e9:.2f}B"
    if v >= 1e6:
        return f"€{v/1e6:.2f}M"
    return f"€{v:,.0f}"


def fmt_price(p, currency) -> str:
    if p is None:
        return "—"
    sym = "€" if currency == "EUR" else currency + " "
    return f"{sym}{p:,.2f}"


def fmt_pe(pe) -> str:
    if pe is None:
        return "—"
    try:
        return f"{float(pe):.1f}x"
    except (ValueError, TypeError):
        return "—"


def build_html(stocks: list[dict], done: int, total: int, complete: bool) -> str:
    rows = ""
    sorted_stocks = sorted(stocks, key=lambda x: x.get("market_cap") or 0, reverse=True)
    for i, s in enumerate(sorted_stocks, 1):
        rows += f"""
        <tr>
          <td class="rank">{i}</td>
          <td class="name">{s['name']}<span class="ticker">{s['symbol']}</span></td>
          <td class="cap">{fmt_cap(s['market_cap']) if s.get('market_cap') else '—'}</td>
          <td>{fmt_price(s['price'], s['currency'])}</td>
          <td>{s['sector']}</td>
          <td>{fmt_pe(s['pe'])}</td>
        </tr>"""

    updated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not complete:
        pct = int(done / total * 100) if total else 0
        banner = f"""
  <div class="banner">
    Fetching… {done}/{total} tickers checked ({pct}%) — {len(stocks)} stocks found so far.
    Page auto-updates every 5 s.
  </div>"""
        refresh = '<meta http-equiv="refresh" content="5">'
    else:
        banner = ""
        refresh = ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  {refresh}
  <title>French Stocks — Market Cap Ranking</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
           background: #0f1117; color: #e2e8f0; min-height: 100vh; padding: 2rem; }}
    h1 {{ font-size: 1.6rem; font-weight: 700; color: #fff; margin-bottom: .25rem; }}
    .sub {{ color: #64748b; font-size: .85rem; margin-bottom: 1rem; }}
    .sub a {{ color: #3b82f6; text-decoration: none; }}
    .banner {{ background: #1e3a5f; border: 1px solid #2563eb; border-radius: .4rem;
               padding: .6rem 1rem; margin-bottom: 1.2rem; font-size: .85rem; color: #93c5fd; }}
    table {{ width: 100%; border-collapse: collapse; font-size: .9rem; }}
    th {{ text-align: left; padding: .6rem 1rem; color: #94a3b8;
          font-weight: 600; font-size: .75rem; text-transform: uppercase;
          letter-spacing: .05em; border-bottom: 1px solid #1e293b; }}
    td {{ padding: .65rem 1rem; border-bottom: 1px solid #1e293b; vertical-align: middle; }}
    tr:hover td {{ background: #1e293b; }}
    .rank {{ color: #475569; font-size: .8rem; width: 3rem; }}
    .name {{ font-weight: 500; color: #f1f5f9; }}
    .ticker {{ display: block; font-size: .75rem; color: #64748b; margin-top: .1rem; }}
    .cap {{ font-weight: 700; color: #34d399; }}
    .footer {{ margin-top: 1.5rem; font-size: .8rem; color: #475569; }}
    .footer a {{ color: #3b82f6; text-decoration: none; }}
  </style>
</head>
<body>
  <h1>🇫🇷 French Stocks — Market Cap Ranking</h1>
  <p class="sub">
    {len(sorted_stocks)} stocks from Euronext Paris &mdash; {updated}
    &nbsp;·&nbsp; <a href="/refresh">Refresh data</a>
  </p>
  {banner}
  <table>
    <thead>
      <tr>
        <th>#</th><th>Company</th><th>Market Cap</th>
        <th>Price</th><th>Sector</th><th>P/E</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>
  <p class="footer">Data via <a href="https://pypi.org/project/yfinance/">yfinance</a>. Prices may be delayed.</p>
</body>
</html>"""


def build_loading_html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="refresh" content="3">
  <title>Loading…</title>
  <style>
    body { font-family: -apple-system, sans-serif; background: #0f1117; color: #e2e8f0;
           display: flex; align-items: center; justify-content: center; min-height: 100vh; }
    .box { text-align: center; }
    h2 { font-size: 1.4rem; margin-bottom: .5rem; }
    p { color: #64748b; font-size: .9rem; }
  </style>
</head>
<body>
  <div class="box">
    <h2>🇫🇷 Fetching tickers from Euronext…</h2>
    <p>Page will refresh automatically.</p>
  </div>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        global _fetch_started, _results, _done, _total, _complete

        if self.path == "/refresh":
            with _lock:
                _results.clear()
                _done = 0
                _total = 0
                _complete = False
            global _fetch_started
            _fetch_started = False
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return

        if not _fetch_started:
            _fetch_started = True
            Thread(target=fetch_all, daemon=True).start()

        with _lock:
            snapshot = list(_results)
            done, total, complete = _done, _total, _complete

        if not snapshot and not complete:
            html = build_loading_html().encode()
        else:
            html = build_html(snapshot, done, total, complete).encode()

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)


def main():
    parser = argparse.ArgumentParser(description="Rank French stocks by market cap")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    host = "0.0.0.0" if os.environ.get("PORT") else "localhost"
    server = HTTPServer((host, args.port), Handler)
    url = f"http://{host}:{args.port}"
    print(f"Server running at {url}")
    print("Press Ctrl+C to stop.\n")

    if host == "localhost":
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()

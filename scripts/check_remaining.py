#!/usr/bin/env python3
import requests, json

BASE = "https://archive.org/advancedsearch.php"
HEADERS = {"User-Agent": "GD-RAT-Scraper/2.0-Incremental"}

for year in range(1993, 1996):
    q = f"collection:GratefulDead AND creator:Grateful Dead AND date:[{year}-01-01 TO {year}-12-31]"
    params = {"q": q, "fl": "identifier", "rows": 0, "output": "json"}
    try:
        r = requests.get(BASE, params=params, headers=HEADERS, timeout=30)
        d = r.json()
        total = d["response"]["numFound"]
        print(f"Year {year}: {total} total shows in IA")
    except Exception as e:
        print(f"Year {year}: ERROR {e}")

# Also check how many are already in processed_ids
try:
    with open("/home/mao/DaveMatt/gd-project/data/scraper_state.json") as f:
        state = json.load(f)
    processed = set(state["processed_ids"])
    print(f"\nAlready processed: {len(processed)} shows")
    # Count how many processed are in 1993-1995 range
    for year in range(1993, 1996):
        count = sum(1 for i in processed if f"gd{year}" in i or f"gd{year-1900}" in i)
        print(f"  Already processed in {year}: {count}")
    print(f"Current year in state: {state['current_year']}")
    print(f"Shows with comments: {state['shows_with_comments']}")
    print(f"Total comments: {state['total_comments']}")
except Exception as e:
    print(f"State load error: {e}")

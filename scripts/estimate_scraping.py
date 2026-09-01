#!/usr/bin/env python3
"""
Estimate disk space and time for full GD scrape.
"""

import json
import os

DATA_FILE = "/home/mao/DaveMatt/gd-project/data/gd_comments_combined.json"

# Total GD shows on IA (from previous research)
TOTAL_GD_SHOWS = 18327

def main():
    # Load current data
    try:
        with open(DATA_FILE, 'r') as f:
            data = json.load(f)
    except FileNotFoundError:
        print("Data file not found - using defaults")
        data = {"comments": [], "setlists": {}, "shows_processed": []}

    shows_processed = len(data.get("shows_processed", []))
    comments_count = len(data.get("comments", []))
    setlists_count = len(data.get("setlists", {}))

    # Current file sizes
    data_size = os.path.getsize(DATA_FILE) / (1024 * 1024)  # MB
    
    print("=== GD Scraping Progress Estimate ===\n")
    print(f"Total GD shows on IA: {TOTAL_GD_SHOWS}")
    print(f"Shows processed so far: {shows_processed}")
    print(f"Completion: {shows_processed/TOTAL_GD_SHOWS*100:.2f}%\n")
    
    print("=== Current Data Stats ===")
    print(f"Comments collected: {comments_count}")
    print(f"Setlists captured: {setlists_count}")
    print(f"Current data file size: {data_size:.2f} MB\n")
    
    # Estimate
    estimated_comments_per_show = comments_count / shows_processed if shows_processed > 0 else 5
    estimated_total_comments = TOTAL_GD_SHOWS * estimated_comments_per_show
    
    # Average comment size: ~100 bytes for JSON metadata
    estimated_comment_size = estimated_total_comments * 100 / (1024 * 1024)  # MB
    estimated_setlist_size = TOTAL_GD_SHOWS * 500 / (1024 * 1024)  # MB
    estimated_total_size = estimated_comment_size + estimated_setlist_size
    
    print("=== Estimated Final Collection ===")
    print(f"Estimated total comments: {int(estimated_total_comments)}")
    print(f"Estimated disk usage: {estimated_total_size:.2f} MB")
    
    # Time estimate (3.25s per show, 100 shows per batch with saves)
    delay = 3.25
    shows_per_hour = 3600 / delay
    remaining_shows = TOTAL_GD_SHOWS - shows_processed
    hours_remaining = remaining_shows / shows_per_hour
    
    print("\n=== Time Estimates ===")
    print(f"Delay: 3.25s per show")
    print(f"Rate: {shows_per_hour:.1f} shows/hour")
    print(f"Remaining shows: {remaining_shows}")
    print(f"Estimated hours remaining: {hours_remaining:.1f}")
    print(f"Estimated days remaining (continuous): {hours_remaining/24:.1f}")
    print(f"Estimated years remaining (1970s-1990s): 17")

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Reddit scraper for r/gratefuldead
Captures show discussions, reviews, setlist commentary, and historical content
Uses Reddit's public JSON API (no authentication required)
"""

import requests
import json
import time
import os
import re
from datetime import datetime

# Output paths
OUTPUT_FILE = '/home/mao/DaveMatt/gd-project/data/gd_reddit_comments.json'
LOG_FILE = '/home/mao/DaveMatt/gd-project/logs/reddit_scraper.log'

# Scraping parameters
SUBREDDIT = 'gratefuldead'
POST_LIMIT = 50  # Reduced for test run
REQUEST_DELAY = 3.25  # Matching archive.org etiquette
TIME_FILTER = 'month'  # Recent posts

def log(message):
    """Log message with timestamp"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_entry = f"[{timestamp}] {message}"
    print(log_entry)
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, 'a') as f:
        f.write(log_entry + '\n')

def get_reddit_json(url):
    """Fetch JSON data from Reddit's public API"""
    headers = {
        'User-Agent': 'GD-RAT/1.0 (by /u/HermesAgent) - Grateful Dead RAG scraper'
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        log(f"Request failed for {url}: {e}")
        return None

def extract_comments(data, max_comments=50):
    """Recursively extract comments from Reddit JSON structure"""
    comments = []
    
    def parse_comment(comment_data, depth=0):
        if depth > 3:  # Limit nesting depth
            return
        if isinstance(comment_data, dict):
            if 'kind' in comment_data and comment_data['kind'] == 't1':
                comment = comment_data['data']
                comments.append({
                    'id': comment.get('id'),
                    'body': comment.get('body', '')[:2000],
                    'author': comment.get('author', '[deleted]'),
                    'score': comment.get('score', 0),
                    'created_utc': comment.get('created_utc', 0),
                    'depth': depth
                })
                # Parse replies
                replies = comment.get('replies', '')
                if isinstance(replies, dict) and 'data' in replies:
                    for child in replies['data'].get('children', []):
                        parse_comment(child, depth + 1)
    
    for item in data:
        parse_comment(item)
    
    return comments[:max_comments]

def extract_show_date(title):
    """Try to extract GD show date from post title"""
    patterns = [
        r'gd\d{4}-\d{2}-\d{2}',     # gd1977-05-08
        r'\b\d{4}-\d{2}-\d{2}\b',   # 1977-05-08
        r'\d{1,2}/\d{1,2}/\d{4}',   # 5/8/1977
    ]
    for pattern in patterns:
        match = re.search(pattern, title, re.IGNORECASE)
        if match:
            return match.group()
    return None

def classify_post(title, selftext):
    """Classify the type of GD-related post"""
    title_lower = title.lower()
    text_lower = selftext.lower()
    
    categories = []
    if any(kw in title_lower or kw in text_lower for kw in ['setlist', 'set list', 'what did they play']):
        categories.append('setlist')
    if any(kw in title_lower or kw in text_lower for kw in ['review', 'thoughts', 'discussion']):
        categories.append('review')
    if re.search(r'\d{4}-\d{2}-\d{2}', title_lower) or re.search(r'gd\d{4}', title_lower):
        categories.append('show_thread')
    if any(kw in title_lower or kw in text_lower for kw in ['song', 'track', 'favorite']):
        categories.append('song')
    
    return categories if categories else ['general']

def scrape_reddit():
    """Main scraping function"""
    log("Starting r/gratefuldead Reddit scraper (public JSON API)...")
    
    # Load existing data if it exists
    existing_data = []
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, 'r') as f:
                existing_data = json.load(f)
            log(f"Loaded {len(existing_data)} existing entries")
        except:
            log("No existing data file, starting fresh")
    
    existing_ids = {entry['id'] for entry in existing_data}
    new_entries = []
    
    # Fetch top posts
    posts_url = f"https://www.reddit.com/r/{SUBREDDIT}/top/.json?limit={POST_LIMIT}&t={TIME_FILTER}"
    log(f"Fetching top {POST_LIMIT} posts from r/{SUBREDDIT}")
    
    data = get_reddit_json(posts_url)
    if not data:
        log("Failed to fetch posts, exiting")
        return False
    
    posts = data['data']['children']
    log(f"Retrieved {len(posts)} posts")
    
    for post in posts:
        try:
            post_data = post['data']
            post_id = post_data['id']
            
            if post_id in existing_ids:
                log(f"Skipping already processed post: {post_data['title'][:50]}...")
                continue
            
            title = post_data.get('title', '')
            log(f"Processing thread: {title[:60]}...")
            
            # Fetch comments for this post
            comments_url = f"https://www.reddit.com/r/{SUBREDDIT}/comments/{post_id}.json"
            comment_data = get_reddit_json(comments_url)
            comments = []
            if comment_data and len(comment_data) > 1:
                comments = extract_comments(comment_data[1]['data']['children'], max_comments=50)
            
            entry = {
                'id': post_id,
                'type': 'post',
                'title': title,
                'author': post_data.get('author', '[deleted]'),
                'score': post_data.get('score', 0),
                'created_utc': post_data.get('created_utc', 0),
                'selftext': post_data.get('selftext', '')[:5000],
                'url': post_data.get('url', ''),
                'permalink': f"https://reddit.com{post_data.get('permalink', '')}",
                'show_date': extract_show_date(title),
                'categories': classify_post(title, post_data.get('selftext', '')),
                'num_comments': post_data.get('num_comments', 0),
                'actual_comments_scraped': len(comments),
                'comments': comments
            }
            
            new_entries.append(entry)
            existing_ids.add(post_id)
            log(f"Added {len(comments)} comments for post {post_id}")
            
            # Rate limit
            time.sleep(REQUEST_DELAY)
            
        except Exception as e:
            log(f"Error processing post {post_id}: {e}")
            time.sleep(REQUEST_DELAY)
            continue
    
    # Save combined data
    all_data = existing_data + new_entries
    with open(OUTPUT_FILE, 'w') as f:
        json.dump(all_data, f, indent=2)
    
    log(f"\nScraping complete!")
    log(f"New entries: {len(new_entries)}")
    log(f"Total entries: {len(all_data)}")
    log(f"Data saved to: {OUTPUT_FILE}")
    
    return True

if __name__ == '__main__':
    scrape_reddit()

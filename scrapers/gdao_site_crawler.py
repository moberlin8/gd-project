#!/usr/bin/env python3
"""
GDAO Site Crawler - Maps the link structure of gdao.org

This script crawls the GDAO website starting from https://www.gdao.org,
follows internal links to a specified depth, and creates a network graph
of the site structure.
"""

import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import networkx as nx
import csv
import time
import logging
from collections import deque
import argparse
import os

# Configure logging
os.makedirs('gdao_tree', exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('gdao_tree/crawler.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class GDAOSiteCrawler:
    def __init__(self, start_url="https://www.gdao.org", max_depth=10, delay=1.0):
        self.start_url = start_url
        self.max_depth = max_depth
        self.delay = delay
        self.visited = set()
        self.graph = nx.DiGraph()
        self.base_domain = urlparse(start_url).netloc
        
        # Session for connection pooling
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (compatible; GDAO-SiteCrawler/1.0)'
        })
    
    def is_internal_link(self, url):
        """Check if a URL is internal to the GDAO domain"""
        parsed = urlparse(url)
        return parsed.netloc == self.base_domain or parsed.netloc == ''
    
    def normalize_url(self, url):
        """Normalize URL by removing fragments and query parameters"""
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    
    def get_links_from_page(self, url):
        """Extract all internal links from a page"""
        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            links = []
            
            # Find all links
            for link in soup.find_all('a', href=True):
                href = link['href']
                absolute_url = urljoin(url, href)
                
                if self.is_internal_link(absolute_url):
                    normalized = self.normalize_url(absolute_url)
                    if normalized not in self.visited:
                        links.append(normalized)
            
            return links
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching {url}: {e}")
            return []
    
    def crawl(self):
        """Crawl the site using breadth-first search"""
        logger.info(f"Starting crawl from {self.start_url} with max depth {self.max_depth}")
        
        # Queue: (url, depth, parent_url)
        queue = deque([(self.start_url, 0, None)])
        
        while queue:
            current_url, depth, parent_url = queue.popleft()
            
            if current_url in self.visited or depth > self.max_depth:
                continue
            
            logger.info(f"Crawling (depth {depth}): {current_url}")
            self.visited.add(current_url)
            
            # Add node to graph
            self.graph.add_node(current_url, depth=depth)
            
            # Add edge from parent if exists
            if parent_url:
                self.graph.add_edge(parent_url, current_url)
            
            # Get links from current page
            links = self.get_links_from_page(current_url)
            
            # Add links to queue for next depth level
            if depth < self.max_depth:
                for link in links:
                    if link not in self.visited:
                        queue.append((link, depth + 1, current_url))
            
            # Respect rate limiting
            time.sleep(self.delay)
        
        logger.info(f"Crawl complete. Visited {len(self.visited)} pages")
    
    def export_csv(self, filename):
        """Export the graph as CSV (edges list)"""
        csv_path = f"gdoa_tree/{filename}"
        with open(csv_path, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(['source', 'target', 'source_depth', 'target_depth'])
            
            for source, target in self.graph.edges():
                source_depth = self.graph.nodes[source].get('depth', 0)
                target_depth = self.graph.nodes[target].get('depth', 0)
                writer.writerow([source, target, source_depth, target_depth])
        
        logger.info(f"CSV exported to {csv_path}")
    
    def export_nodes_csv(self, filename):
        """Export nodes as CSV"""
        csv_path = f"gdoa_tree/{filename}"
        with open(csv_path, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(['url', 'depth', 'in_degree', 'out_degree'])
            
            for node in self.graph.nodes():
                depth = self.graph.nodes[node].get('depth', 0)
                in_degree = self.graph.in_degree(node)
                out_degree = self.graph.out_degree(node)
                writer.writerow([node, depth, in_degree, out_degree])
        
        logger.info(f"Nodes CSV exported to {csv_path}")
    
    def export_dot(self, filename):
        """Export the graph as DOT file for visualization"""
        dot_path = f"gdoa_tree/{filename}"
        
        # Create DOT content
        dot_content = ["digraph GDAO {"]
        dot_content.append("  rankdir=TB;")
        dot_content.append("  node [shape=box, style=filled];")
        
        # Add nodes with colors based on depth
        colors = ['lightblue', 'lightgreen', 'lightyellow', 'lightcoral', 'lightpink', 'lightgray', 'lightsalmon', 'lightseagreen', 'lightsteelblue', 'lightgoldenrodyellow']
        for node in self.graph.nodes():
            depth = self.graph.nodes[node].get('depth', 0)
            color = colors[min(depth, len(colors) - 1)]
            # Shorten URL for display
            label = node.replace('https://www.gdao.org', '').replace('/', '/\\n') or '/'
            dot_content.append(f'  "{node}" [label="{label}", fillcolor="{color}"];')
        
        # Add edges
        for source, target in self.graph.edges():
            dot_content.append(f'  "{source}" -> "{target}";')
        
        dot_content.append("}")
        
        with open(dot_path, 'w', encoding='utf-8') as dotfile:
            dotfile.write('\n'.join(dot_content))
        
        logger.info(f"DOT file exported to {dot_path}")
    
    def print_stats(self):
        """Print crawling statistics"""
        print(f"\n=== GDAO Site Crawl Statistics ===")
        print(f"Total pages crawled: {len(self.visited)}")
        print(f"Total links found: {self.graph.number_of_edges()}")
        print(f"Max depth reached: {max(self.graph.nodes[node].get('depth', 0) for node in self.graph.nodes())}")
        
        # Pages by depth
        depth_counts = {}
        for node in self.graph.nodes():
            depth = self.graph.nodes[node].get('depth', 0)
            depth_counts[depth] = depth_counts.get(depth, 0) + 1
        
        print("\nPages by depth:")
        for depth in sorted(depth_counts.keys()):
            print(f"  Depth {depth}: {depth_counts[depth]} pages")
        
        # Most linked pages
        in_degrees = [(node, self.graph.in_degree(node)) for node in self.graph.nodes()]
        in_degrees.sort(key=lambda x: x[1], reverse=True)
        
        print(f"\nMost linked-to pages:")
        for node, degree in in_degrees[:5]:
            print(f"  {degree} links -> {node}")

def main():
    parser = argparse.ArgumentParser(description='Crawl GDAO website and create link map')
    parser.add_argument('--depth', type=int, default=10, help='Maximum crawl depth (default: 10)')
    parser.add_argument('--delay', type=float, default=1.0, help='Delay between requests in seconds (default: 1.0)')
    parser.add_argument('--start-url', default='https://www.gdao.org', help='Starting URL (default: https://www.gdao.org)')
    
    args = parser.parse_args()
    
    # Create output directory
    os.makedirs('gdoa_tree', exist_ok=True)
    
    # Initialize and run crawler
    crawler = GDAOSiteCrawler(
        start_url=args.start_url,
        max_depth=args.depth,
        delay=args.delay
    )
    
    crawler.crawl()
    
    # Export results
    crawler.export_csv('gdao_links.csv')
    crawler.export_nodes_csv('gdao_nodes.csv')
    crawler.export_dot('gdao_graph.dot')
    
    # Print statistics
    crawler.print_stats()
    
    print(f"\n=== Output Files ===")
    print(f"Links CSV: gdoa_tree/gdao_links.csv")
    print(f"Nodes CSV: gdoa_tree/gdao_nodes.csv")
    print(f"DOT file: gdoa_tree/gdao_graph.dot")
    print(f"Log file: gdoa_tree/crawler.log")
    print(f"\nTo visualize the DOT file, use: dot -Tpng gdoa_tree/gdao_graph.dot -o gdoa_tree/gdao_graph.png")

if __name__ == "__main__":
    main()
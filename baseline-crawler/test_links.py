import sys
from pathlib import Path
from bs4 import BeautifulSoup

# Add the baseline-crawler directory to path so we can import crawler
sys.path.append(str(Path(__file__).resolve().parent))

from crawler.processor import LinkExtractor

content = Path("data/tmp/10111.html").read_text(encoding="utf-8")
base_url = "https://www.sitewall.net/sebi-cscrf-faq-2025-what-regulated-entities-need-to-know/"

print(f"File content length: {len(content)}")
print(f"Grep count was 59, let's see BeautifulSoup count...")

soup = BeautifulSoup(content, 'html.parser')
anchors = soup.find_all('a', href=True)
print(f"BeautifulSoup anchors found: {len(anchors)}")

urls, assets = LinkExtractor.extract_urls(content, base_url)
print(f"LinkExtractor.extract_urls returned: {len(urls)} internal-registered-domain urls")

print("\n--- Details of Internal Links ---")
for u in urls[:20]:
    print(f"  - {u}")

print("\n--- Why were others rejected by _is_allowed_url? ---")
import tldextract
base_domain = "www.sitewall.net"
base_ext = tldextract.extract(f"https://{base_domain}")
print(f"Base Registered Domain: {base_ext.registered_domain}")

count = 0
for a in anchors:
    href = a['href'].strip()
    from urllib.parse import urljoin
    u = urljoin(base_url, href)
    cand_ext = tldextract.extract(u)
    if cand_ext.registered_domain != base_ext.registered_domain:
        if count < 10:
            print(f"  Rejected (External): {u} ( {cand_ext.registered_domain} != {base_ext.registered_domain} )")
        count += 1
print(f"Total External/Rejected: {count}")

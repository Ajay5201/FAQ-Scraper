#!/usr/bin/env python3
"""
FAQ Scraper Web UI - Flask application with optimized crawling
"""

import json
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
from typing import Set, List, Dict, Optional, Tuple

import requests
from flask import Flask, render_template, request, jsonify
from bs4 import BeautifulSoup

app = Flask(__name__)


class FAQScraper:
    def __init__(self, website_url: str, max_pages: int = 100, timeout: int = 30000):
        self.base_url = website_url.rstrip("/")
        self.domain = urlparse(self.base_url).netloc
        self.max_pages = max_pages
        self.timeout = timeout
        self.visited_urls: Set[str] = set()
        self.all_faqs: List[Dict] = []
        self.seen_questions: Set[str] = set()
        self.faq_page_found = False

    def _is_valid_internal_url(self, url: str) -> bool:
        """Check if URL is internal and valid for crawling."""
        if not url:
            return False
        
        parsed = urlparse(url)
        
        if parsed.scheme and parsed.scheme not in ["http", "https"]:
            return False
        
        skip_patterns = [
            r"utm_", r"fbclid", r"gclid", r"#", r"javascript:",
            r"mailto:", r"tel:", r"\.pdf$", r"\.jpg$", r"\.png$",
            r"\.gif$", r"\.svg$", r"\.css$", r"\.js$", r"\.zip$",
            r"\.mp4$", r"\.mp3$", r"\.doc$", r"\.xls$"
        ]
        for pattern in skip_patterns:
            if re.search(pattern, url, re.IGNORECASE):
                return False
        
        if parsed.netloc and parsed.netloc != self.domain:
            return False
        
        # Limit to 1 level depth (e.g., /path is ok, /path/subpath is not)
        path = parsed.path.strip('/')
        if path:
            path_parts = path.split('/')
            if len(path_parts) > 1:
                return False
        
        return True

    def _normalize_url(self, url: str) -> str:
        """Normalize URL for deduplication."""
        if not url.startswith(("http://", "https://")):
            url = urljoin(self.base_url, url)
        
        parsed = urlparse(url)
        path = parsed.path.rstrip("/") or "/"
        return f"{parsed.scheme}://{parsed.netloc}{path}"

    def _extract_links_with_text(self, soup: BeautifulSoup) -> List[Tuple[str, str]]:
        """Extract all valid internal links with their text from page."""
        links = []
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            link_text = a_tag.get_text(strip=True).lower()
            full_url = self._normalize_url(href)
            if self._is_valid_internal_url(full_url) and full_url not in self.visited_urls:
                links.append((full_url, link_text))
        # Deduplicate by URL
        seen = set()
        unique_links = []
        for url, text in links:
            if url not in seen:
                seen.add(url)
                unique_links.append((url, text))
        return unique_links

    def _is_faq_link(self, url: str, link_text: str) -> bool:
        """Check if link is FAQ-related by URL path OR link text."""
        url_lower = url.lower()
        text_lower = link_text.lower()
        
        # Check URL patterns
        url_patterns = ['/faq', '/faqs', '/frequently-asked', '/help/faq', '/support/faq', 
                        'faq.', '/questions', '/q-and-a', '/qa']
        
        # Check link text patterns
        text_patterns = ['faq', 'frequently asked', 'questions', 'q&a', 'help center']
        
        return (any(pattern in url_lower for pattern in url_patterns) or
                any(pattern in text_lower for pattern in text_patterns))

    def _normalize_text(self, text: str) -> str:
        """Clean and normalize text."""
        if not text:
            return ""
        text = re.sub(r'\s+', ' ', text).strip()
        text = text.strip('•·-–—*#')
        noise_patterns = [
            r'Subscribe Newsletter.*$',
            r'Sign up to get.*$',
            r'© \d{4}.*$',
            r'All Rights Reserved.*$',
            r'Privacy Policy.*Terms.*$',
            r'To Top$'
        ]
        for pattern in noise_patterns:
            text = re.sub(pattern, '', text, flags=re.IGNORECASE)
        return text.strip()

    def _is_english(self, text: str) -> bool:
        """Check if text is primarily English."""
        if not text:
            return False
        # Count ASCII letters vs non-ASCII characters
        ascii_letters = sum(1 for c in text if c.isascii() and c.isalpha())
        non_ascii = sum(1 for c in text if not c.isascii())
        total_letters = ascii_letters + non_ascii
        if total_letters == 0:
            return True
        # If more than 30% non-ASCII, likely not English
        return (non_ascii / total_letters) < 0.3

    def _add_faq(self, question: str, answer: str, source_url: str):
        """Add FAQ if not duplicate and is a real FAQ."""
        q_normalized = question.lower().strip()
        
        # Skip non-English content
        if not self._is_english(question) or not self._is_english(answer):
            return
        
        # Remove leading numbers like "1)", "2.", "1What", "11What", "2 What" etc.
        # Also handle string prefixes like b"..." that may appear in scraped content
        question = re.sub(r'^[bB]?[\"\']?', '', question).strip()  # Remove string prefixes
        question = re.sub(r'^\d+[\)\.\:\s]*(?=[A-Za-z])', '', question).strip()
        q_normalized = question.lower().strip()
        
        
        skip_patterns = [
            'forgot your password', 'reset password', 'sign in', 'log in',
            'create account', 'register', 'subscribe', 'newsletter',
            'contact us', 'get in touch'
        ]
        if any(pattern in q_normalized for pattern in skip_patterns):
            return
        
        if q_normalized and q_normalized not in self.seen_questions and len(question) > 5:
            self.seen_questions.add(q_normalized)
            self.all_faqs.append({
                "question": question,
                "answer": answer,
                "sourceUrl": source_url
            })

    def _is_faq_heading(self, text: str) -> bool:
        """Check if text is a FAQ-related heading."""
        text_lower = text.lower().strip()
        faq_heading_patterns = [
            'faq', 'faqs', 'f.a.q', 'f.a.q.s',
            'frequently asked questions', 'frequently asked',
            'common questions', 'questions & answers', 'questions and answers',
            'q&a', 'q & a', 'have questions', 'got questions'
        ]
        return any(pattern in text_lower for pattern in faq_heading_patterns)

    def _find_faq_sections(self, soup: BeautifulSoup) -> List:
        """Find all FAQ sections in the page (elements under FAQ headings)."""
        faq_sections = []
        
        # Find headings that indicate FAQ sections
        for heading in soup.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6']):
            heading_text = heading.get_text(strip=True)
            if self._is_faq_heading(heading_text):
                # Collect all content until next same-level or higher heading
                section_content = []
                current = heading.find_next_sibling()
                heading_level = int(heading.name[1])  # h1 -> 1, h2 -> 2, etc.
                
                while current:
                    # Stop if we hit another heading of same or higher level
                    if current.name and current.name in ['h1', 'h2', 'h3', 'h4', 'h5', 'h6']:
                        current_level = int(current.name[1])
                        if current_level <= heading_level:
                            break
                    section_content.append(current)
                    current = current.find_next_sibling()
                
                if section_content:
                    faq_sections.append({
                        'heading': heading,
                        'content': section_content
                    })
        
        # Also find sections/divs with FAQ-related classes or IDs
        faq_containers = soup.find_all(
            ['section', 'div', 'article'],
            class_=lambda x: x and self._is_faq_heading(' '.join(x) if isinstance(x, list) else x)
        )
        for container in faq_containers:
            if container not in [s.get('heading') for s in faq_sections]:
                faq_sections.append({
                    'heading': None,
                    'content': [container]
                })
        
        # Check for ID-based FAQ sections
        faq_by_id = soup.find_all(
            ['section', 'div', 'article'],
            id=lambda x: x and self._is_faq_heading(x)
        )
        for container in faq_by_id:
            if container not in [s.get('heading') for s in faq_sections]:
                faq_sections.append({
                    'heading': None,
                    'content': [container]
                })
        
        return faq_sections

    def _extract_faqs_from_page(self, soup: BeautifulSoup, html: str, url: str):
        """Extract FAQs ONLY from sections with FAQ headings."""
        # Always check for schema FAQs (they are explicitly marked as FAQ)
        self._extract_schema_faqs(soup, url)
        
        # Find FAQ sections in the page
        faq_sections = self._find_faq_sections(soup)
        
        if not faq_sections:
            # No FAQ sections found, skip extraction
            return
        
        # Extract from each FAQ section
        for section in faq_sections:
            content_elements = section['content']
            
            # Handle case where content elements are direct paragraphs (not nested)
            # Collect all paragraphs from the section
            all_paragraphs = []
            for elem in content_elements:
                if hasattr(elem, 'name'):
                    if elem.name == 'p':
                        all_paragraphs.append(elem)
                    else:
                        # Also extract from nested elements
                        self._extract_from_faq_element(elem, url)
            
            # Process collected paragraphs as Q&A pairs
            if all_paragraphs:
                self._extract_from_paragraph_list(all_paragraphs, url)

    def _extract_from_paragraph_list(self, paragraphs: List, url: str):
        """Extract Q&A from a list of paragraph elements."""
        i = 0
        while i < len(paragraphs):
            p_text = paragraphs[i].get_text(strip=True)
            # Check if this paragraph looks like a question
            if p_text.endswith('?') or p_text.lower().startswith(('can ', 'do ', 'does ', 'is ', 'are ', 'how ', 'what ', 'why ', 'when ', 'where ', 'will ', 'should ')):
                question = self._normalize_text(p_text)
                # Next paragraph(s) are the answer
                answer_parts = []
                j = i + 1
                while j < len(paragraphs):
                    next_text = paragraphs[j].get_text(strip=True)
                    # Stop if we hit another question
                    if next_text.endswith('?') or next_text.lower().startswith(('can ', 'do ', 'does ', 'is ', 'are ', 'how ', 'what ', 'why ', 'when ', 'where ', 'will ', 'should ')):
                        break
                    answer_parts.append(next_text)
                    j += 1
                
                answer = self._normalize_text(' '.join(answer_parts))
                if question and answer and len(answer) > 20:
                    self._add_faq(question, answer, url)
                i = j  # Skip to the next question
            else:
                i += 1

    def _extract_schema_faqs(self, soup: BeautifulSoup, url: str):
        """Extract FAQs from JSON-LD schema markup (always valid as explicitly marked)."""
        for script in soup.find_all('script', type='application/ld+json'):
            try:
                data = json.loads(script.string)
                if isinstance(data, dict):
                    if data.get('@type') == 'FAQPage':
                        for item in data.get('mainEntity', []):
                            if item.get('@type') == 'Question':
                                q = self._normalize_text(item.get('name', ''))
                                a = self._normalize_text(item.get('acceptedAnswer', {}).get('text', ''))
                                if q and a:
                                    self._add_faq(q, a, url)
            except:
                pass

    def _extract_from_faq_element(self, elem, url: str):
        """Extract FAQs from an element that is within a FAQ section."""
        # Pattern 1: details/summary (accordion)
        for details in elem.find_all('details'):
            summary = details.find('summary')
            if summary:
                question = self._normalize_text(summary.get_text(strip=True))
                answer_parts = []
                for child in details.children:
                    if child != summary and hasattr(child, 'get_text'):
                        answer_parts.append(child.get_text(strip=True))
                answer = self._normalize_text(' '.join(answer_parts))
                if question and answer:
                    self._add_faq(question, answer, url)
        
        # Pattern 2: Class-based Q&A containers
        qa_containers = elem.find_all(
            ['div', 'li', 'article'],
            class_=lambda x: x and any(p in str(x).lower() for p in ['faq', 'question', 'qa-', 'accordion-item'])
        )
        for container in qa_containers:
            q_elem = container.find(
                class_=lambda x: x and any(p in str(x).lower() for p in ['question', 'title', 'header', 'trigger'])
            )
            a_elem = container.find(
                class_=lambda x: x and any(p in str(x).lower() for p in ['answer', 'content', 'body', 'panel'])
            )
            if q_elem and a_elem:
                question = self._normalize_text(q_elem.get_text(strip=True))
                answer = self._normalize_text(a_elem.get_text(strip=True))
                if question and answer:
                    self._add_faq(question, answer, url)
        
        # Pattern 3: Heading + paragraph/div patterns
        for heading in elem.find_all(['h3', 'h4', 'h5', 'h6']):
            heading_text = heading.get_text(strip=True)
            # Skip if this is another FAQ section heading
            if self._is_faq_heading(heading_text):
                continue
            
            next_elem = heading.find_next_sibling()
            answer_parts = []
            while next_elem and next_elem.name not in ['h1', 'h2', 'h3', 'h4', 'h5', 'h6']:
                if next_elem.name in ['p', 'div']:
                    text = next_elem.get_text(strip=True)
                    if text:
                        answer_parts.append(text)
                next_elem = next_elem.find_next_sibling()
            
            answer = self._normalize_text(' '.join(answer_parts))
            question = self._normalize_text(heading_text)
            question = re.sub(r'^\d+\.\s*', '', question)  # Remove leading numbers
            
            if question and answer and len(answer) > 20:
                self._add_faq(question, answer, url)
        
        # Pattern 4: Definition lists (dt/dd)
        for dl in elem.find_all('dl'):
            dts = dl.find_all('dt')
            dds = dl.find_all('dd')
            for i, dt in enumerate(dts):
                question = self._normalize_text(dt.get_text(strip=True))
                if i < len(dds):
                    answer = self._normalize_text(dds[i].get_text(strip=True))
                    if question and answer:
                        self._add_faq(question, answer, url)
        
        # Pattern 5: Markdown-style FAQ patterns in text
        text = elem.get_text()
        pattern = r'FAQ\s*Question\s*\d*\.?\s*([^\n]+?)(?:\n|FAQ\s*Answer)'
        answer_pattern = r'FAQ\s*Answer\s*\d*\.?\s*([^#]+?)(?=FAQ\s*Question|\Z|####)'
        
        questions = re.findall(pattern, text, re.IGNORECASE)
        answers = re.findall(answer_pattern, text, re.IGNORECASE | re.DOTALL)
        
        for i, q in enumerate(questions):
            q_clean = self._normalize_text(q)
            if i < len(answers):
                a_clean = self._normalize_text(answers[i])
                if q_clean and a_clean and len(a_clean) > 20:
                    self._add_faq(q_clean, a_clean, url)
        
        # Pattern 6: Paragraph-based Q&A (question paragraph ending with ?, followed by answer paragraph)
        paragraphs = elem.find_all('p')
        i = 0
        while i < len(paragraphs) - 1:
            p_text = paragraphs[i].get_text(strip=True)
            # Check if this paragraph looks like a question (ends with ? or starts with question words)
            if p_text.endswith('?') or p_text.lower().startswith(('can ', 'do ', 'does ', 'is ', 'are ', 'how ', 'what ', 'why ', 'when ', 'where ', 'will ', 'should ')):
                question = self._normalize_text(p_text)
                # Next paragraph(s) are the answer
                answer_parts = []
                j = i + 1
                while j < len(paragraphs):
                    next_text = paragraphs[j].get_text(strip=True)
                    # Stop if we hit another question
                    if next_text.endswith('?') or next_text.lower().startswith(('can ', 'do ', 'does ', 'is ', 'are ', 'how ', 'what ', 'why ', 'when ', 'where ', 'will ', 'should ')):
                        break
                    answer_parts.append(next_text)
                    j += 1
                
                answer = self._normalize_text(' '.join(answer_parts))
                if question and answer and len(answer) > 20:
                    self._add_faq(question, answer, url)
                i = j  # Skip to the next question
            else:
                i += 1

    def _fetch_page_sync(self, url: str) -> Optional[Tuple[BeautifulSoup, str, List[Tuple[str, str]]]]:
        """Fetch a page using requests (fallback method)."""
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            }
            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code >= 400:
                return None
            
            html = response.text
            soup = BeautifulSoup(html, "html.parser")
            
            for element in soup(["script", "style", "noscript", "iframe"]):
                element.decompose()
            
            links = self._extract_links_with_text(soup)
            
            return soup, html, links
            
        except Exception as e:
            print(f"Error fetching {url}: {e}")
            return None

    async def _fetch_page(self, page, url: str) -> Optional[Tuple[BeautifulSoup, str, List[Tuple[str, str]]]]:
        """Fetch a page and return soup, html, and links with text."""
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout)
            if not response or response.status >= 400:
                return None
            
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except:
                pass
            
            html = await page.content()
            soup = BeautifulSoup(html, "lxml")
            
            for element in soup(["script", "style", "noscript", "iframe"]):
                element.decompose()
            
            links = self._extract_links_with_text(soup)
            
            return soup, html, links
            
        except Exception as e:
            print(f"Playwright error for {url}, trying requests fallback...")
            return self._fetch_page_sync(url)

    def scrape_sync(self) -> Dict:
        """Synchronous scrape method using requests only."""
        # First, fetch homepage to get all links
        self.visited_urls.add(self.base_url)
        print(f"Crawling: {self.base_url}")
        
        homepage_result = self._fetch_page_sync(self.base_url)
        all_links_with_text = []
        
        if homepage_result:
            soup, html, links_with_text = homepage_result
            all_links_with_text = links_with_text
            
            # Check if homepage itself has FAQs
            self._extract_faqs_from_page(soup, html, self.base_url)
        
        # Look for FAQ links by URL path OR link text
        faq_links = [(url, text) for url, text in all_links_with_text if self._is_faq_link(url, text)]
        
        if faq_links:
            # FAQ page found! Crawl only FAQ pages
            faq_urls = [url for url, _ in faq_links]
            print(f"FAQ page(s) found: {faq_urls}")
            for faq_url, _ in faq_links:
                if faq_url not in self.visited_urls:
                    self.visited_urls.add(faq_url)
                    print(f"Crawling FAQ: {faq_url}")
                    
                    result = self._fetch_page_sync(faq_url)
                    if result:
                        soup, html, _ = result
                        self._extract_faqs_from_page(soup, html, faq_url)
                        self.faq_page_found = True
            
            # If we found FAQs from dedicated FAQ pages, we're done
            if self.all_faqs:
                print(f"Found {len(self.all_faqs)} FAQs from dedicated FAQ page(s). Stopping crawl.")
        
        # If no FAQ page found or no FAQs extracted, crawl all pages
        if not self.all_faqs:
            print("No dedicated FAQ page found or no FAQs extracted. Crawling all pages...")
            to_crawl = [(url, text) for url, text in all_links_with_text if url not in self.visited_urls]
            
            while to_crawl and len(self.visited_urls) < self.max_pages:
                url, _ = to_crawl.pop(0)
                if url in self.visited_urls:
                    continue
                
                self.visited_urls.add(url)
                print(f"Crawling: {url}")
                
                result = self._fetch_page_sync(url)
                if result:
                    soup, html, links_with_text = result
                    self._extract_faqs_from_page(soup, html, url)
                    
                    # Add new links
                    new_links = [(l, t) for l, t in links_with_text if l not in self.visited_urls]
                    to_crawl.extend(new_links)
        
        return {
            "website": self.base_url,
            "faqs": self.all_faqs,
            "metadata": {
                "pagesProcessed": len(self.visited_urls),
                "totalFaqsFound": len(self.all_faqs),
                "faqPageFound": self.faq_page_found,
                "extractedAt": datetime.now(timezone.utc).isoformat()
            }
        }

    async def scrape(self) -> Dict:
        """Main scrape method - optimized to stop early if FAQ page found."""
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=[
                        '--disable-blink-features=AutomationControlled',
                        '--no-sandbox',
                        '--disable-dev-shm-usage',
                        '--disable-gpu'
                    ]
                )
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    viewport={'width': 1920, 'height': 1080}
                )
                context.set_default_timeout(self.timeout)
                
                # Use a single page for all requests to avoid browser crashes
                page = await context.new_page()
                
                # First, fetch homepage to get all links
                self.visited_urls.add(self.base_url)
                print(f"Crawling: {self.base_url}")
                
                homepage_result = await self._fetch_page(page, self.base_url)
                all_links_with_text = []
                
                if homepage_result:
                    soup, html, links_with_text = homepage_result
                    all_links_with_text = links_with_text
                    
                    # Check if homepage itself has FAQs
                    self._extract_faqs_from_page(soup, html, self.base_url)
                
                # Look for FAQ links by URL path OR link text
                faq_links = [(url, text) for url, text in all_links_with_text if self._is_faq_link(url, text)]
                
                if faq_links:
                    # FAQ page found! Crawl only FAQ pages
                    faq_urls = [url for url, _ in faq_links]
                    print(f"FAQ page(s) found: {faq_urls}")
                    for faq_url, _ in faq_links:
                        if faq_url not in self.visited_urls:
                            self.visited_urls.add(faq_url)
                            print(f"Crawling FAQ: {faq_url}")
                            
                            result = await self._fetch_page(page, faq_url)
                            if result:
                                soup, html, _ = result
                                self._extract_faqs_from_page(soup, html, faq_url)
                                self.faq_page_found = True
                    
                    # If we found FAQs from dedicated FAQ pages, we're done
                    if self.all_faqs:
                        print(f"Found {len(self.all_faqs)} FAQs from dedicated FAQ page(s). Stopping crawl.")
                
                # If no FAQ page found or no FAQs extracted, crawl all pages
                if not self.all_faqs:
                    print("No dedicated FAQ page found or no FAQs extracted. Crawling all pages...")
                    to_crawl = [(url, text) for url, text in all_links_with_text if url not in self.visited_urls]
                    
                    while to_crawl and len(self.visited_urls) < self.max_pages:
                        url, _ = to_crawl.pop(0)
                        if url in self.visited_urls:
                            continue
                        
                        self.visited_urls.add(url)
                        print(f"Crawling: {url}")
                        
                        result = await self._fetch_page(page, url)
                        if result:
                            soup, html, links_with_text = result
                            self._extract_faqs_from_page(soup, html, url)
                            
                            # Add new links
                            new_links = [(l, t) for l, t in links_with_text if l not in self.visited_urls]
                            to_crawl.extend(new_links)
                
                try:
                    await page.close()
                except:
                    pass
                try:
                    await context.close()
                except:
                    pass
                try:
                    await browser.close()
                except:
                    pass
        except Exception as e:
            print(f"Playwright failed, falling back to requests: {e}")
            return self.scrape_sync()
        
        return {
            "website": self.base_url,
            "faqs": self.all_faqs,
            "metadata": {
                "pagesProcessed": len(self.visited_urls),
                "totalFaqsFound": len(self.all_faqs),
                "faqPageFound": self.faq_page_found,
                "extractedAt": datetime.now(timezone.utc).isoformat()
            }
        }


def run_scraper(url: str, max_pages: int = 50) -> Dict:
    """Run the scraper using requests (more reliable)."""
    scraper = FAQScraper(website_url=url, max_pages=max_pages)
    return scraper.scrape_sync()


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/scrape', methods=['POST'])
def scrape():
    data = request.get_json()
    url = data.get('url', '').strip()
    max_pages = data.get('maxPages', 50)
    
    if not url:
        return jsonify({'error': 'URL is required'}), 400
    
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    
    try:
        result = run_scraper(url, max_pages)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True, port=5000)

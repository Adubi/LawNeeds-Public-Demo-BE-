import requests
from bs4 import BeautifulSoup
import logging
from typing import Dict, Optional, List
import urllib.parse
from urllib.parse import urljoin
import time

logger = logging.getLogger(__name__)

class WebScraper:
    def __init__(self):
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        self.session = requests.Session()
    
    def scrape_url(self, url: str) -> Dict[str, str]:
        """
        Scrape content from a single URL
        """
        try:
            # Add delay to be respectful to servers
            time.sleep(2)
            
            response = self.session.get(url, headers=self.headers, timeout=10)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Remove unwanted elements
            for element in soup(['script', 'style', 'nav', 'footer', 'iframe']):
                element.decompose()
            
            # Extract content
            content = {
                'title': self._get_title(soup) or '',
                'main_content': self._get_main_content(soup) or '',
                'meta_description': self._get_meta_description(soup) or '',
                'url': url
            }
            
            logger.info(f"Successfully scraped {url}")
            return content
            
        except Exception as e:
            logger.error(f"Error scraping {url}: {str(e)}")
            return {
                'error': str(e),
                'title': '',
                'main_content': '',
                'meta_description': '',
                'url': url
            }
    
    def _get_title(self, soup: BeautifulSoup) -> str:
        """Extract page title"""
        if soup.title:
            return soup.title.string.strip()
        return ''
    
    def _get_meta_description(self, soup: BeautifulSoup) -> str:
        """Extract meta description"""
        meta = soup.find('meta', attrs={'name': 'description'})
        if meta and 'content' in meta.attrs:
            return meta['content'].strip()
        return ''
    
    def _get_main_content(self, soup: BeautifulSoup) -> str:
        """Extract main content using common content containers"""
        content = ''
        
        # Try to find main content area
        main_content = soup.find(['main', 'article', 'div'], 
                               class_=['content', 'main-content', 'article-content'])
        
        if main_content:
            content = main_content.get_text(separator=' ', strip=True)
        else:
            # Fallback to body content
            content = soup.body.get_text(separator=' ', strip=True)
        
        return ' '.join(content.split())
    
    def _get_links(self, soup: BeautifulSoup, base_url: str) -> List[str]:
        """Extract all links from the page"""
        links = []
        for link in soup.find_all('a', href=True):
            href = link['href']
            absolute_url = urljoin(base_url, href)
            if self._is_valid_url(absolute_url):
                links.append(absolute_url)
        return links
    
    def _is_valid_url(self, url: str) -> bool:
        """Check if URL is valid and not a resource file"""
        try:
            parsed = urllib.parse.urlparse(url)
            if not parsed.scheme or not parsed.netloc:
                return False
            
            # Exclude common resource files
            excluded_extensions = ['.pdf', '.jpg', '.png', '.gif', '.css', '.js']
            return not any(url.lower().endswith(ext) for ext in excluded_extensions)
            
        except Exception:
            return False
from bs4 import BeautifulSoup
import re

def clean_html(html_content: str) -> str:
    """Strip HTML tags, scripts, and styles, returning clean plain text from the main container if found"""
    if not html_content:
        return ""
    soup = BeautifulSoup(html_content, "html.parser")
    
    # Locate main content container to bypass universal headers/footers
    main_selectors = [
        "main", "article", "#maincontent", ".column.main", ".page-main",
        ".cms-content", ".product-info-main", ".product-view", ".product-detail",
        ".main-content", "#main-content"
    ]
    
    main_el = None
    import copy
    for selector in main_selectors:
        try:
            el = soup.select_one(selector)
            if el:
                # Deep copy to check text length without modifying original tree
                el_copy = copy.deepcopy(el)
                for element in el_copy(["script", "style", "head", "title", "meta", "[document]", "noscript", "iframe"]):
                    element.decompose()
                text_check = el_copy.get_text()
                text_check = re.sub(r'\s+', ' ', text_check).strip()
                if len(text_check) > 100:
                    main_el = el
                    break
        except Exception:
            continue
            
    container = main_el if main_el else soup
    
    # Strip script/style tags from the selected container
    for element in container(["script", "style", "head", "title", "meta", "[document]", "noscript", "iframe"]):
        element.decompose()
        
    text = container.get_text()
    text = re.sub(r'\s+', ' ', text).strip()
    return text

"""
NEWS HTML SANITIZER
Fixes for the news aggregator to strip HTML tags from descriptions
Add this to your news_aggregator.py or create as a separate utility
"""

import re
from html import unescape

def strip_html_tags(text):
    """Remove HTML tags and clean up text for display"""
    if not text:
        return ""
    
    # First unescape HTML entities
    text = unescape(text)
    
    # Remove script and style elements entirely
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    
    # Remove all HTML tags
    text = re.sub(r'<[^>]+>', '', text)
    
    # Clean up whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    
    # Remove any remaining HTML entities
    text = re.sub(r'&[a-zA-Z]+;', '', text)
    text = re.sub(r'&#\d+;', '', text)
    
    return text

def clean_news_article(article):
    """Clean all text fields in a news article dict"""
    if 'title' in article:
        article['title'] = strip_html_tags(article['title'])
    if 'description' in article:
        article['description'] = strip_html_tags(article['description'])
    if 'summary' in article:
        article['summary'] = strip_html_tags(article['summary'])
    if 'content' in article:
        article['content'] = strip_html_tags(article['content'])
    return article


# ===========================================
# UPDATED PARSE_FEED FUNCTION
# Replace the existing parse_feed function in news_aggregator.py with this:
# ===========================================

def parse_feed_fixed(feed_url, source_name):
    """Parse RSS feed and extract relevant articles with HTML stripping"""
    import feedparser
    
    articles = []
    try:
        feed = feedparser.parse(feed_url)
        
        for entry in feed.entries[:20]:  # Limit to 20 per source
            # Get description/summary
            description = ''
            if hasattr(entry, 'description'):
                description = entry.description
            elif hasattr(entry, 'summary'):
                description = entry.summary
            elif hasattr(entry, 'content'):
                description = entry.content[0].value if entry.content else ''
            
            # CRITICAL: Strip HTML tags
            clean_title = strip_html_tags(entry.get('title', ''))
            clean_description = strip_html_tags(description)
            
            # Check relevance
            full_text = f"{clean_title} {clean_description}".lower()
            
            # DC keywords for relevance filtering
            dc_keywords = [
                'data center', 'datacenter', 'data centre', 'colocation', 'colo',
                'hyperscale', 'megawatt', 'mw capacity', 'server farm',
                'cloud infrastructure', 'ai infrastructure', 'gpu cluster',
                'digital infrastructure', 'edge computing', 'fiber optic',
                'power capacity', 'cooling system', 'ups system',
                'equinix', 'digital realty', 'cyrusone', 'qts', 'coresite',
                'vantage', 'compass datacenters', 'stack infrastructure',
                'aws', 'azure', 'google cloud', 'meta', 'microsoft',
                'amazon web services', 'oracle cloud'
            ]
            
            is_relevant = any(kw in full_text for kw in dc_keywords)
            
            if is_relevant and clean_title:
                article = {
                    'title': clean_title,
                    'description': clean_description[:500] if clean_description else '',  # Limit length
                    'url': entry.get('link', ''),
                    'source': source_name,
                    'published': entry.get('published', ''),
                    'categories': extract_categories(full_text),
                    'companies': extract_companies(full_text),
                    'locations': extract_locations(full_text)
                }
                articles.append(article)
                
    except Exception as e:
        print(f"Error parsing {source_name}: {e}")
    
    return articles


# ===========================================
# DATABASE UPDATE SCRIPT
# Run this to clean existing articles in the database
# ===========================================

def clean_existing_articles_in_db(db_path='dc_nexus.db'):
    """Clean HTML from all existing news articles in database"""
    import sqlite3
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Get all articles
    cursor.execute("SELECT id, title, description FROM news_articles")
    articles = cursor.fetchall()
    
    cleaned_count = 0
    for article_id, title, description in articles:
        clean_title = strip_html_tags(title) if title else ''
        clean_desc = strip_html_tags(description) if description else ''
        
        # Only update if changed
        if clean_title != title or clean_desc != description:
            cursor.execute("""
                UPDATE news_articles 
                SET title = ?, description = ?
                WHERE id = ?
            """, (clean_title, clean_desc, article_id))
            cleaned_count += 1
    
    conn.commit()
    conn.close()
    
    print(f"Cleaned {cleaned_count} articles")
    return cleaned_count


# ===========================================
# QUICK FIX FOR API ENDPOINT
# Add this to your api_server.py news route
# ===========================================

"""
# In your /api/v1/news endpoint, add this helper:

import re
from html import unescape

def strip_html(text):
    if not text:
        return ""
    text = unescape(text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

# Then in your news route, clean each article before returning:
for article in articles:
    article['title'] = strip_html(article.get('title', ''))
    article['description'] = strip_html(article.get('description', ''))
"""


if __name__ == '__main__':
    # Run cleanup on existing database
    import sys
    
    if len(sys.argv) > 1:
        db_path = sys.argv[1]
    else:
        db_path = 'dc_nexus.db'
    
    print(f"Cleaning HTML from articles in {db_path}...")
    clean_existing_articles_in_db(db_path)
    print("Done!")

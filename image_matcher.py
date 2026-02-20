# backend/services/image_matcher.py
# Drop this into your Replit backend

import re
import os
import json
import uuid
from typing import Dict, List, Optional
from datetime import datetime

# Simple file-based storage (replace with your DB later)
IMAGE_LIBRARY_FILE = 'data/image_library.json'

def ensure_data_dir():
    os.makedirs('data', exist_ok=True)

def load_image_library():
    ensure_data_dir()
    if not os.path.exists(IMAGE_LIBRARY_FILE):
        initial_library = get_initial_library()
        save_image_library(initial_library)
        return initial_library
    with open(IMAGE_LIBRARY_FILE, 'r') as f:
        return json.load(f)

def save_image_library(library):
    ensure_data_dir()
    with open(IMAGE_LIBRARY_FILE, 'w') as f:
        json.dump(library, f, indent=2)

def get_initial_library():
    """Initialize with placeholder images - replace URLs with your actual images."""
    return [
        {'id': str(uuid.uuid4()), 'category': 'facility_exterior', 'subcategory': 'modern',
         'tags': ['data center', 'building', 'exterior', 'modern'], 'url': '/assets/images/library/dc-exterior-1.jpg', 'style': 'photorealistic', 'usage_count': 0},
        {'id': str(uuid.uuid4()), 'category': 'facility_exterior', 'subcategory': 'campus',
         'tags': ['data center', 'campus', 'hyperscale'], 'url': '/assets/images/library/dc-exterior-2.jpg', 'style': 'photorealistic', 'usage_count': 0},
        {'id': str(uuid.uuid4()), 'category': 'construction', 'subcategory': 'groundbreaking',
         'tags': ['construction', 'build', 'development', 'site'], 'url': '/assets/images/library/dc-construction-1.jpg', 'style': 'photorealistic', 'usage_count': 0},
        {'id': str(uuid.uuid4()), 'category': 'construction', 'subcategory': 'steel_frame',
         'tags': ['construction', 'steel', 'frame', 'progress'], 'url': '/assets/images/library/dc-construction-2.jpg', 'style': 'photorealistic', 'usage_count': 0},
        {'id': str(uuid.uuid4()), 'category': 'facility_interior', 'subcategory': 'server_room',
         'tags': ['servers', 'racks', 'interior', 'data center'], 'url': '/assets/images/library/dc-interior-1.jpg', 'style': 'photorealistic', 'usage_count': 0},
        {'id': str(uuid.uuid4()), 'category': 'power_infrastructure', 'subcategory': 'substation',
         'tags': ['power', 'substation', 'electricity', 'grid'], 'url': '/assets/images/library/power-substation-1.jpg', 'style': 'photorealistic', 'usage_count': 0},
        {'id': str(uuid.uuid4()), 'category': 'power_infrastructure', 'subcategory': 'solar',
         'tags': ['solar', 'renewable', 'energy', 'green'], 'url': '/assets/images/library/power-solar-1.jpg', 'style': 'photorealistic', 'usage_count': 0},
        {'id': str(uuid.uuid4()), 'category': 'business', 'subcategory': 'deal',
         'tags': ['acquisition', 'deal', 'business', 'corporate'], 'url': '/assets/images/library/business-deal-1.jpg', 'style': 'photorealistic', 'usage_count': 0},
        {'id': str(uuid.uuid4()), 'category': 'abstract_tech', 'subcategory': 'digital',
         'tags': ['technology', 'digital', 'abstract', 'data'], 'url': '/assets/images/library/abstract-tech-1.jpg', 'style': 'abstract', 'usage_count': 0},
        {'id': str(uuid.uuid4()), 'category': 'fiber_network', 'subcategory': 'fiber_cables',
         'tags': ['fiber', 'cables', 'network', 'connectivity'], 'url': '/assets/images/library/fiber-cables-1.jpg', 'style': 'photorealistic', 'usage_count': 0},
        {'id': str(uuid.uuid4()), 'category': 'maps_locations', 'subcategory': 'aerial',
         'tags': ['aerial', 'map', 'location', 'geography'], 'url': '/assets/images/library/aerial-view-1.jpg', 'style': 'photorealistic', 'usage_count': 0},
    ]


class ImageMatcher:
    """Intelligent image matching based on content analysis."""
    
    KEYWORD_RULES = {
        'construction': ['construction', 'build', 'development', 'groundbreaking', 'site', 'phase', 'permit', 'planned', 'develops'],
        'business': ['acquisition', 'acquire', 'merge', 'merger', 'deal', 'purchase', 'investment', 'funding', 'partnership', 'sells', 'sold', 'billion', 'million'],
        'power_infrastructure': ['power', 'mw', 'megawatt', 'energy', 'electricity', 'substation', 'grid', 'renewable', 'solar', 'wind', 'generator'],
        'fiber_network': ['fiber', 'connectivity', 'network', 'interconnect', 'cable', 'submarine', 'bandwidth'],
        'facility_interior': ['rack', 'server', 'cooling', 'hvac', 'capacity', 'colocation', 'cabinet', 'cage'],
        'facility_exterior': ['campus', 'facility', 'building', 'data center', 'hyperscale', 'expansion', 'sqft', 'square feet', 'opens', 'operational'],
        'maps_locations': ['location', 'region', 'market', 'geography', 'site selection', 'land', 'property', 'enters']
    }
    
    def __init__(self):
        self.library = load_image_library()
    
    def find_best_match(self, title: str, content: str = '', category: Optional[str] = None, style: str = 'photorealistic') -> Dict:
        """Find the best matching image for given content."""
        text = f"{title} {content}".lower()
        
        # Score each category
        scores = {}
        for cat, keywords in self.KEYWORD_RULES.items():
            score = sum(1 for kw in keywords if kw.lower() in text)
            if score > 0:
                scores[cat] = score
        
        # Get top category
        if category:
            top_category = category
            confidence = 0.9
        elif scores:
            top_category = max(scores, key=scores.get)
            confidence = min(scores[top_category] / 5, 1.0)
        else:
            top_category = 'abstract_tech'
            confidence = 0.3
        
        self.library = load_image_library()
        
        # Filter images
        matching = [img for img in self.library if img['category'] == top_category]
        if not matching:
            matching = [img for img in self.library if img['category'] == 'abstract_tech']
        
        if not matching:
            return {'success': False, 'error': 'No images available'}
        
        # Sort by usage (prefer less-used)
        matching.sort(key=lambda x: x.get('usage_count', 0))
        
        best = matching[0]
        self._increment_usage(best['id'])
        
        return {
            'success': True,
            'image': {'id': best['id'], 'url': best['url'], 'category': best['category'], 'tags': best.get('tags', [])},
            'confidence': confidence,
            'matched_category': top_category,
            'alternatives': [{'id': img['id'], 'url': img['url']} for img in matching[1:4]]
        }
    
    def _increment_usage(self, image_id: str):
        """Increment usage count for an image."""
        for img in self.library:
            if img['id'] == image_id:
                img['usage_count'] = img.get('usage_count', 0) + 1
                break
        save_image_library(self.library)
    
    def add_image(self, url: str, category: str, tags: List[str], subcategory: str = None, style: str = 'photorealistic') -> Dict:
        """Add a new image to the library."""
        image = {
            'id': str(uuid.uuid4()),
            'url': url,
            'category': category,
            'subcategory': subcategory,
            'tags': tags,
            'style': style,
            'usage_count': 0,
            'created_at': datetime.utcnow().isoformat()
        }
        self.library.append(image)
        save_image_library(self.library)
        return image
    
    def list_images(self, category: str = None, limit: int = 50) -> List[Dict]:
        """List images with optional category filter."""
        self.library = load_image_library()
        if category:
            filtered = [img for img in self.library if img['category'] == category]
        else:
            filtered = self.library
        return filtered[:limit]


# Singleton instance
_matcher = None

def get_matcher() -> ImageMatcher:
    global _matcher
    if _matcher is None:
        _matcher = ImageMatcher()
    return _matcher

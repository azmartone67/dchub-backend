"""
Image Matcher Service
Matches content to appropriate images from the library.
"""

import json
import os
from typing import Dict, List, Optional

IMAGES_FILE = 'data/images.json'

class ImageMatcher:
    def __init__(self):
        self.images = []
        self.load_images()
    
    def load_images(self):
        if os.path.exists(IMAGES_FILE):
            with open(IMAGES_FILE, 'r') as f:
                self.images = json.load(f)
        else:
            self.images = self._get_default_images()
            self._save_images()
    
    def _save_images(self):
        os.makedirs('data', exist_ok=True)
        with open(IMAGES_FILE, 'w') as f:
            json.dump(self.images, f, indent=2)
    
    def _get_default_images(self) -> List[Dict]:
        return [
            {
                "id": "dc-construction-1",
                "url": "https://images.unsplash.com/photo-1558494949-ef010cbdcc31",
                "category": "construction",
                "tags": ["construction", "building", "cranes", "development"],
                "style": "photorealistic"
            },
            {
                "id": "dc-server-1",
                "url": "https://images.unsplash.com/photo-1544197150-b99a580bb7a8",
                "category": "servers",
                "tags": ["servers", "racks", "technology", "hardware"],
                "style": "photorealistic"
            },
            {
                "id": "dc-cooling-1",
                "url": "https://images.unsplash.com/photo-1600267165477-6d4cc741b379",
                "category": "cooling",
                "tags": ["cooling", "hvac", "infrastructure"],
                "style": "photorealistic"
            },
            {
                "id": "dc-power-1",
                "url": "https://images.unsplash.com/photo-1473341304170-971dccb5ac1e",
                "category": "power",
                "tags": ["power", "electrical", "grid", "energy"],
                "style": "photorealistic"
            },
            {
                "id": "dc-exterior-1",
                "url": "https://images.unsplash.com/photo-1558494949-ef010cbdcc31",
                "category": "exterior",
                "tags": ["building", "facility", "campus"],
                "style": "photorealistic"
            }
        ]
    
    def get_categories(self) -> List[Dict]:
        categories = {}
        for img in self.images:
            cat = img.get('category', 'other')
            if cat not in categories:
                categories[cat] = {'name': cat, 'count': 0, 'sample_image': img.get('url')}
            categories[cat]['count'] += 1
        return list(categories.values())
    
    def find_best_match(self, title: str, content: str = "", category: str = None, style: str = 'photorealistic') -> Dict:
        """Alias for match() - used by images.py API"""
        return self.match(title, content, category, style)
    
    def match(self, title: str, content: str = "", category: str = None, preferred_style: str = None) -> Dict:
        text = f"{title} {content}".lower()
        
        best_match = None
        best_score = 0
        
        for img in self.images:
            score = 0
            
            if category and img.get('category') == category:
                score += 5
            
            for tag in img.get('tags', []):
                if tag.lower() in text:
                    score += 2
            
            if preferred_style and img.get('style') == preferred_style:
                score += 1
            
            if score > best_score:
                best_score = score
                best_match = img
        
        if not best_match and self.images:
            best_match = self.images[0]
            best_score = 0.5
        
        confidence = min(best_score / 10, 1.0) if best_match else 0
        
        alternatives = [i for i in self.images if i != best_match][:3]
        
        return {
            'image': best_match,
            'confidence': confidence,
            'matched_category': best_match.get('category') if best_match else None,
            'alternatives': alternatives
        }
    
    def get_library(self, category: str = None, limit: int = 50) -> List[Dict]:
        images = self.images
        if category:
            images = [i for i in images if i.get('category') == category]
        return images[:limit]
    
    def add_image(self, image_data: Dict) -> Dict:
        self.images.append(image_data)
        self._save_images()
        return image_data


_matcher = None

def get_matcher() -> ImageMatcher:
    global _matcher
    if _matcher is None:
        _matcher = ImageMatcher()
    return _matcher

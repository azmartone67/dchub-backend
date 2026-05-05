# backend/api/images.py
# Drop this into your Replit backend

from flask import Blueprint, jsonify, request
from functools import wraps

# Import your image matcher
from services.image_matcher import get_matcher

images_bp = Blueprint('images', __name__)

def rate_limit(limit=100, per=60):
    """Simple rate limit decorator - replace with your implementation."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            return f(*args, **kwargs)
        return wrapper
    return decorator


@images_bp.route('/api/v1/images/match', methods=['POST'])
@rate_limit(limit=100, per=60)
def match_image():
    """
    Find best matching image for content.
    
    Request:
    {
        "title": "Equinix Announces 50MW Expansion in Dallas",
        "content": "...",
        "category": null,
        "preferred_style": "photorealistic"
    }
    
    Response:
    {
        "success": true,
        "image": {
            "id": "uuid",
            "url": "https://...",
            "category": "construction",
            "tags": [...]
        },
        "confidence": 0.87,
        "matched_category": "construction",
        "alternatives": [...]
    }
    """
    data = request.json or {}
    
    if not data.get('title'):
        return jsonify({'success': False, 'error': 'Title is required'}), 400
    
    matcher = get_matcher()
    result = matcher.find_best_match(
        title=data.get('title', ''),
        content=data.get('content', ''),
        category=data.get('category'),
        style=data.get('preferred_style', 'photorealistic')
    )
    
    return jsonify(result)


@images_bp.route('/api/v1/images/library', methods=['GET'])
@rate_limit(limit=100, per=60)
def list_images():
    """
    List images with filtering.
    
    Query params:
    - category: Filter by category
    - limit: Max results (default 50)
    """
    category = request.args.get('category')
    limit = min(int(request.args.get('limit', 50)), 200)
    
    matcher = get_matcher()
    images = matcher.list_images(category=category, limit=limit)
    
    return jsonify({
        'success': True,
        'data': images,
        'meta': {'count': len(images)}
    })


@images_bp.route('/api/v1/images/library', methods=['POST'])
@rate_limit(limit=50, per=60)
def add_image():
    """
    Add a new image to the library.
    
    Request:
    {
        "url": "/assets/images/library/new-image.jpg",
        "category": "construction",
        "subcategory": "groundbreaking",
        "tags": ["construction", "site", "development"],
        "style": "photorealistic"
    }
    """
    data = request.json or {}
    
    required = ['url', 'category', 'tags']
    missing = [f for f in required if f not in data]
    if missing:
        return jsonify({'success': False, 'error': f'Missing fields: {missing}'}), 400
    
    valid_categories = [
        'facility_exterior', 'facility_interior', 'construction',
        'power_infrastructure', 'fiber_network', 'maps_locations',
        'business', 'abstract_tech'
    ]
    
    if data['category'] not in valid_categories:
        return jsonify({'success': False, 'error': f'Invalid category. Use: {valid_categories}'}), 400
    
    matcher = get_matcher()
    image = matcher.add_image(
        url=data['url'],
        category=data['category'],
        tags=data['tags'],
        subcategory=data.get('subcategory'),
        style=data.get('style', 'photorealistic')
    )
    
    return jsonify({
        'success': True,
        'image': image,
        'message': 'Image added to library'
    }), 201


@images_bp.route('/api/v1/images/categories', methods=['GET'])
def list_categories():
    """List available image categories with descriptions."""
    categories = {
        'facility_exterior': {
            'description': 'Data center building exteriors',
            'subcategories': ['modern', 'industrial', 'campus', 'urban']
        },
        'facility_interior': {
            'description': 'Inside data centers',
            'subcategories': ['server_room', 'cooling', 'power_room', 'corridor']
        },
        'construction': {
            'description': 'Data center construction',
            'subcategories': ['groundbreaking', 'steel_frame', 'shell', 'progress']
        },
        'power_infrastructure': {
            'description': 'Power and energy systems',
            'subcategories': ['substation', 'transmission', 'solar', 'generators']
        },
        'fiber_network': {
            'description': 'Connectivity and fiber',
            'subcategories': ['fiber_cables', 'network_room', 'connectivity']
        },
        'maps_locations': {
            'description': 'Geographic and location imagery',
            'subcategories': ['aerial', 'map', 'region', 'city']
        },
        'business': {
            'description': 'Business and corporate',
            'subcategories': ['deal', 'handshake', 'meeting', 'signing']
        },
        'abstract_tech': {
            'description': 'Abstract technology visuals',
            'subcategories': ['digital', 'network', 'cloud', 'data']
        }
    }
    
    return jsonify({
        'success': True,
        'categories': categories
    })


# Register blueprint in your main app:
# from api.images import images_bp
# app.register_blueprint(images_bp)

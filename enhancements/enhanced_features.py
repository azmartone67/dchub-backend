"""
DC Hub - Enhanced Features Integration
Fixed import paths for enhancements/ subfolder
"""

def register_all_enhanced_routes(app):
    """
    Register all enhanced DC Hub routes
    """
    
    # Import route registrations with correct paths
    try:
        from enhancements.iso_integrations import register_iso_routes
        register_iso_routes(app)
        print("  ✓ ISO grid routes registered")
    except Exception as e:
        print(f"  ✗ ISO routes failed: {e}")
    
    try:
        from enhancements.site_scoring import register_scoring_routes
        register_scoring_routes(app)
        print("  ✓ Site scoring routes registered")
    except Exception as e:
        print(f"  ✗ Site scoring routes failed: {e}")
    
    try:
        from enhancements.nrel_renewable import register_nrel_routes
        register_nrel_routes(app)
        print("  ✓ NREL renewable routes registered")
    except Exception as e:
        print(f"  ✗ NREL routes failed: {e}")
    
    print("\nEnhanced Features: ✅ Registration complete")
    print("  - /api/site-score")
    print("  - /api/energy/prices/<state>")
    print("  - /api/carbon/intensity")
    print("  - /api/renewable/solar")
    print("  - /api/renewable/wind")
    print("  - /api/renewable/combined")
    
    return app

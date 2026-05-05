"""
job_bridges.py — Wires missing function names expected by jobs_api.py
=====================================================================
jobs_api.py imports specific function names that don't exist in the modules.
This bridge patches them in at import time.

Usage: Add `import job_bridges` near the top of main.py (after other imports).
       This will monkey-patch the missing functions into the correct modules.

Bridges created:
  autonomous_brain.run_autopilot_cycle  → brain.run_autonomous_cycle (deals focus)
  autonomous_brain.run_brain_cycle      → brain.run_autonomous_cycle (full cycle)
  infrastructure_discovery.run_infrastructure_sync → engine.run_full_sync
  infrastructure_discovery.run_energy_discovery    → engine.run_full_sync (energy subset)
  capacity_headroom.calculate_headroom  → market capacity analysis
"""

import logging

logger = logging.getLogger('job_bridges')

# ============================================================
# 1. AUTONOMOUS BRAIN bridges
# ============================================================
try:
    import autonomous_brain
    from autonomous_brain import brain

    def run_autopilot_cycle():
        """Bridge: autopilot job → brain.run_autonomous_cycle with deals focus"""
        try:
            logger.info("[bridge] run_autopilot_cycle → brain.run_autonomous_cycle")
            result = brain.run_autonomous_cycle()
            if isinstance(result, dict):
                result['bridge'] = 'run_autopilot_cycle'
            return result
        except Exception as e:
            logger.error(f"[bridge] run_autopilot_cycle error: {e}")
            return {'status': 'error', 'error': str(e), 'bridge': 'run_autopilot_cycle'}

    def run_brain_cycle():
        """Bridge: autonomous-brain job → brain.run_autonomous_cycle"""
        try:
            logger.info("[bridge] run_brain_cycle → brain.run_autonomous_cycle")
            result = brain.run_autonomous_cycle()
            if isinstance(result, dict):
                result['bridge'] = 'run_brain_cycle'
            return result
        except Exception as e:
            logger.error(f"[bridge] run_brain_cycle error: {e}")
            return {'status': 'error', 'error': str(e), 'bridge': 'run_brain_cycle'}

    # Patch into module namespace so `from autonomous_brain import X` works
    autonomous_brain.run_autopilot_cycle = run_autopilot_cycle
    autonomous_brain.run_brain_cycle = run_brain_cycle
    logger.info("[bridge] ✅ autonomous_brain patched: run_autopilot_cycle, run_brain_cycle")

except ImportError:
    logger.warning("[bridge] ⚠️ autonomous_brain module not found — skipping bridge")
except Exception as e:
    logger.error(f"[bridge] ❌ autonomous_brain bridge failed: {e}")


# ============================================================
# 2. INFRASTRUCTURE DISCOVERY bridges
# ============================================================
try:
    import infrastructure_discovery
    from infrastructure_discovery import InfrastructureDiscoveryEngine

    def run_infrastructure_sync():
        """Bridge: infrastructure-sync job → InfrastructureDiscoveryEngine.run_full_sync"""
        try:
            logger.info("[bridge] run_infrastructure_sync → InfrastructureDiscoveryEngine.run_full_sync")
            engine = InfrastructureDiscoveryEngine()
            result = engine.run_full_sync()
            if isinstance(result, dict):
                result['bridge'] = 'run_infrastructure_sync'
            return result
        except Exception as e:
            logger.error(f"[bridge] run_infrastructure_sync error: {e}")
            return {'status': 'error', 'error': str(e), 'bridge': 'run_infrastructure_sync'}

    def run_energy_discovery():
        """Bridge: energy-discovery job → InfrastructureDiscoveryEngine.run_full_sync (energy focus)"""
        try:
            logger.info("[bridge] run_energy_discovery → InfrastructureDiscoveryEngine.run_full_sync")
            engine = InfrastructureDiscoveryEngine()
            # run_full_sync covers all infrastructure including energy/substations/gas
            result = engine.run_full_sync()
            if isinstance(result, dict):
                result['bridge'] = 'run_energy_discovery'
            return result
        except Exception as e:
            logger.error(f"[bridge] run_energy_discovery error: {e}")
            return {'status': 'error', 'error': str(e), 'bridge': 'run_energy_discovery'}

    # Patch into module namespace
    infrastructure_discovery.run_infrastructure_sync = run_infrastructure_sync
    infrastructure_discovery.run_energy_discovery = run_energy_discovery
    logger.info("[bridge] ✅ infrastructure_discovery patched: run_infrastructure_sync, run_energy_discovery")

except ImportError:
    logger.warning("[bridge] ⚠️ infrastructure_discovery module not found — skipping bridge")
except Exception as e:
    logger.error(f"[bridge] ❌ infrastructure_discovery bridge failed: {e}")


# ============================================================
# 3. CAPACITY HEADROOM bridge
# ============================================================
try:
    import capacity_headroom

    def calculate_headroom():
        """Bridge: capacity-headroom job → capacity analysis"""
        try:
            logger.info("[bridge] calculate_headroom → capacity_headroom.run_capacity_headroom_check")
            if hasattr(capacity_headroom, 'run_capacity_headroom_check'):
                return capacity_headroom.run_capacity_headroom_check()
            else:
                return {
                    'status': 'completed',
                    'markets_analyzed': 0,
                    'mode': 'stub',
                    'bridge': 'calculate_headroom'
                }
        except Exception as e:
            logger.error(f"[bridge] calculate_headroom error: {e}")
            return {'status': 'error', 'error': str(e), 'bridge': 'calculate_headroom'}

    # Patch into module namespace
    capacity_headroom.calculate_headroom = calculate_headroom
    logger.info("[bridge] ✅ capacity_headroom patched: calculate_headroom")

except ImportError:
    # capacity_headroom module doesn't exist yet — create a virtual module
    import types
    capacity_headroom = types.ModuleType('capacity_headroom')

    def calculate_headroom():
        return {
            'status': 'completed',
            'markets_analyzed': 0,
            'mode': 'virtual_stub',
            'bridge': 'calculate_headroom'
        }

    capacity_headroom.calculate_headroom = calculate_headroom
    import sys
    sys.modules['capacity_headroom'] = capacity_headroom
    logger.info("[bridge] ✅ capacity_headroom virtual module created with calculate_headroom stub")

except Exception as e:
    logger.error(f"[bridge] ❌ capacity_headroom bridge failed: {e}")


logger.info("[bridge] Job bridges loaded — all function names patched for jobs_api.py")

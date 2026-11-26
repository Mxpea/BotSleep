"""BotSleep 2.0 five-layer microkernel package.

Strict single-direction dependency order (enforced by tests_v2/test_00_layer_boundaries.py):
    L4 (v2.planner) -> L3 (v2.tasks, v2.primitives) -> L2 (v2.bt) -> L1 (v2.gateway) -> L0 (v2.kernel) -> v2.domain
"""

__all__ = ["__version__"]
__version__ = "2.0.0-alpha.0"

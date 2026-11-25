"""L4 DEPS Explainer: synthesizes 3-line structured root-cause diagnosis & alternative recovery hint."""

from __future__ import annotations

from v2.bt.blackboard import Blackboard


class DepsExplainer:
    """Describe -> Explain -> Alternative 3-line local diagnostic for NEEDS_AI handoffs."""

    def explain(self, bb: Blackboard) -> str:
        reason = bb.last_failure_reason or str(bb.get("watchdog_reason") or "idle_replan")
        pos = [round(v, 1) for v in bb.pos]

        if bb.get("pending_frozen"):
            cause = "Write channel frozen by unconfirmed pending transaction."
            alt = "Call wait() only; never replay or issue cleanup writes."
        elif "occluded" in reason:
            cause = "Line-of-sight blocked along y+0.85 liquid or dig ray."
            alt = "Clear single grass/dirt occluder or choose another water/lava source."
        elif "explore_budget_exhausted" in reason or "no_exposed_block_found" in reason:
            cause = "Target resource absent in current scanned radius."
            alt = "Switch to explore or mine down to ore Y-band (Y=16 iron / Y=-54 diamond)."
        elif "lava_pool" in reason or "nether" in reason:
            cause = "Lava pool or portal casting prerequisites incomplete."
            alt = "Ensure iron_pickaxe + water_bucket + flint_and_steel, then explore for lava."
        elif "watchdog" in reason or bb.get("watchdog_stalled"):
            cause = f"Repeated no-progress oscillation ({reason})."
            alt = "Switch target goal or explore 20m away to break local geometry trap."
        else:
            cause = f"Last goal stopped with '{reason}' (last_outcome={bb.last_action_outcome})."
            alt = "Pick highest-value HorizonSelector candidate with lowest step cost."

        return (
            f"DESCRIBE: dim={bb.dimension} pos={pos} hp={bb.health:.1f} food={bb.food} goal={bb.active_goal_name or 'none'}\n"
            f"EXPLAIN: {cause}\n"
            f"ALTERNATIVE: {alt}"
        )

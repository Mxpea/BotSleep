"""Deterministic competition selection and production route dispatch."""

from agent.survival_planner import Decision


ROUTES = {
    "minecraft:story/mine_stone": ("gather", "cobblestone"),
    "minecraft:story/upgrade_tools": ("craft", "stone_pickaxe"),
    "minecraft:story/smelt_iron": ("smelt", "iron_ingot"),
    "minecraft:story/iron_tools": ("craft", "iron_pickaxe"),
    "minecraft:story/obtain_armor": ("craft", "iron_boots"),
    "minecraft:story/mine_diamond": ("gather", "diamond"),
    "minecraft:story/shiny_gear": ("craft", "diamond_boots"),
}


def choose(board: dict, attempted: bool) -> Decision | None:
    """Return None only to hand an active goal to model repair."""
    def action(name, args, reason):
        return Decision(name, args, reason, "competition_director")

    if board["stale"]:
        return action("wait", {"seconds": 1}, "Refresh server advancement evidence")
    if board["phase"] == "selection":
        candidates = [row for row in board["tasks"] if row["selectable"]]
        if not candidates:
            return action("wait", {"seconds": 2}, "Reobserve unlocking conditions and cooldowns")
        order = list(ROUTES)
        row = min(candidates, key=lambda r: (r["id"] not in ROUTES, r["availability"] != "ready",
                  order.index(r["id"]) if r["id"] in ROUTES else len(order), -r["points"], r["id"]))
        return action("select_task", {"goal_id": row["id"]}, "Select an unlocked scored goal locally")
    goal = board["active_goal_id"]
    row = next((r for r in board["tasks"] if r["id"] == goal), None)
    if row and row["confirmed"]:
        if board["assessment_complete"]:
            return action("finish_task", {"reason": "Server confirmed the advancement"}, "Close confirmed goal")
        return action("wait", {"seconds": 1}, "Await fresh assessment")
    if goal not in ROUTES or attempted:
        return None
    kind, item = ROUTES[goal]
    return action("run_survival_task", {"task": kind, "resource" if kind == "gather" else "item": item,
                  "count": 1}, "Execute the scored production route locally")

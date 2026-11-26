"""L3b Stateless Recursive Tasks exports."""

from v2.tasks.acquire import AcquireItemTask, SmeltItemTask
from v2.tasks.base import MacroSequenceTask, Task
from v2.tasks.catalogue import GatherEntry, RecipeEntry, SmeltEntry, TaskCatalogue
from v2.tasks.explore import ExploreUntilTask
from v2.tasks.mining import MineResourceTask
from v2.tasks.nether_portal import EnterNetherTask
from v2.tasks.shelter import BuildShelterTask
from v2.tasks.stronghold_end import StrongholdLocateTask
from v2.tasks.surface import ReturnToSurfaceTask, is_underground

__all__ = [
    "AcquireItemTask",
    "BuildShelterTask",
    "EnterNetherTask",
    "ExploreUntilTask",
    "GatherEntry",
    "MacroSequenceTask",
    "MineResourceTask",
    "RecipeEntry",
    "ReturnToSurfaceTask",
    "SmeltEntry",
    "SmeltItemTask",
    "StrongholdLocateTask",
    "Task",
    "TaskCatalogue",
    "is_underground",
]

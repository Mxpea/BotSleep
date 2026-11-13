"""Procedural Skills Module for BotSleep."""

from dataclasses import dataclass, field
from typing import Optional, Dict, Any
from abc import ABC, abstractmethod


@dataclass
class SkillResult:
    success: bool
    message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    error_code: Optional[str] = None


class BaseSkill(ABC):
    """Abstract base class for all procedural macro skills."""

    def __init__(self, client):
        self.client = client

    @abstractmethod
    def validate_preconditions(self, **kwargs) -> bool:
        """Validates that items, space, and environment meet preconditions."""
        pass

    @abstractmethod
    def execute(self, **kwargs) -> SkillResult:
        """Executes the procedural skill sequence."""
        pass

    def rollback(self, reason: str) -> None:
        """Restores safe posture/view on abnormal interruption."""
        pass


from .movement import PillarUpSkill
from .clutch import MLGWaterSkill
from .sleep import AutoSleepSkill
from .exploration import ContinuousRadarSkill

__all__ = [
    "BaseSkill",
    "SkillResult",
    "PillarUpSkill",
    "MLGWaterSkill",
    "AutoSleepSkill",
    "ContinuousRadarSkill",
]

"""Pure stronghold calculation skill; this skill never throws or moves the bot."""

from integrations.ninjabrain import CalculationError, EyeThrow, StrongholdCalculator
from . import BaseSkill, SkillResult


class CalculateStrongholdSkill(BaseSkill):
    """Rank Java 1.19+ stronghold chunks from independent coordinate rays."""

    def __init__(self, client=None, calculator=None):
        super().__init__(client)
        self.calculator = calculator if calculator is not None else StrongholdCalculator()

    def validate_preconditions(self, throws=None, **kwargs) -> bool:
        return isinstance(throws, list) and 1 <= len(throws) <= 32

    def execute(self, throws=None, top_k: int = 5) -> SkillResult:
        if not self.validate_preconditions(throws=throws):
            return SkillResult(False, "Provide 1..32 independent eye observations", error_code="invalid_observations")
        try:
            measurements = [EyeThrow(**value) for value in throws]
            result = self.calculator.calculate(measurements, top_k=top_k)
        except (TypeError, ValueError) as error:
            return SkillResult(False, str(error), error_code="invalid_observations")
        except CalculationError as error:
            return SkillResult(False, str(error), error_code="calculation_failed")
        return SkillResult(True, "Stronghold candidates calculated; location is not confirmed in game", data=result)

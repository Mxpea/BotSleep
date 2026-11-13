"""Run the unmodified Ninjabrain Bot 1.5.2 statistics engine without its GUI."""

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import urllib.request

VERSION = "1.5.2"
JAR_SHA256 = "98099f6321a90d4acec13400e86d05f7af8d60e5639c2f38427e968dc71450ff"
JAR_URL = f"https://github.com/Ninjabrain1/Ninjabrain-Bot/releases/download/{VERSION}/Ninjabrain-Bot-{VERSION}.jar"
ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / ".cache" / "ninjabrain"
JAR_PATH = CACHE / f"Ninjabrain-Bot-{VERSION}.jar"
BRIDGE = Path(__file__).with_name("CalculatorBridge.java")


class CalculationError(RuntimeError):
    """A dependency, observation, or calculation could not be validated."""


def _finite(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


@dataclass(frozen=True)
class EyeThrow:
    """One independent horizontal ray in overworld blocks; vanilla yaw in degrees.

    South (+Z) = 0, west (-X) = 90. Sigma describes angular uncertainty,
    not the precision of the printed coordinates. No visual aim correction is used.
    """

    x: float
    z: float
    bearing_deg: float
    sigma_deg: float = 0.1

    def __post_init__(self):
        for field in ("x", "z", "bearing_deg", "sigma_deg"):
            object.__setattr__(self, field, _finite(getattr(self, field), field))
        if not 0.001 <= self.sigma_deg <= 5:
            raise ValueError("sigma_deg must be between 0.001 and 5 degrees")
        if max(abs(self.x), abs(self.z)) > 30_000_000:
            raise ValueError("observation is outside the Minecraft world border")
        object.__setattr__(self, "bearing_deg", (self.bearing_deg + 180) % 360 - 180)

    @classmethod
    def from_positions(cls, origin, eye, sigma_deg: float = 0.1):
        """Build a ray from two [x,y,z] positions on the same eye trajectory.

        A single eye position is insufficient without a known ray origin. Prefer
        two or more entity samples to player-to-eye offsets and rounded bearings.
        """
        if len(origin) != 3 or len(eye) != 3:
            raise ValueError("positions must contain x, y, z")
        values = [_finite(v, "position") for p in (origin, eye) for v in p]
        dx, dz = values[3] - values[0], values[5] - values[2]
        if math.hypot(dx, dz) < 0.1:
            raise ValueError("eye positions are too close to determine a direction")
        return cls(values[0], values[2], math.degrees(math.atan2(-dx, dz)), sigma_deg)


def _check_jar(path: Path) -> None:
    if not path.is_file():
        raise CalculationError("Ninjabrain JAR missing; run: python -m integrations.ninjabrain setup")
    if hashlib.sha256(path.read_bytes()).hexdigest() != JAR_SHA256:
        raise CalculationError("Ninjabrain JAR checksum mismatch; run setup to restore the pinned release")


def install_engine() -> Path:
    """Download the official fixed release and verify GitHub's SHA-256 digest."""
    if JAR_PATH.exists():
        try:
            _check_jar(JAR_PATH)
            return JAR_PATH
        except CalculationError:
            pass
    CACHE.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(JAR_URL, timeout=60) as response:
        data = response.read(16_000_001)
    if len(data) > 16_000_000 or hashlib.sha256(data).hexdigest() != JAR_SHA256:
        raise CalculationError("downloaded release failed SHA-256 verification")
    temporary = JAR_PATH.with_suffix(".download")
    temporary.write_bytes(data)
    temporary.replace(JAR_PATH)
    return JAR_PATH


def _java_tool(name: str) -> str:
    home = os.environ.get("JAVA_HOME")
    if home:
        path = Path(home) / "bin" / (name + (".exe" if os.name == "nt" else ""))
        if path.is_file():
            return str(path)
    found = shutil.which(name)
    if not found:
        raise CalculationError(f"{name} is required; install JDK 11 or newer")
    return found


class StrongholdCalculator:
    """JSON bridge to the official Bayesian posterior, including nearest filtering."""

    def __init__(self, jar_path=None, timeout_seconds: float = 30):
        self.jar_path = Path(jar_path) if jar_path else JAR_PATH
        self.timeout_seconds = timeout_seconds

    def calculate(self, throws, top_k: int = 5, advanced: bool = True) -> dict:
        throws = list(throws)
        if not throws or len(throws) > 32:
            raise ValueError("provide between 1 and 32 independent throws")
        if not all(isinstance(t, EyeThrow) for t in throws):
            raise ValueError("throws must contain EyeThrow observations")
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 100:
            raise ValueError("top_k must be an integer between 1 and 100")
        for index, throw in enumerate(throws):
            if any(math.hypot(throw.x - previous.x, throw.z - previous.z) < 1 for previous in throws[:index]):
                raise ValueError("throws must be independent measurements from positions at least 1 block apart")
        _check_jar(self.jar_path)
        java = _java_tool("java")
        digest = hashlib.sha256(BRIDGE.read_bytes() + JAR_SHA256.encode()).hexdigest()[:16]
        classes = CACHE / "classes" / digest
        try:
            if not (classes / "CalculatorBridge.class").exists():
                classes.mkdir(parents=True, exist_ok=True)
                compiled = subprocess.run(
                    [_java_tool("javac"), "-encoding", "UTF-8", "-cp", str(self.jar_path), "-d", str(classes), str(BRIDGE)],
                    capture_output=True, text=True, encoding="utf-8", timeout=30,
                )
                if compiled.returncode:
                    raise CalculationError("Java bridge compilation failed: " + compiled.stderr[-3000:])
            process = subprocess.run(
                [java, "-Djava.awt.headless=true", "-Xmx512m", "-cp", os.pathsep.join((str(classes), str(self.jar_path.resolve()))), "CalculatorBridge"],
                input=json.dumps({"throws": [asdict(t) for t in throws], "top_k": top_k, "advanced": advanced}),
                capture_output=True, text=True, encoding="utf-8", timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as error:
            raise CalculationError("Ninjabrain calculation or compilation timed out") from error
        except OSError as error:
            raise CalculationError(f"could not run Java: {error}") from error
        if process.returncode:
            raise CalculationError("Ninjabrain calculation failed: " + process.stderr[-3000:])
        try:
            result = json.loads(process.stdout)
        except ValueError as error:
            raise CalculationError("Java bridge returned invalid JSON") from error
        if not result.get("ok"):
            raise CalculationError(result.get("error", "no consistent stronghold candidates"))
        result["engine"] = {"name": "Ninjabrain Bot", "version": VERSION, "sha256": JAR_SHA256}
        result["warnings"] = [
            "Probabilities depend on the supplied angular uncertainty and vanilla Java stronghold generation.",
            "API coordinate measurement uncertainty has not yet been calibrated on a real eye throw.",
            "All throws must point to the same stronghold; crossing nearest-stronghold regions invalidates this model.",
        ]
        if len(throws) == 1:
            result["warnings"].append("One throw does not uniquely locate a stronghold; collect a second separated observation.")
        return result

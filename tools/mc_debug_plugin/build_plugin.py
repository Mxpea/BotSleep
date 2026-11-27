"""Build & optionally deploy BotSleepDebug.jar against local Leaves/Paper 1.21+ API jars."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import List, Optional

PLUGIN_ROOT = Path(__file__).resolve().parent
DEFAULT_SERVER_DIR = PLUGIN_ROOT.parents[2] / "minecraft-server"
DEFAULT_JDK_BIN = Path(r"C:\Program Files\Microsoft\jdk-21.0.8.9-hotspot\bin")


def _find_tool(name: str) -> str:
    candidate = DEFAULT_JDK_BIN / f"{name}.exe"
    if candidate.is_file():
        return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    raise FileNotFoundError(f"Unable to locate '{name}' (checked {candidate} and PATH)")


def _find_compile_classpath(libraries_dir: Path) -> str:
    if not libraries_dir.is_dir():
        raise FileNotFoundError(f"Minecraft server libraries directory not found: {libraries_dir}")
    # Pick the highest version jar per Maven artifact directory (libraries/<group...>/<artifact>/<ver>/<jar>)
    by_artifact: dict[Path, List[Path]] = {}
    for hit in sorted(libraries_dir.rglob("*.jar")):
        by_artifact.setdefault(hit.parent.parent, []).append(hit)
    jars: List[str] = [str(sorted(paths)[-1]) for paths in by_artifact.values() if paths]
    if not any("leaves-api" in j or "paper-api" in j or "spigot-api" in j for j in jars):
        raise FileNotFoundError(f"No Bukkit/Paper/Leaves API jar found under {libraries_dir}")
    return os.pathsep.join(sorted(jars))


def build_plugin(*, server_dir: Path, deploy_dir: Optional[Path] = None) -> Path:
    javac = _find_tool("javac")
    jar_bin = _find_tool("jar")
    cp = _find_compile_classpath(server_dir / "libraries")

    java_src = PLUGIN_ROOT / "src" / "main" / "java" / "com" / "botsleep" / "debug" / "BotSleepDebugPlugin.java"
    plugin_yml = PLUGIN_ROOT / "src" / "main" / "resources" / "plugin.yml"
    build_dir = PLUGIN_ROOT / "build" / "classes"
    dist_dir = PLUGIN_ROOT / "dist"
    out_jar = dist_dir / "BotSleepDebug.jar"

    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)
    dist_dir.mkdir(parents=True, exist_ok=True)

    cmd_compile = [
        javac,
        "-J-Duser.language=en",
        "--release", "21",
        "-encoding", "UTF-8",
        "-cp", cp,
        "-d", str(build_dir),
        str(java_src),
    ]
    res = subprocess.run(cmd_compile, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if res.returncode != 0:
        raise RuntimeError(f"javac failed ({res.returncode}):\n{res.stdout}\n{res.stderr}")

    shutil.copy2(plugin_yml, build_dir / "plugin.yml")
    cmd_jar = [jar_bin, "--create", "--file", str(out_jar), "-C", str(build_dir), "."]
    res2 = subprocess.run(cmd_jar, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if res2.returncode != 0:
        raise RuntimeError(f"jar packaging failed ({res2.returncode}):\n{res2.stdout}\n{res2.stderr}")

    if deploy_dir is not None:
        deploy_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(out_jar, deploy_dir / "BotSleepDebug.jar")

    return out_jar


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build and optionally deploy BotSleepDebug.jar.")
    parser.add_argument("--server-dir", default=str(DEFAULT_SERVER_DIR), help="Minecraft server directory containing libraries/")
    parser.add_argument("--deploy", default="", help="Optional plugins directory to copy BotSleepDebug.jar into")
    args = parser.parse_args(argv)

    deploy_path = Path(args.deploy) if args.deploy else None
    out_jar = build_plugin(server_dir=Path(args.server_dir), deploy_dir=deploy_path)
    print(f"Built {out_jar} ({out_jar.stat().st_size} bytes)")
    if deploy_path is not None:
        print(f"Deployed -> {deploy_path / 'BotSleepDebug.jar'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

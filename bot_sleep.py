"""Compatibility entry point; both launch commands use the same configured runner."""

from main import main


if __name__ == "__main__":
    raise SystemExit(main())

"""Small target program demonstrating observable execution effects."""

import sys
from pathlib import Path


def main() -> None:
    """Write two streams and one output artifact."""
    print("Hello from AppMonitor")
    print("Diagnostic message on stderr", file=sys.stderr)
    Path("hello-output.txt").write_text("observed output\n", encoding="utf-8")


if __name__ == "__main__":
    main()


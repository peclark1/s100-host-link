#!/usr/bin/env python3
"""Launch the generated dual-pane development UI."""
from pathlib import Path
import gzip

HERE = Path(__file__).resolve().parent
PAYLOAD = HERE / "s100_hostlink_dualpane.py.gz"
SOURCE_NAME = str(HERE / "s100_hostlink_dualpane.py")
code = gzip.decompress(PAYLOAD.read_bytes())
namespace = {"__name__": "__main__", "__file__": SOURCE_NAME}
exec(compile(code, SOURCE_NAME, "exec"), namespace)

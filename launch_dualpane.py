#!/usr/bin/env python3
"""Launch the generated dual-pane development UI.

The payload on the first dual-pane branch commit arrived with a bad gzip CRC.
The deflate stream itself is still usable, so fall back to raw-deflate decode
and verify the exact expected source SHA-256 before executing anything.
"""
from pathlib import Path
import gzip
import hashlib
import zlib

HERE = Path(__file__).resolve().parent
PAYLOAD = HERE / "s100_hostlink_dualpane.py.gz"
SOURCE_NAME = str(HERE / "s100_hostlink_dualpane.py")
EXPECTED_SHA256 = "4878170fa1a43005672c5aa928cfb771b2e8541fca971c0af0cd022b6de6107c"

payload = PAYLOAD.read_bytes()
try:
    code = gzip.decompress(payload)
except gzip.BadGzipFile:
    # gzip header is 10 bytes and trailer is 8 bytes for this payload.
    # Decode only the raw DEFLATE stream, then independently verify the
    # resulting source so a damaged payload can never be executed silently.
    code = zlib.decompress(payload[10:-8], -zlib.MAX_WBITS)

actual_sha256 = hashlib.sha256(code).hexdigest()
if actual_sha256 != EXPECTED_SHA256:
    raise RuntimeError(
        "Dual-pane source payload failed integrity verification: "
        f"expected {EXPECTED_SHA256}, got {actual_sha256}"
    )

namespace = {"__name__": "__main__", "__file__": SOURCE_NAME}
exec(compile(code, SOURCE_NAME, "exec"), namespace)

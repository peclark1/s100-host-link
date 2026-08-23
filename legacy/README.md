# Legacy / Historical Files

These files are retained for reference but are **not** the current IMSAI target-system HOST implementation.

- `HOST_SBC_V2.ASM` — original Z80 SBC onboard-USB HOST source. It uses data port `34h` and status port `36h`. Do not use this build with the current target system; `34h` is part of the Dual IDE/CF interface there.
- `HOST21.COM` — previous known-good HOST v2.1 binary for the S100Computers Serial I/O V3 DLP-USB interface. It supports PUT/DIR/GET and is retained as a fallback/reference version.
- `DEVELOPMENT_DUAL_PANE.md` — notes from the earlier dual-pane UI development stage, before the current production file-management UI was promoted.

Current CP/M builds are documented in the repository root `README.md` and `HOST_V22.md`.

# Using S-100 Host Link with targetsim

The `z80pack-target-system` emulator exposes the emulated Serial I/O V3 DLP-USB245R interface as a Linux pseudo-terminal.

With the emulator running, the default endpoint is:

```text
/tmp/targets100sim-usb-<uid>
```

`./run.sh` automatically adds a running emulator endpoint to the **USB device** list as **IMSAI target emulator Serial I/O USB**. If no previous serial device is selected, the emulator endpoint is preferred automatically.

The CP/M side is unchanged: boot CP/M in the emulator and run the same Serial I/O `HOST.COM` / `HOST22.COM` used on the physical IMSAI. The guest still accesses USB FIFO status at `AAH` and data at `ACH`; only the host-side transport is replaced by a PTY.

The emulator also supports an alternate endpoint path through `TARGET_SERIALIO_USB_TTY`. If Host Link is started with the same environment variable, it discovers that path too.

No baud-rate matching is required by the emulator bridge; Host Link may keep the normal saved baud setting. pySerial still configures the PTY as 8N1/raw, so the existing Host Link protocol code is unchanged.

# S-100 Host Link — GTK4 v4.1

**v4.1 UI change:** the local Linux file-selection section now appears above the CP/M destination/directory section. Protocol and HOST.COM v2 are unchanged.

A native GTK4/libadwaita Linux host-link utility for the **S-100 Z80 SBC**.

Version 4 extends the working v3 automatic-filename transfer into a small
bidirectional CP/M host service. With the new `HOST.COM` running, the Linux GUI
can now:

- choose a target CP/M **drive** (`A:` through `P:` or the current drive),
- choose a CP/M **user area** (`0` through `15` or the current user),
- request and display a **directory listing** for that selected drive/user,
- send a file directly to the selected drive/user without changing the CP/M
  console, and
- automatically refresh the directory after a successful v2 file transfer.

The previous HOST v1 automatic-filename mode and classic XMODEM mode are both
retained as fallbacks.

## Linux requirements

On Ubuntu/Debian:

```bash
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-serial
```

Run:

```bash
./run.sh
```

or:

```bash
python3 s100_hostlink_gtk4.py
```

## Upgrading from the working v3 HOST.COM

The v4.1 GUI deliberately includes **HOST.COM v1 — legacy automatic filename** so
it can install its own new receiver.

1. Start your existing v3 `HOST.COM` on CP/M with:

   ```text
   HOST
   ```

2. Start the v4.1 Linux GUI.
3. Set **CP/M receiver** to **HOST.COM v1 — legacy automatic filename**.
4. Browse to the new `HOST.COM` included in this v4.1 package.
5. Click **Send File**. The old HOST program is already loaded in memory, so it
   can replace the `HOST.COM` file on disk while it is running.
6. On the CP/M console press **Q** (or Esc) to exit the old HOST program.
7. Run:

   ```text
   HOST
   ```

   again. The banner should now say:

   ```text
   S-100 HOST LINK V2.0
   Linux <-> CP/M drive/user host service
   ```

8. In the GUI select **HOST.COM v2 — drive/user + directory**.

Classic XMODEM remains another way to install the new `HOST.COM` if desired.

## Normal v4.1 workflow

Run `HOST` once on the CP/M VGA/keyboard console and leave it running.

On Linux:

1. Select **HOST.COM v2 — drive/user + directory**.
2. Choose a CP/M **Drive** and **User area**.
3. Press **Refresh Directory** to see the files already present there.
4. Choose a local Linux file.
5. Click **Send File**.

`HOST.COM` temporarily switches to the requested drive/user, creates or replaces
the target file, receives it, then restores the drive and user that were active
before the request. The GUI refreshes the selected directory after a successful
transfer.

`Current drive` / `Current user` mean the CP/M context active when HOST receives
the command. Because HOST restores that context after each operation, selecting
explicit destinations does not leave CP/M silently switched to a different
area.

## Directory display

The GUI requests raw 32-byte CP/M directory entries and merges multiple extents
of the same filename. It shows:

- CP/M 8.3 filename,
- file size as CP/M 128-byte logical records (and the corresponding rounded byte
  size), and
- R/O, SYS and ARC attributes when present.

CP/M directory information is record-based, so the displayed byte size is
necessarily rounded to the 128-byte CP/M logical-record boundary rather than the
original exact Linux byte count.

## Host-link v2 protocol

`HOST.COM` periodically sends `C` (43h) to advertise readiness. Linux then sends
a CRC-protected 128-byte command block 0:

```text
bytes 0-7    "S100HST2"
byte  8      command: 01h PUT, 02h DIR
byte  9      drive: 0=A ... 15=P, FF=current
byte 10      user: 0..15, FF=current
bytes 11-21  raw CP/M 8.3 filename for PUT
bytes 22-25  original Linux byte size (little endian; informational)
```

### PUT

After the command block is ACKed, file data uses the same proven 128-byte
CRC/XMODEM-shaped blocks as v3. Each block is ACKed before Linux sends the next,
and `EOT` closes the CP/M file.

### DIR

After the DIR command is ACKed, CP/M becomes the packet sender. Each 128-byte
CRC-protected packet contains four raw 32-byte CP/M directory entries. Linux
ACKs each packet. The final packet is followed by `EOT`, which Linux ACKs.
Unused slots are padded with entries beginning with `E5h`.

This keeps directory transfer robust while preserving enough CP/M metadata for
the Linux GUI to merge extents and display attributes.

## SBC USB I/O

The receiver continues to use the same direct SBC USB configuration as the
working XMODEM setup:

```text
data port   34h
status port 36h
RX ready    bit 7, active low
TX ready    bit 6, active low
```

## Included files

- `s100_hostlink_gtk4.py` — GTK4/libadwaita Linux GUI
- `HOST.COM` — ready-to-run v2 CP/M host receiver
- `HOST.ASM` — v2 receiver source, written with 8080-compatible instructions
- `build_host.py` — self-contained assembler used to rebuild `HOST.COM`
- `test_host_protocol.py` — headless Linux-side protocol tests
- `run.sh` — launcher
- `s100-host-link.desktop` — desktop-entry template

## Validation performed

For this build:

- `HOST.ASM` assembles successfully to a 2,136-byte `HOST.COM` at `0100h`.
- The Linux GUI source passes Python bytecode compilation.
- Headless protocol tests verify the v2 PUT metadata, drive/user fields,
  multi-block file transfer framing, DIR packet reception, CP/M extent merging,
  record-size calculation, and file attributes.

Real-hardware validation has also been completed successfully on an S-100 Z80 SBC running CP/M 3 over the onboard USB interface.

# S-100 Host Link

A GTK4/libadwaita Linux file manager and CP/M host service for the IMSAI 8080 target system.

## Current production baseline

The current CP/M side is **HOST v2.2** running on the real IMSAI through the **S100Computers Serial I/O V3 DLP-USB245R interface**.

CPU-visible USB interface used by HOST:

```text
status port  AAh
  bit 7      RXE, active low (host -> IMSAI data available)
  bit 6      TXE, active low (host can accept IMSAI -> host data)

data port    ACh
```

This is the interface used by the present target system. The old Z80 SBC onboard-USB implementation at ports `34h/36h` is historical and has been moved under `legacy/` so it cannot be mistaken for the current source.

HOST v2.2 supports:

- PUT: Linux -> CP/M
- DIR: remote CP/M directory browsing
- GET: CP/M -> Linux
- DELETE
- RENAME
- ATTR: R/O, SYS, and CP/M 3 ARC
- explicit CP/M drive and user-area selection
- multi-file drag/drop in both directions from the Linux GUI
- external Linux editor workflow for CP/M files

The v2.2 path has been successfully exercised on the physical IMSAI with the Serial I/O V3 USB connection.

## Run the Linux application

Ubuntu/Debian requirements:

```sh
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-serial
```

Launch:

```sh
./run.sh
```

`run.sh` starts the production UI through `launch_targetsim.py`, which includes the file-management, external-editor, and targetsim serial-discovery layers.

## Edit a CP/M file with a Linux editor

Right-click a single file in the CP/M pane and choose **Edit…**. Host Link will:

1. receive the CP/M file into a temporary Linux working directory;
2. remove normal trailing CP/M `1Ah` padding when the file is clearly ASCII text;
3. open the file in the configured Linux editor;
4. wait for the editor to release the file;
5. compare the edited contents with the downloaded copy;
6. if it changed, stage the new file on CP/M and safely replace the original;
7. restore the original R/O, SYS, and ARC attributes and refresh the CP/M directory.

If the editor closes without changing the file, nothing is sent back to CP/M.

The default editor command is:

```text
subl --wait
```

when Sublime Text is installed. Choose **Editor Settings…** from the same right-click menu to change the command. Host Link appends the temporary filename automatically; alternatively, put `{file}` anywhere in the command where the filename should be substituted.

For GUI editors, configure an option that waits until the edited file is closed. `Ctrl+E` is a shortcut for editing the currently selected CP/M file.

## Build HOST v2.2

The known-good Serial I/O V3 PUT/DIR/GET v2.1 source is retained as:

```text
HOST_SERIALIO_GET.ASM.gz
```

The v2.2 builder applies the tested DELETE/RENAME/ATTR additions and assembles the result:

```sh
python3 build_host_fileops.py
```

It creates:

```text
HOST22.ASM   readable generated v2.2 source
HOST22.COM   CP/M executable
```

Copy `HOST22.COM` to CP/M and run:

```text
A>HOST22
```

The generated source is intentionally readable so the complete current CP/M implementation can be inspected after building.

## HOST v2 protocol

Linux and CP/M exchange CRC-protected 128-byte packets. Command block 0 begins with:

```text
bytes 0-7    "S100HST2"
byte  8      command
byte  9      drive: 0=A ... 15=P, FF=current
byte 10      user: 0..15, FF=current
bytes 11-21  CP/M 8.3 filename where applicable
```

Current commands:

```text
01h  PUT
02h  DIR
03h  GET
04h  DELETE
05h  RENAME
06h  ATTR
```

PUT and GET use 128-byte data packets with CRC-16/XMODEM framing. DIR returns raw CP/M directory entries. Metadata-only commands return normal protocol completion or cancellation on a BDOS error.

## Repository layout

```text
run.sh                    production launcher
launch_targetsim.py       production entry point + targetsim serial discovery
launch_editor.py          CP/M external-editor workflow
launch_fileops.py         CP/M rename/attributes/delete UI layer
launch_listview.py        multi-selection/list-view layer
launch_resizable.py       pane and common UI behavior
launch_dualpane.py        bidirectional GET support layer
s100_hostlink_*.py        base implementation modules

HOST_SERIALIO_GET.ASM.gz  tested Serial I/O V3 v2.1 source baseline
build_host_fileops.py     builds HOST v2.2 from that baseline
build_host.py             self-contained 8080 assembler used by the HOST build
HOST_V22.md               current CP/M-side build and protocol notes
legacy/                   superseded SBC and development material
```

## Important hardware note

Do not use the historical SBC `34h/36h` HOST source on the target system. Port `34h` belongs to the Dual IDE/CF interface in the current IMSAI configuration. The production HOST path uses the Serial I/O V3 DLP-USB interface at `AAh/ACh`.

This same hardware-visible interface is the one the target-system emulator should reproduce, allowing the same `HOST22.COM` to run unchanged on the physical IMSAI and in emulation.

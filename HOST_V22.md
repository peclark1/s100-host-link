# HOST v2.2 — Current CP/M Service

HOST v2.2 is the current CP/M-side companion for S-100 Host Link.

## Hardware interface

This build targets the **S100Computers Serial I/O V3 DLP-USB245R** interface used by the physical IMSAI target system:

```text
AAh  USB handshake/status
     bit 7 = RXE, active low
     bit 6 = TXE, active low

ACh  USB data
```

It does **not** use the historical Z80 SBC USB ports `34h/36h`.

## Build

The tested v2.1 PUT/DIR/GET Serial I/O source is stored as `HOST_SERIALIO_GET.ASM.gz`. Build the current v2.2 source and executable with:

```sh
python3 build_host_fileops.py
```

Outputs:

```text
HOST22.ASM
HOST22.COM
```

The builder uses `build_host.py` to assemble the generated 8080-compatible source.

## Commands

HOST v2.2 retains the working transfer commands and adds CP/M file management:

```text
01h  PUT       Linux -> CP/M
02h  DIR       return CP/M directory entries
03h  GET       CP/M -> Linux
04h  DELETE    delete a CP/M file
05h  RENAME    rename a CP/M file
06h  ATTR      set R/O, SYS and CP/M 3 ARC attributes
```

Rename uses CP/M BDOS Function 23. Attribute changes use BDOS Function 30. R/O and SYS are standard CP/M attributes; ARC is exposed for CP/M 3.

## Production status

HOST v2.2 has been successfully run on the physical IMSAI through the Serial I/O V3 USB interface. Existing PUT/DIR/GET transfers and the newer rename/attribute/delete functions are the current production path.

The Linux application is launched with:

```sh
./run.sh
```

The production UI supports two-pane Linux/CP/M browsing, drive/user selection, multi-file drag/drop in both directions, rename, attributes, and delete.

## Historical source

The old root `HOST.ASM` was an SBC-era implementation using `34h/36h` and did not correspond to the newer Serial I/O binary. It has been archived under `legacy/` to make the hardware lineage explicit.

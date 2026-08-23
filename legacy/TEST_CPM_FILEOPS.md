# CP/M file management test

Feature branch: `feature/cpm-file-management`

This branch adds CP/M-side **Rename**, **Attributes**, and **Delete** to the
production two-pane ListView UI. Existing PUT/DIR/GET and multi-file drag/drop
are left in place.

## 1. Pull the branch

```sh
cd ~/Applications/s100-host-link
git fetch origin
git switch feature/cpm-file-management
git pull
```

## 2. Build the matching CP/M HOST v2.2

The repository's tested GET-capable v2.1 source is
`HOST_SERIALIO_GET.ASM.gz`. Build the file-management version from that source:

```sh
python3 build_host_fileops.py
```

This creates:

- `HOST22.ASM` — readable generated source
- `HOST22.COM` — CP/M executable to test

The builder deliberately does **not** overwrite the known-good `HOST.COM` or
`HOST21.COM` files.

Transfer `HOST22.COM` to CP/M by your usual method and run:

```text
A>HOST22
```

## 3. Start the Linux UI

```sh
./run.sh
```

`run.sh` launches `launch_fileops.py` on this branch.

## 4. Smoke test existing transfers first

Before testing file management, verify no regression:

1. Refresh the CP/M directory.
2. Drag one Linux file to CP/M.
3. Drag it back to Linux.
4. Try a multi-file transfer in both directions.

## 5. Test file management

Right-click a CP/M file. The menu now contains:

- **Rename…** — validates CP/M 8.3 names and refuses a name already present in
  the displayed directory.
- **Attributes…** — changes R/O and SYS; ARC is also exposed for CP/M 3.
- **Delete…** — confirms before deleting. Multi-selected CP/M files can be
  deleted as a batch.

Keyboard shortcuts:

- `F2` — rename the single selected CP/M file
- `Delete` — confirm deletion of the selected CP/M file(s)

The CP/M file list displays attributes beside file size, for example:

```text
TEST.COM        2.0 KiB  ·  R/O, SYS
```

After every successful operation, the CP/M directory refreshes automatically.

## Protocol notes

HOST v2.2 adds metadata-only commands after the existing GET command:

- `04H` DELETE
- `05H` RENAME
- `06H` ATTR

All three use normal CP/M BDOS functions. Rename uses BDOS Function 23 and
attributes use Function 30. CP/M 2.2 formally defines R/O and SYS attributes;
ARC is labeled as a CP/M 3 option in the UI.

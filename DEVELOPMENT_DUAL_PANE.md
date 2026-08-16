# Dual-pane UI development branch

This branch is for testing the new two-pane S-100 Host Link workflow.

## What changed

- Linux directory pane on the left.
- CP/M directory pane on the right.
- CP/M drive/user selectors live with the CP/M pane.
- Select a Linux file and use **Send →**.
- Select a CP/M file and use **← Receive**.
- Drag-and-drop copies files in either direction.
- The two directory panes scroll independently; the full-window nested scrolling from the previous layout is removed.
- Transfer status, progress and the log remain below the panes.

## CP/M protocol baseline

The branch carries the tested HOST v2.1 binaries (`HOST21.COM` and `HOST.COM`) with PUT, DIR and GET support. The GET-capable Serial I/O V3 source is included as `HOST_SERIALIO_GET.ASM.gz` for this development snapshot.

The CP/M interface used by that build is the S100Computers Serial I/O V3 DLP-USB245R path at data port ACh / status port AAh.

## Testing

After switching to this branch, launch normally with:

```sh
./run.sh
```

Suggested smoke test:

1. Run the already-tested HOST21/HOST v2.1 on CP/M.
2. Refresh the CP/M directory.
3. Send one small Linux file using **Send →**.
4. Receive one small CP/M file using **← Receive**.
5. Repeat both operations using drag-and-drop.
6. Check that the Linux and CP/M panes scroll independently and the transfer controls/log remain accessible.

## Development packaging note

For this test branch, the generated v5 Python UI is stored as `s100_hostlink_dualpane.py.gz` and launched by `launch_dualpane.py`. This keeps the branch testable while preserving the original v4.1 module untouched as a reference. Once the UI is accepted, the final merge should normalize the generated v5 source back into the regular Python source tree.

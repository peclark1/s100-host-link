#!/bin/sh
set -eu

APP_ID="com.s100computers.HostLink"
APP_NAME="S-100 Host Link"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DEST_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DEST="$DEST_DIR/$APP_ID.desktop"
OLD_DEST="$DEST_DIR/s100-host-link.desktop"

mkdir -p "$DEST_DIR"

# Preserve the user's existing custom icon when migrating from the old launcher.
ICON=""
for existing in "$DEST" "$OLD_DEST"; do
    if [ -f "$existing" ]; then
        candidate=$(sed -n 's/^Icon=//p' "$existing" | head -n 1 || true)
        if [ -n "$candidate" ]; then
            ICON=$candidate
            break
        fi
    fi
done

# Fall back to a standard icon name if this is a fresh install.
if [ -z "$ICON" ]; then
    ICON="computer"
fi

cat > "$DEST" <<EOF
[Desktop Entry]
Type=Application
Name=$APP_NAME
Comment=Transfer files between Linux and CP/M over the S-100 Host Link
Exec=$SCRIPT_DIR/run.sh
Path=$SCRIPT_DIR
Icon=$ICON
Terminal=false
Categories=Utility;
StartupNotify=true
StartupWMClass=$APP_ID
EOF

chmod 644 "$DEST"

# Remove the obsolete desktop ID. GNOME associates Wayland windows primarily by
# application/desktop ID, so keeping the old differently-named launcher can make
# the running window appear as a second application.
if [ -f "$OLD_DEST" ] && [ "$OLD_DEST" != "$DEST" ]; then
    rm -f "$OLD_DEST"
fi

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$DEST_DIR" >/dev/null 2>&1 || true
fi

printf '%s\n' "Installed $DEST"
printf '%s\n' "Application ID: $APP_ID"
printf '%s\n' "Icon: $ICON"
printf '%s\n' "If the old launcher is pinned, unpin it and pin S-100 Host Link again once."

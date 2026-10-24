#!/bin/bash
# Installs the `room-heater` command into ~/.local/bin, pointing at this folder.
# Run it again if you move the folder.

set -e
HEATER_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$HOME/.local/bin"

if [ "$(uname)" != "Darwin" ]; then
    echo "❌ room-heater only works on macOS"
    exit 1
fi

# clang, swiftc and python3 come with Apple's Command Line Tools
if ! xcode-select -p >/dev/null 2>&1; then
    echo "❌ Xcode Command Line Tools are missing. Install them with:"
    echo "     xcode-select --install"
    echo "   then run ./install.sh again."
    exit 1
fi

mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/room-heater" <<EOF
#!/bin/bash
exec $(printf '%q' "$HEATER_DIR/room-heater") "\$@"
EOF
chmod +x "$BIN_DIR/room-heater" "$HEATER_DIR"/room-heater "$HEATER_DIR"/*.sh
echo "✅ Installed: $BIN_DIR/room-heater → $HEATER_DIR"

case ":$PATH:" in
    *":$BIN_DIR:"*) echo "   Try it: room-heater start" ;;
    *)
        echo "⚠️  $BIN_DIR is not on your PATH. Add it with:"
        echo "     echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc && source ~/.zshrc"
        ;;
esac

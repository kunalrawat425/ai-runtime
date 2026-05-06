#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${AI_RUNTIME_INSTALL_DIR:-$HOME/.local/bin}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "[ai-runtime] installing to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"

# Copy main script
cp "$SCRIPT_DIR/ai_runtime.py" "$INSTALL_DIR/ai_runtime.py"
chmod +x "$INSTALL_DIR/ai_runtime.py"

# Create wrapper
cat > "$INSTALL_DIR/ai-runtime" << 'EOF'
#!/usr/bin/env bash
exec python3 "$(dirname "$0")/ai_runtime.py" "$@"
EOF
chmod +x "$INSTALL_DIR/ai-runtime"

echo "[ai-runtime] installed: $INSTALL_DIR/ai-runtime"

# Check PATH
if ! echo "$PATH" | tr ':' '\n' | grep -qx "$INSTALL_DIR"; then
    echo ""
    echo "Add $INSTALL_DIR to your PATH:"
    echo "  echo 'export PATH=\"$INSTALL_DIR:\$PATH\"' >> ~/.zshrc && source ~/.zshrc"
fi

echo "[ai-runtime] done. try: ai-runtime run 'your task here'"

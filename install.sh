#!/usr/bin/env bash
# install.sh — Install ai-runtime as a Claude Code skill + standalone CLI
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_NAME="ai-runtime"

# ── Claude Code skill install ─────────────────────────────────────────────────

case "$(uname -s)" in
  Darwin*) SKILL_TARGET="$HOME/.claude/skills" ;;
  Linux*)
    SKILL_TARGET="${XDG_CONFIG_HOME:-$HOME/.config}/claude/skills"
    [ -d "$SKILL_TARGET" ] || SKILL_TARGET="$HOME/.claude/skills"
    ;;
  *) echo "Windows: copy skills/ai-runtime/ to %APPDATA%\\Claude\\skills\\" ; ;;
esac

echo "[ai-runtime] installing Claude Code skill → $SKILL_TARGET/$SKILL_NAME"
mkdir -p "$SKILL_TARGET"

DEST="$SKILL_TARGET/$SKILL_NAME"
if [ -d "$DEST" ]; then
  echo "[ai-runtime] updating existing skill at $DEST"
  rm -rf "$DEST"
fi
cp -r "$SCRIPT_DIR/skills/$SKILL_NAME" "$DEST"
echo "[ai-runtime] skill installed ✓"

# ── Standalone CLI install ────────────────────────────────────────────────────

CLI_DIR="${AI_RUNTIME_INSTALL_DIR:-$HOME/.local/bin}"
mkdir -p "$CLI_DIR"

cp "$SCRIPT_DIR/skills/$SKILL_NAME/ai_runtime.py" "$CLI_DIR/ai_runtime.py"
chmod +x "$CLI_DIR/ai_runtime.py"

cat > "$CLI_DIR/ai-runtime" << WRAPPER
#!/usr/bin/env bash
exec python3 "\$(dirname "\$0")/ai_runtime.py" "\$@"
WRAPPER
chmod +x "$CLI_DIR/ai-runtime"

echo "[ai-runtime] CLI installed → $CLI_DIR/ai-runtime ✓"

# ── PATH check ────────────────────────────────────────────────────────────────

if ! echo "$PATH" | tr ':' '\n' | grep -qx "$CLI_DIR"; then
  echo ""
  echo "  Add to PATH:"
  echo "    echo 'export PATH=\"$CLI_DIR:\$PATH\"' >> ~/.zshrc && source ~/.zshrc"
fi

echo ""
echo "Done. Usage:"
echo "  ai-runtime run 'your task here'"
echo "  /ai-runtime run 'your task here'   (in Claude Code)"

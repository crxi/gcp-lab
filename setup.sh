#!/usr/bin/env bash
# Installs `glab` and checks what it needs. --check verifies without changing.
set -u

CHECK=0
[ "${1:-}" = "--check" ] && CHECK=1
HERE="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.local/bin"

ok()   { printf '  ok    %s\n' "$1"; }
bad()  { printf '  no    %s\n' "$1"; FAILED=1; }
FAILED=0

python3 - <<'PY' 2>/dev/null && ok "google-cloud-compute" || bad "google-cloud-compute (pip install google-cloud-compute)"
import google.cloud.compute_v1, google.auth
PY

if command -v gcloud >/dev/null 2>&1; then
  ok "gcloud $(gcloud version 2>/dev/null | head -1)"
else
  bad "gcloud (https://cloud.google.com/sdk/docs/install) -- needed for shell, run, push, pull"
fi

if [ "$CHECK" = 0 ]; then
  mkdir -p "$BIN"
  printf '#!/usr/bin/env bash\nexec python3 %q "$@"\n' "$HERE/glab.py" > "$BIN/glab"
  chmod +x "$BIN/glab" "$HERE/glab.py"
  ok "installed $BIN/glab"
fi

case ":$PATH:" in
  *":$BIN:"*) ok "$BIN is on PATH" ;;
  *) bad "$BIN is not on PATH (add it to your shell profile)" ;;
esac

if python3 -c "import google.auth,sys; google.auth.default()" 2>/dev/null; then
  ok "application-default credentials work"
else
  bad "no credentials (gcloud auth application-default login)"
fi

echo
if [ "$FAILED" = 1 ]; then
  echo "fix the above, then re-run."
  exit 1
fi
echo "ready. next: glab project PROJECT_ID && glab zone asia-southeast1-b"

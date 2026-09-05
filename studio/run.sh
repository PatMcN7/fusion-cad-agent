#!/bin/zsh
# Launch Fusion Studio. Creates the venv on first run.
set -euo pipefail
here="${0:A:h}"
venv="$here/.venv"

if [[ ! -x "$venv/bin/python" ]]; then
  print "  creating venv…"
  python3 -m venv "$venv"
  "$venv/bin/pip" install --quiet --upgrade pip
  "$venv/bin/pip" install --quiet anthropic
fi

# Optional backends -- only installed if asked for, so a Claude-only setup
# stays lean and a missing SDK is reported rather than assumed.
case "${1:-}" in
  --with-openai) "$venv/bin/pip" install --quiet openai; shift ;;
  --with-gemini) "$venv/bin/pip" install --quiet google-genai; shift ;;
  --with-all)    "$venv/bin/pip" install --quiet openai google-genai; shift ;;
esac

exec "$venv/bin/python" "$here/server.py" "$@"

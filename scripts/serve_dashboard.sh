#!/usr/bin/env bash
# Bring up the API and the dashboard together, and print a token to sign in with.
#
# Exists because doing this by hand is three terminals and two easy mistakes. The
# frontend defaults to calling `/api` and relying on Vite's dev proxy, so a
# production build served by anything else must be told where the API is -- and the
# API must be told to accept that origin. Getting either wrong produces a console
# that loads and then fails every request, which looks like a broken dashboard
# rather than a misconfiguration.
#
# Static mode is the default because on this machine the Vite dev server is
# reachable inside WSL and invisible from Windows (see docs/stage-reports/stage-11.md).
# Use --dev once that is fixed: it is faster and has hot reload.
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

PORT=8001
API_PORT=8000
ROLE=auditor
MINUTES=180
MODE=static
BUILD=1

usage() {
    cat <<'EOF'
usage: scripts/serve_dashboard.sh [options]

  --port N        serve the dashboard here          (default 8001)
  --api-port N    serve the API here                (default 8000)
  --role ROLE     viewer | operator | auditor       (default auditor)
  --minutes N     token lifetime                    (default 180)
  --dev           use the Vite dev server instead of a production build
  --no-build      reuse the existing frontend/dist
  -h, --help      this

Ctrl-C stops everything it started.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)     PORT="$2"; shift 2 ;;
        --api-port) API_PORT="$2"; shift 2 ;;
        --role)     ROLE="$2"; shift 2 ;;
        --minutes)  MINUTES="$2"; shift 2 ;;
        --dev)      MODE=dev; shift ;;
        --no-build) BUILD=0; shift ;;
        -h|--help)  usage; exit 0 ;;
        *) echo "unknown option: $1"; usage; exit 2 ;;
    esac
done

[[ "$MODE" == "dev" ]] && PORT=5173

# ------------------------------------------------------------------ preflight

fail() { echo "  $1"; exit 1; }

[[ -d .venv ]] || fail "no .venv -- see PROJECT_CONTEXT section 2"
# shellcheck disable=SC1091
source .venv/bin/activate

command -v node >/dev/null || fail "node is not installed in this distro"
case "$(command -v node)" in
    /mnt/c/*)
        # The Windows node on PATH through WSL's interop. Running npm with it
        # against a Linux checkout is the trap PROJECT_CONTEXT warns about.
        fail "node resolves to the Windows install at $(command -v node).
       Install it inside Ubuntu, or fix PATH so /usr/bin/node comes first." ;;
esac

pg_isready -h localhost -U confidentia >/dev/null 2>&1 \
    || fail "PostgreSQL is not accepting connections -- the API will start but every panel will 503"

for port in "$PORT" "$API_PORT"; do
    if ss -ltn 2>/dev/null | grep -q ":${port} "; then
        fail "port ${port} is already in use"
    fi
done

CHILDREN=()
cleanup() {
    for pid in "${CHILDREN[@]:-}"; do
        kill "$pid" 2>/dev/null
    done
}
trap cleanup EXIT INT TERM

# ------------------------------------------------------------------------ API

ORIGIN="http://localhost:${PORT}"
echo "starting the API on :${API_PORT}"
# The console runs on a different origin from the API in both modes, so the origin
# has to be named. Both spellings: a browser treats localhost and 127.0.0.1 as
# different origins, and people type whichever they remember.
API_CORS_ORIGINS="[\"${ORIGIN}\",\"http://127.0.0.1:${PORT}\"]" \
    python -m confidentia.cli api serve --port "$API_PORT" >/tmp/confidentia-api.log 2>&1 &
CHILDREN+=($!)

for _ in $(seq 1 40); do
    curl -sf -o /dev/null "http://127.0.0.1:${API_PORT}/health" && break
    sleep 0.5
done
if ! curl -sf -o /dev/null "http://127.0.0.1:${API_PORT}/health"; then
    echo "the API did not come up. Last 20 lines:"
    tail -20 /tmp/confidentia-api.log
    exit 1
fi

# ------------------------------------------------------------------- frontend

if [[ "$MODE" == "dev" ]]; then
    echo "starting the Vite dev server on :${PORT}"
    ( cd frontend && API_ORIGIN="http://127.0.0.1:${API_PORT}" npm run dev >/tmp/confidentia-vite.log 2>&1 ) &
    CHILDREN+=($!)
    sleep 4
else
    if [[ "$BUILD" == "1" ]]; then
        echo "building the dashboard against http://localhost:${API_PORT}"
        # Baked in at build time -- Vite inlines it, so this cannot be changed by
        # restarting the server, only by rebuilding.
        ( cd frontend && VITE_API_BASE_URL="http://localhost:${API_PORT}" npm run build ) \
            >/tmp/confidentia-build.log 2>&1 \
            || { echo "the build failed:"; tail -25 /tmp/confidentia-build.log; exit 1; }
    fi
    [[ -f frontend/dist/index.html ]] || fail "frontend/dist is empty -- drop --no-build"

    echo "serving frontend/dist on :${PORT}"
    python3 - "$PORT" >/tmp/confidentia-static.log 2>&1 <<'PY' &
"""A static server that behaves like the nginx config in docker/frontend/.

`python -m http.server` is not good enough for two reasons, both hit during
Stage 11. React Router owns paths like /evaluation, so a reload or a pasted link
asks the *server* for a file that does not exist and gets a 404 -- hence the
fallback to index.html. And a cached index.html names asset hashes the next build
deletes, which renders as a blank page and a 404 in the console -- hence no-store
on the document while the hashed assets stay cacheable.
"""
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class SpaHandler(SimpleHTTPRequestHandler):
    def send_head(self):
        # A path with no dot in its last segment is a route, not a file. Anything
        # with an extension that is missing stays a 404, so a genuinely absent
        # asset is still reported as absent instead of quietly returning HTML.
        target = Path(self.translate_path(self.path))
        if not target.exists() and "." not in target.name:
            self.path = "/index.html"
        return super().send_head()

    def end_headers(self):
        if self.path in ("/", "/index.html"):
            self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def log_message(self, *args):
        pass  # quiet: the API log is the one worth reading


handler = partial(SpaHandler, directory="frontend/dist")
ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), handler).serve_forever()
PY
    CHILDREN+=($!)
    sleep 2
fi

if ! curl -sf -o /dev/null "http://127.0.0.1:${PORT}/"; then
    echo "the dashboard did not come up. Last 20 lines:"
    tail -20 /tmp/confidentia-static.log /tmp/confidentia-vite.log 2>/dev/null
    exit 1
fi

# ---------------------------------------------------------------------- token

TOKEN=$(python -m confidentia.cli api token "$(whoami)" --role "$ROLE" --minutes "$MINUTES")

cat <<EOF

  dashboard   ${ORIGIN}
  API docs    http://localhost:${API_PORT}/docs
  role        ${ROLE}, valid ${MINUTES} minutes

  paste this into the sign-in box:

${TOKEN}

  logs        /tmp/confidentia-api.log
  Ctrl-C to stop everything.

EOF

# If the API exits the dashboard is useless, so this waits on everything and
# returns as soon as any of it dies rather than leaving half a stack running.
wait -n
echo
echo "a process exited; shutting down"

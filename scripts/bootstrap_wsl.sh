#!/usr/bin/env bash
# Bootstrap the CONFIDENTIA development environment inside WSL2 Ubuntu.
#
# Why WSL rather than Windows: Hadoop's Shell class hard-fails on Windows without
# winutils.exe/hadoop.dll, which are only available from unofficial mirrors. Linux
# also matches the container and cloud targets, so there is one runtime to reason
# about instead of two.
#
# Why native processes rather than Docker: the host has 7.4 GB of RAM. Docker
# Desktop's VM would take ~1.5-2 GB before a single service starts. The Compose
# files are still maintained as the portable/cloud deliverable and are validated
# in CI -- they are just not the local development path.
#
# Installs only what Stage 1 needs. Postgres, Kafka and MinIO arrive in the stages
# that first use them, so the disk budget is spent as late as possible.
#
# Idempotent: safe to re-run.

set -euo pipefail

PROJECT_DIR="${CONFIDENTIA_HOME:-$HOME/confidentia}"
DATA_ROOT="${CONFIDENTIA_DATA_ROOT:-$HOME/confidentia-data}"
JDK_VERSION=17

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }

# ---------------------------------------------------------------- system deps
log "Updating apt metadata"
sudo apt-get update -qq

log "Installing JDK ${JDK_VERSION}, Python and build tools"
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    "openjdk-${JDK_VERSION}-jdk-headless" \
    python3 python3-venv python3-dev python3-pip \
    build-essential git curl unzip pkg-config

JAVA_HOME_PATH="$(dirname "$(dirname "$(readlink -f "$(command -v java)")")")"
log "JAVA_HOME resolved to ${JAVA_HOME_PATH}"

# Spark 3.5 supports Java 8/11/17 only. If a newer JDK is also installed and is
# the default, Spark fails with opaque module-access errors rather than a clear
# version message -- so pin it explicitly for this shell and for the venv.
if ! java -version 2>&1 | grep -q "\"${JDK_VERSION}\."; then
    warn "Default java is not ${JDK_VERSION}; pinning via update-alternatives"
    sudo update-alternatives --set java \
        "/usr/lib/jvm/java-${JDK_VERSION}-openjdk-amd64/bin/java"
fi

# ------------------------------------------------------------------- layout
log "Creating data root at ${DATA_ROOT}"
mkdir -p "${DATA_ROOT}"/{lake,checkpoints,spark-warehouse,spark-tmp,keystore,raw,logs}
chmod 700 "${DATA_ROOT}/keystore"   # key material: owner-only

if [[ ! -d "${PROJECT_DIR}" ]]; then
    warn "Project not found at ${PROJECT_DIR}."
    warn "Clone or copy the repository there, then re-run this script."
    exit 1
fi
cd "${PROJECT_DIR}"

# ------------------------------------------------------------------- python
if [[ ! -d .venv ]]; then
    log "Creating virtual environment"
    python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# A venv created by `uv venv` has no pip in it, and `pip install` then fails with
# "command not found" -- which is confusing halfway through a bootstrap. venvs made
# by this script do have pip; this only matters when re-running against one that
# was created another way.
if ! python -m pip --version >/dev/null 2>&1; then
    warn "This venv has no pip (created by uv?). Bootstrapping one with ensurepip."
    python -m ensurepip --upgrade >/dev/null
fi
python -m pip install --upgrade pip --quiet

# Every extra the project actually runs, not just Stage 1's. Installing a subset
# produces an environment where the CLI imports but half its subcommands raise
# ImportError at the point of use -- `confidentia api serve` without [api] being
# the obvious one. The cost is a slower bootstrap, once.
log "Installing Python dependencies (all runtime extras)"
python -m pip install --quiet -e \
    ".[dev,generators,storage,ingestion,cdc,spark,lake,api,observability,stream]"

# Persist JAVA_HOME into the venv so every activation gets a Spark-compatible JDK
# without touching the user's global environment.
ACTIVATE_HOOK=".venv/bin/activate"
if ! grep -q "CONFIDENTIA_JAVA_HOME" "${ACTIVATE_HOOK}"; then
    log "Pinning JAVA_HOME into the venv activation script"
    cat >> "${ACTIVATE_HOOK}" <<EOF

# --- CONFIDENTIA ---
export JAVA_HOME="${JAVA_HOME_PATH}"
export CONFIDENTIA_JAVA_HOME="${JAVA_HOME_PATH}"
export CONFIDENTIA_DATA_ROOT="${DATA_ROOT}"
export PYSPARK_PYTHON="\${VIRTUAL_ENV}/bin/python"
export PYSPARK_DRIVER_PYTHON="\${VIRTUAL_ENV}/bin/python"
EOF
fi

if [[ ! -f .env ]]; then
    log "Creating .env from .env.example"
    cp .env.example .env
    sed -i "s|^CONFIDENTIA_DATA_ROOT=.*|CONFIDENTIA_DATA_ROOT=${DATA_ROOT}|" .env
    python - <<'PY'
import pathlib, secrets, re
env = pathlib.Path(".env")
env.write_text(
    re.sub(r"^AUTH_JWT_SECRET=.*$",
           f"AUTH_JWT_SECRET={secrets.token_urlsafe(48)}",
           env.read_text(), flags=re.M)
)
print("generated a random AUTH_JWT_SECRET")
PY
fi

# --------------------------------------------------------------------- node
# This breaks the "install it in the stage that first needs it" rule above, and the
# reason is the failure mode rather than the size.
#
# A missing Postgres produces a connection error that names Postgres. A missing
# Node produces *the Windows Node*: WSL puts `/mnt/c/Program Files/nodejs` on PATH
# through interop, so `npm install` silently runs a Windows binary against a Linux
# checkout and fails somewhere a long way from the cause. ~50 MB to make that
# impossible is a good trade.
#
# Set SKIP_NODE=1 to leave it out on a machine that will never build the frontend.
NODE_MAJOR=22

node_is_linux() {
    command -v node >/dev/null || return 1
    case "$(command -v node)" in
        /mnt/c/*) return 1 ;;   # the Windows install, reached through interop
        *) return 0 ;;
    esac
}

if [[ "${SKIP_NODE:-0}" == "1" ]]; then
    warn 'SKIP_NODE=1: not installing Node. frontend/ will not build.'
elif node_is_linux && [[ "$(node -v | sed 's/^v\([0-9]*\).*/\1/')" -ge 20 ]]; then
    log "Node already present: $(node -v) at $(command -v node)"
else
    if command -v node >/dev/null && ! node_is_linux; then
        warn "The node on PATH is the Windows install at $(command -v node)."
        warn "Installing a Linux one; /usr/bin will take precedence."
    fi
    # NodeSource rather than apt's own: Ubuntu ships a Node too old for Vite 6,
    # and the resulting error is a syntax error inside a dependency rather than a
    # version message.
    log "Installing Node ${NODE_MAJOR} from NodeSource"
    curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | sudo -E bash - >/dev/null
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nodejs
fi

# Dependencies are deliberately *not* installed here. `npm ci` in frontend/ pulls
# ~150 packages and tens of thousands of files, which is the one part of this
# project genuinely worth deferring until someone builds the dashboard.

# ------------------------------------------------------------------ verify
log "Verifying the toolchain"
export JAVA_HOME="${JAVA_HOME_PATH}"
java -version 2>&1 | head -1
if node_is_linux; then
    echo "node $(node -v) ($(command -v node)), npm $(npm -v)"
fi
python -c "import pyspark, delta; print(f'pyspark {pyspark.__version__}')"
python -c "from confidentia.config.settings import get_settings; s=get_settings(); print(f'settings ok: data_root={s.data_root} tee={s.tee.backend.value} ({s.tee.measurement_label})')"

log "Bootstrap complete."
echo
echo "  source ${PROJECT_DIR}/.venv/bin/activate"
echo "  python spikes/pme/build_kms_jar.py && python spikes/pme/run_spike.py"
echo
echo "  services this script does not install, in the order the stages need them:"
echo "    scripts/install_postgres.sh      metadata store + CDC source"
echo "    scripts/install_kafka.sh         streaming (stage 9)"
echo "    scripts/install_airflow.sh       orchestration (stage 8)"
echo
echo "  the dashboard needs its dependencies fetched once:"
echo "    (cd frontend && npm install) && bash scripts/serve_dashboard.sh"
echo

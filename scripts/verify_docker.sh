#!/usr/bin/env bash
# Verify the container stack: that it builds, and that the core profile serves.
#
# This exists because `compose-validate` asserts far less than its name suggests.
# `docker compose config` checks syntax and interpolation and **nothing else** -- it
# passed for months against a file referencing four Dockerfiles that did not exist.
# A job that green-lights a stack which cannot be built is worse than no job, so:
#
#   1. every `dockerfile:` a service names must exist          (the gap that hid)
#   2. every profile must interpolate and parse
#   3. every image must build
#   4. the core profile must come up and answer
#
# Steps 1 and 2 are seconds and run in CI. Steps 3 and 4 need a Docker daemon and
# several minutes; CI runs them in a separate job.
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

# `demo` is validated and deliberately not brought up: it mounts monitoring
# configuration that Stage 12 has not written yet.
PROFILES=(core batch stream bench demo)
BUILD=1
UP=1
failed=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-build) BUILD=0; shift ;;
        --no-up)    UP=0; shift ;;
        --static)   BUILD=0; UP=0; shift ;;   # what CI's cheap job runs
        -h|--help)  sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "unknown option: $1"; exit 2 ;;
    esac
done

ok()   { printf '  ok    %s\n' "$1"; }
bad()  { printf '  FAIL  %s\n' "$1"; failed=$((failed + 1)); }

# --------------------------------------------------- 1. referenced files exist

echo "referenced build files"
# Parsed out of the compose file rather than listed here: a hard-coded list is a
# list that goes stale the moment a service is added, which is the same failure
# this whole script exists to catch.
while read -r dockerfile; do
    [[ -z "$dockerfile" ]] && continue
    if [[ -f "$dockerfile" ]]; then
        ok "$dockerfile"
    else
        bad "$dockerfile referenced by docker-compose.yml but missing"
    fi
done < <(grep -oE 'dockerfile: [^},]+' docker-compose.yml | awk '{print $2}' | sort -u)

echo
echo "referenced bind mounts"
# A missing host path is not an error to Docker: it silently creates an empty
# directory and mounts that. Prometheus then starts with no configuration and looks
# like a broken container rather than a missing file.
#
# Emptiness counts as missing. `monitoring/prometheus` and `monitoring/grafana`
# exist on the development machine as empty directories left from Stage 1, and git
# does not track empty directories -- so they are present here, absent on a fresh
# clone, and useless in both places. Checking only for existence would pass locally
# and fail in CI, which is the worst of the three outcomes.
#
# Paths whose owning stage has not run yet are reported as `pending` and do not
# fail: they are known gaps with a name attached, not surprises. Anything else
# missing is a failure.
# Empty now that Stage 12 has written the monitoring configuration. Kept rather
# than deleted: the next service added ahead of its configuration should be
# declared here with the stage that owns it, not silently allowed to fail.
declare -A PENDING=()

while read -r path; do
    [[ -z "$path" ]] && continue
    present=0
    if [[ -d "$path" ]]; then
        [[ -n "$(ls -A "$path" 2>/dev/null)" ]] && present=1
    elif [[ -e "$path" ]]; then
        present=1
    fi

    if [[ $present -eq 1 ]]; then
        ok "$path"
    elif [[ -n "${PENDING[$path]:-}" ]]; then
        printf '  pend  %s - not built yet: %s\n' "$path" "${PENDING[$path]}"
    else
        bad "$path is bind-mounted but missing or empty (Docker will mount an empty dir)"
    fi
done < <(grep -oE '^\s+- \./[^:]+' docker-compose.yml | sed 's/^[[:space:]]*- //' | sort -u)

# ------------------------------------------------------- 2. profiles validate

echo
echo "profiles parse"
for profile in "${PROFILES[@]}"; do
    if docker compose --profile "$profile" config --quiet 2>/dev/null; then
        ok "--profile $profile"
    else
        bad "--profile $profile does not validate"
    fi
done

if [[ "$BUILD" == "0" && "$UP" == "0" ]]; then
    echo
    [[ $failed -eq 0 ]] && { echo "static checks passed"; exit 0; }
    echo "FAILED: $failed static check(s)"; exit 1
fi

# -------------------------------------------------------------- 3. images build

if [[ "$BUILD" == "1" ]]; then
    echo
    echo "images build"
    for service in api frontend spark airflow; do
        if docker compose build "$service" >/tmp/confidentia-build-"$service".log 2>&1; then
            size=$(docker images --format '{{.Size}}' "confidentia-${service}:latest" | head -1)
            ok "$service (${size:-unknown})"
        else
            bad "$service - see /tmp/confidentia-build-${service}.log"
            tail -12 /tmp/confidentia-build-"$service".log
        fi
    done
fi

# ------------------------------------------------------- 4. the core profile serves

if [[ "$UP" == "1" ]]; then
    echo
    echo "core profile"

    # Published on offset ports, always. The development machine runs Postgres on
    # 5432, the API on 8000 and Vite on 5173 natively, so verifying against the
    # defaults fails with "address already in use" before a single container
    # starts -- and would otherwise require stopping the developer's database to
    # test the containers, which nobody would do twice.
    export POSTGRES_PORT=15432 API_PUBLISHED_PORT=18000 FRONTEND_PORT=15173
    export MINIO_PORT=19000 MINIO_CONSOLE_PORT=19001
    api_url="http://localhost:${API_PUBLISHED_PORT}"
    ui_url="http://localhost:${FRONTEND_PORT}"
    echo "  (published on ${API_PUBLISHED_PORT}/${FRONTEND_PORT} to avoid the native services)"

    # shellcheck disable=SC2064
    trap "docker compose --profile demo down -v >/dev/null 2>&1" EXIT

    if ! docker compose --profile core up -d >/tmp/confidentia-up.log 2>&1; then
        bad "compose up"
        tail -20 /tmp/confidentia-up.log
    else
        # Postgres has to initialise and the API has to open a pool before either
        # answers; polling beats a fixed sleep that is either flaky or wasteful.
        for _ in $(seq 1 60); do
            curl -sf -o /dev/null "${api_url}/health" && break
            sleep 2
        done

        status() { curl -s -o /dev/null -w '%{http_code}' "$1"; }

        [[ "$(status "${api_url}/health")" == "200" ]] \
            && ok "api /health" || bad "api /health"
        [[ "$(status "${ui_url}/")" == "200" ]] \
            && ok "frontend serves the console" || bad "frontend serves the console"
        # Through nginx, which is what makes the containerised console same-origin.
        [[ "$(status "${ui_url}/api/health")" == "200" ]] \
            && ok "frontend proxies /api to the api service" \
            || bad "frontend proxies /api to the api service"
        # The SPA fallback: a deep link must return the app, not a 404.
        [[ "$(status "${ui_url}/evaluation")" == "200" ]] \
            && ok "SPA history fallback on /evaluation" \
            || bad "SPA history fallback on /evaluation"
        # Unauthenticated reads must still be refused inside a container.
        [[ "$(status "${api_url}/runs")" == "401" ]] \
            && ok "api refuses an unauthenticated request" \
            || bad "api refuses an unauthenticated request"
        # /ready is the one that proves the API reached Postgres, which is the whole
        # point of running them in the same network.
        [[ "$(status "${api_url}/ready")" == "200" ]] \
            && ok "api reaches postgres over the compose network" \
            || bad "api reaches postgres over the compose network"

        for db in ehr_oltp airflow; do
            if docker compose exec -T postgres \
                psql -U confidentia -d "$db" -c 'SELECT 1' >/dev/null 2>&1; then
                ok "database $db created by the init script"
            else
                bad "database $db missing"
            fi
        done
    fi

    # ----------------------------------------------------- the monitoring slice
    #
    # Prometheus and Grafana, brought up alongside core rather than as the whole
    # `demo` profile: demo also starts Spark, Airflow and Kafka, which is 6 GB of
    # containers on a 3.9 GB VM. What is being verified here is that the scrape
    # path works and that provisioning took -- neither needs a scheduler.
    echo
    echo "monitoring"
    export PROMETHEUS_PORT=19090 GRAFANA_PORT=13000
    prom="http://localhost:${PROMETHEUS_PORT}"
    graf="http://localhost:${GRAFANA_PORT}"

    if ! docker compose up -d prometheus grafana >>/tmp/confidentia-up.log 2>&1; then
        bad "prometheus/grafana up"
    else
        for _ in $(seq 1 40); do
            curl -sf -o /dev/null "${prom}/-/healthy" && break
            sleep 3
        done

        [[ "$(status "${prom}/-/healthy")" == "200" ]] \
            && ok "prometheus healthy" || bad "prometheus healthy"

        rules=$(curl -sf "${prom}/api/v1/rules" | grep -o '"name"' | wc -l)
        [[ "$rules" -gt 8 ]] \
            && ok "alert rules loaded ($((rules - 2)) rules)" \
            || bad "alert rules did not load"

        # The scrape has to succeed, not merely be configured: a target stuck in
        # `down` is the state this whole stage exists to make visible.
        #
        # Waited on with the same precise check that decides pass/fail, not a grep.
        # The first version matched `confidentia-api.*"health":"up"` against the
        # targets payload -- which is a single line containing both targets, so the
        # *prometheus* job being up satisfied it. The loop exited after 12 seconds,
        # before the first 30s scrape had happened, and the check then failed for
        # real. A readiness probe that is weaker than the assertion it guards is
        # worse than no probe.
        api_target_up() {
            curl -sf "${prom}/api/v1/targets" 2>/dev/null | python3 -c '
import json, sys
try:
    targets = json.load(sys.stdin)["data"]["activeTargets"]
except Exception:
    sys.exit(1)
api = [t for t in targets if t["labels"].get("job") == "confidentia-api"]
sys.exit(0 if api and api[0]["health"] == "up" else 1)
' 2>/dev/null
        }

        # Generously longer than one 30s scrape interval plus startup.
        for _ in $(seq 1 30); do
            api_target_up && break
            sleep 4
        done

        if api_target_up; then
            ok "prometheus is scraping the API"
        else
            bad "prometheus is not scraping the API"
            curl -sf "${prom}/api/v1/targets" \
                | python3 -c 'import json,sys; [print("      ", t["labels"].get("job"), t["health"], t.get("lastError","")) for t in json.load(sys.stdin)["data"]["activeTargets"]]' \
                2>/dev/null || true
        fi

        # The metric has to arrive in the time series database, not merely be
        # served by the API. Retried for the same reason as the target check: the
        # first sample lands one scrape interval after the target comes up.
        metric_present() {
            curl -sf --get "${prom}/api/v1/query" \
                --data-urlencode 'query=sum(confidentia_http_requests_total)' 2>/dev/null \
                | grep -q '"value"'
        }
        for _ in $(seq 1 12); do
            metric_present && break
            sleep 4
        done
        if metric_present; then
            ok "platform metrics queryable through prometheus"
        else
            bad "platform metrics not queryable through prometheus"
        fi

        # Polled, not probed once. This was a bare status check run immediately after
        # the prometheus assertions, which passed only because Grafana used to be ready
        # by then. 13.3.0 needs about a minute to run its migrations and load its
        # plugins, so the single probe became a guaranteed failure -- and it reported
        # "grafana is broken" when the truth was "grafana is slow". Same reasoning as
        # the api and prometheus waits above: polling beats a fixed sleep that is
        # either flaky or wasteful.
        for _ in $(seq 1 30); do
            [[ "$(status "${graf}/api/health")" == "200" ]] && break
            sleep 4
        done

        [[ "$(status "${graf}/api/health")" == "200" ]] \
            && ok "grafana healthy" || bad "grafana healthy"

        curl -sf -u admin:"${GRAFANA_PASSWORD:-admin}" "${graf}/api/datasources" \
            | grep -q 'confidentia-prometheus' \
            && ok "grafana datasource provisioned" || bad "grafana datasource provisioned"

        curl -sf -u admin:"${GRAFANA_PASSWORD:-admin}" "${graf}/api/search?query=CONFIDENTIA" \
            | grep -q 'confidentia-platform' \
            && ok "grafana dashboard provisioned" || bad "grafana dashboard provisioned"
    fi
fi

echo
if [[ $failed -eq 0 ]]; then
    echo "the container stack verified: builds, comes up, and serves"
    exit 0
fi
echo "FAILED: $failed check(s)"
exit 1

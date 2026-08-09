#!/usr/bin/env bash
set -uo pipefail
cd "$HOME/confidentia"
source .venv/bin/activate

ruff check src tests spikes --fix --output-format=concise >/dev/null 2>&1
ruff check src tests spikes --output-format=concise && echo "lint clean"

echo
echo "=== why is Silver 163 MB for ~260k rows? ==="
for layer in bronze silver; do
  for d in "$HOME/confidentia-data/lake/$layer"/*/*; do
    [ -d "$d" ] || continue
    files=$(find "$d" -name '*.parquet' | wc -l)
    parts=$(find "$d" -mindepth 1 -maxdepth 1 -type d -name '*=*' | wc -l)
    size=$(du -sm "$d" | cut -f1)
    printf "  %-8s %-28s %5s MB  %4s files  %4s partitions\n" \
      "$layer" "$(basename "$(dirname "$d")")/$(basename "$d")" "$size" "$files" "$parts"
  done
done

echo
echo "=== Delta history retained (full-refresh keeps old versions) ==="
python - <<'PY' 2>/dev/null
from delta.tables import DeltaTable
from confidentia.pipelines.silver.writer import silver_path
from confidentia.spark.session import session_for_tenant
with session_for_tenant("healthcare") as (spark, _k):
    dt = DeltaTable.forPath(spark, str(silver_path("healthcare", "encounter")))
    print(f"  encounter table versions: {dt.history().count()}")
PY

git add -A
git -c core.fileMode=false commit -q -m "Stage 5: remove unused noqa directives" 2>&1 | tail -2
echo "HEAD=$(git rev-parse --short HEAD)"

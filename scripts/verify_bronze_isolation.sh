#!/usr/bin/env bash
# Prove column-level key separation on the Bronze table (contribution C1).
#
# A byte search is not sufficient evidence on its own: Parquet dictionary-encodes
# and Snappy-compresses, so a value's absence from the raw bytes can be a
# compression artefact rather than encryption. The definitive test is to hold only
# some of the keys and observe which columns become unreadable.
#
# Each arm runs in its own OS process because Parquet's KeyToolkit caches unwrapped
# keys per JVM; a second attempt in the same process would read from cache and
# falsely pass.
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
# shellcheck disable=SC1091
source .venv/bin/activate

run_arm() {
    python - "$@" <<'PY'
import sys
from confidentia.config.settings import Sensitivity, get_settings
from confidentia.crypto.broker import request_keys
from confidentia.crypto.keys import KeyId
from confidentia.pipelines.bronze.writer import bronze_path
from confidentia.spark.session import CRYPTO_FACTORY, KMS_CLIENT, build_session

withheld = sys.argv[1] if len(sys.argv) > 1 else ""
column = sys.argv[2]

from confidentia.crypto.keys import KeyHierarchy

keys = request_keys("healthcare")
material = dict(keys.material)
if withheld:
    # Simulate a principal who was never granted this sensitivity class.
    material.pop(str(KeyId(tenant="healthcare", sensitivity=withheld)), None)

spark = build_session("confidentia-isolation-probe")
conf = spark.sparkContext._jsc.hadoopConfiguration()
conf.set("parquet.crypto.factory.class", CRYPTO_FACTORY)
conf.set("parquet.encryption.kms.client.class", KMS_CLIENT)
conf.set("parquet.encryption.key.list", KeyHierarchy().key_list_property(material))
conf.set("parquet.encryption.footer.key", str(KeyId.footer("healthcare")))

held = "all keys" if not withheld else f"all keys EXCEPT {withheld}"
try:
    df = spark.read.format("delta").load(str(bronze_path("healthcare", "encounter")))
    rows = df.select(column).limit(3).collect()
    print(f"  [{held:26s}] read {column:24s} -> OK      {[r[0] for r in rows][:1]}")
except Exception as exc:
    first = str(exc).splitlines()
    detail = next((ln.strip() for ln in first if "KeyAccessDenied" in ln), first[0])[:80]
    print(f"  [{held:26s}] read {column:24s} -> DENIED  {detail}")
finally:
    spark.stop()
PY
}

echo "Column-level key separation on healthcare/encounter"
echo
echo "-- holding every key: everything must be readable --"
run_arm "" primary_diagnosis_code 2>/dev/null
run_arm "" facility_id 2>/dev/null

echo
echo "-- withholding the PHI key: PHI columns must fail, public columns must not --"
run_arm phi primary_diagnosis_code 2>/dev/null
run_arm phi facility_id 2>/dev/null
run_arm phi encounter_type 2>/dev/null

echo
echo "-- withholding the INTERNAL key --"
run_arm internal encounter_id 2>/dev/null
run_arm internal facility_id 2>/dev/null

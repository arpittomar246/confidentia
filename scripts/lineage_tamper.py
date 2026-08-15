"""Tamper with a real Bronze table, then put it back. Used by verify_lineage.sh.

A chain that is never tested against tampering is a hash column with extra steps,
so this edits an **actual encrypted Delta table** rather than a fixture. Two things
make that safe enough to do on the working lake:

* the target is small and fully regenerable (`bronze write --full-refresh`);
* the edit is undone with Delta time travel, which does not delete the original
  files -- `RESTORE` writes a new commit pointing back at them.

The tampering write sets the column-key mapping first. Writing the tampered rows in
plaintext would "work", and would leave unencrypted files in the lake for as long as
the demonstration lasted -- which is precisely the failure ADR 0007 exists to
prevent, and not something to do casually in a script about integrity.

    python scripts/lineage_tamper.py version  <tenant> <dataset>
    python scripts/lineage_tamper.py tamper   <tenant> <dataset> <column>
    python scripts/lineage_tamper.py restore  <tenant> <dataset> <version>
"""

from __future__ import annotations

import sys

from delta.tables import DeltaTable
from pyspark.sql import functions as F

from confidentia.catalog.contracts import get_contract
from confidentia.config.settings import Layer, Tenant, get_settings
from confidentia.spark.session import key_ids_by_sensitivity, session_for_tenant, set_column_keys

USAGE = """usage:
    python scripts/lineage_tamper.py version  <tenant> <dataset>
    python scripts/lineage_tamper.py tamper   <tenant> <dataset> <column>
    python scripts/lineage_tamper.py restore  <tenant> <dataset> <version>"""


def parse_args() -> tuple[str, str, str, str | None]:
    """Check the arguments before Spark is started, not after.

    A missing argument used to surface as an IndexError from inside the session
    block: forty seconds of JVM startup to be told nothing useful, and a traceback
    where three lines of usage belong. This script is part of the documented
    recovery when verify_lineage.sh is interrupted -- the moment someone is least
    inclined to read a stack trace to find out they typed the wrong thing.
    """
    if len(sys.argv) < 4:
        raise SystemExit(USAGE)

    action, tenant, dataset = sys.argv[1:4]
    if action not in {"version", "tamper", "restore"}:
        raise SystemExit(f"unknown action {action!r}\n\n{USAGE}")

    extra = sys.argv[4] if len(sys.argv) > 4 else None
    if action == "tamper" and extra is None:
        raise SystemExit(f"tamper needs a <column>\n\n{USAGE}")
    if action == "restore":
        if extra is None:
            raise SystemExit(f"restore needs a <version>\n\n{USAGE}")
        if not extra.isdigit():
            raise SystemExit(f"version must be a number, got {extra!r}\n\n{USAGE}")

    return action, tenant, dataset, extra


action, tenant, dataset, extra = parse_args()
path = get_settings().layer_path(Layer.BRONZE, Tenant(tenant), dataset)

with session_for_tenant(tenant, app_name=f"confidentia-tamper-{tenant}") as (spark, _keys):
    table = DeltaTable.forPath(spark, str(path))

    if action == "version":
        print(table.history(1).collect()[0]["version"])
        raise SystemExit(0)

    if action == "restore":
        table.restoreToVersion(int(extra))
        print(f"restored {tenant}/{dataset} to version {extra}")
        raise SystemExit(0)

    column = extra
    contract = get_contract(tenant, dataset)
    set_column_keys(spark, contract.column_keys_property(key_ids_by_sensitivity(tenant)))

    key = contract.business_key[0]
    victim = spark.read.format("delta").load(str(path)).select(key).limit(1).collect()[0][key]

    # One row, one column. A subtle edit is the honest test: a truncated table
    # would be caught by the row count alone, which proves nothing about the root.
    table.update(
        condition=F.col(key) == F.lit(victim),
        set={column: F.lit("TAMPERED")},
    )
    print(f"tampered {tenant}/{dataset}: {key}={victim}, {column} -> 'TAMPERED'")

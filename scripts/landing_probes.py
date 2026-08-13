"""Probes that re-prove the landing zone's encryption guarantee from the bytes.

ADR 0022 claims four things about the landing zone. Each has a probe here, run in
its own OS process by ``scripts/verify_landing.sh``, because Parquet's KeyToolkit
caches unwrapped keys statically and an in-process retry would read from that cache
and falsely pass — the same trap that made the first Bronze verification report a
pass on plaintext.

    magic      every landing file carries the PARE footer magic
    bytes      no SSN- or email-shaped string survives in the raw bytes
    keyless    a read without key material is refused, not silently empty
    spark      Spark reads what pandas wrote, which is what lets Bronze promote

The last is the one worth being suspicious of. Landing is written by pyarrow and
read by Spark's Java crypto factory, and the two agree only because both implement
the same PKMT1 key material format with a byte-compatible key wrapping envelope. A
drift on either side would be invisible until a promotion failed, so it is checked
rather than assumed.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from confidentia.config.settings import get_settings

SSN = re.compile(rb"[0-9]{3}-[0-9]{2}-[0-9]{4}")
EMAIL = re.compile(rb"[a-zA-Z0-9._%+-]{2,}@[a-zA-Z0-9.-]{2,}\.[a-zA-Z]{2,}")


def landing_root() -> Path:
    return get_settings().lake_root / "landing"


def parts() -> list[Path]:
    root = landing_root()
    return sorted(p for p in root.rglob("*.parquet") if p.is_file())


def probe_magic() -> int:
    from confidentia.crypto.landing import footer_magic, plaintext_parts

    files = parts()
    if not files:
        print("no landing files to check")
        return 1
    offenders = plaintext_parts(landing_root())
    for path in offenders:
        print(f"  PLAINTEXT {footer_magic(path)!r}  {path}")
    print(f"{len(files) - len(offenders)}/{len(files)} files carry PARE")
    return 1 if offenders else 0


def probe_bytes() -> int:
    """Grep the raw bytes for the patterns that exposed the hole in the first place.

    Deliberately pattern matching rather than a decryption attempt: this is the
    check an attacker with filesystem access would run, and it is the one that
    found 19 SSNs in a landing patient file when the zone was plaintext.
    """
    hits = 0
    for path in parts():
        raw = path.read_bytes()
        ssn = len(SSN.findall(raw))
        email = len(EMAIL.findall(raw))
        if ssn or email:
            hits += ssn + email
            print(f"  LEAK {path.name}: {ssn} SSN-shaped, {email} email-shaped")
    print(f"{len(parts())} files scanned, {hits} identifier-shaped strings found")
    return 1 if hits else 0


def probe_keyless() -> int:
    """A read with no key material must fail loudly."""
    import pyarrow.parquet as pq

    files = parts()
    if not files:
        print("no landing files to check")
        return 1
    target = max(files, key=lambda p: p.stat().st_size)
    try:
        table = pq.read_table(target)
    except Exception as exc:  # noqa: BLE001 - any refusal is the correct outcome
        print(f"  refused as expected: {type(exc).__name__}: {str(exc)[:160]}")
        return 0
    print(f"  READ SUCCEEDED WITHOUT KEYS: {target} -> {table.num_rows} rows")
    return 1


def probe_spark() -> int:
    """Spark must read what pyarrow wrote — the claim Bronze promotion rests on."""
    from confidentia.spark.session import session_for_tenant

    root = landing_root()
    tenant = "healthcare"
    candidates = sorted((root / tenant).glob("*/batch=*"))
    if not candidates:
        print(f"no landing batches under {root / tenant}")
        return 1
    batch = max(candidates, key=lambda p: sum(f.stat().st_size for f in p.glob("*.parquet")))

    with session_for_tenant(tenant, app_name="verify-landing") as (spark, keys):
        frame = spark.read.parquet(str(batch))
        rows = frame.count()
        print(f"  {batch.parent.name}/{batch.name}: {rows:,} rows, {len(frame.columns)} columns")
        # Reading the count alone would pass on an encrypted footer with unreadable
        # column data, since row counts live in metadata. Materialise a protected
        # column to prove the column keys unwrapped too.
        sample = frame.limit(1).collect()
        if not sample:
            print("  table is empty; nothing proved")
            return 1
        print(f"  key ids released: {', '.join(keys.key_ids)}")
    return 0


PROBES = {
    "magic": probe_magic,
    "bytes": probe_bytes,
    "keyless": probe_keyless,
    "spark": probe_spark,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("probe", choices=sorted(PROBES))
    args = parser.parse_args()
    return PROBES[args.probe]()


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python
"""Every defect the generator injects must have a rule that could catch it.

Not *does* catch it — `unit_inconsistency` has a rule and still finds only a
quarter of what is injected, and that is a legitimate result about a weak rule.
This checks the weaker, load-bearing property: that a rule for that column and
that defect family **exists at all**.

Where it does not, the injected defect is a permanent false negative. Recall then
measures rule *coverage* rather than rule *quality*, and the two are
indistinguishable in the number. Stage 7b spent most of its findings on exactly
this: five family/field pairs with no rule, reported as recall 0.000 alongside
families where the rule ran and genuinely missed.

Run in CI. Importing pyspark is not needed — `build_rules` only constructs
predicates lazily — so this is fast enough to run on every push.
"""

from __future__ import annotations

import sys

from confidentia.catalog.contracts import get_contract
from confidentia.generators import financial as fin
from confidentia.generators import healthcare as hc
from confidentia.generators.defects import DefectType
from confidentia.quality.rules import build_rules
from confidentia.quality.runner import REFERENTIAL_RELATIONSHIPS

# Families whose rule is added by the runner at evaluation time rather than by
# `build_rules`, because neither can be decided from a single row.
RUNTIME_FAMILIES = {"duplicate_record", "referential_orphan"}


def covered_pairs(tenant: str, dataset: str) -> set[tuple[str, str]]:
    """(family, field) pairs a quality run would actually evaluate."""
    contract = get_contract(tenant, dataset)
    pairs = {(r.family, r.field) for r in build_rules(contract, "bronze") if r.field}

    # The uniqueness rule, added per run over a window.
    pairs.add(("duplicate_record", contract.business_key[0]))
    # The referential rules, added per run from the declared relationships.
    for foreign_key, _parent, _parent_key in REFERENTIAL_RELATIONSHIPS.get(dataset, []):
        pairs.add(("referential_orphan", foreign_key))
    return pairs


def main() -> int:
    gaps: list[str] = []
    checked = 0

    for plan in [*hc.defect_plans(), *fin.defect_plans()]:
        covered = covered_pairs(plan.tenant, plan.dataset)
        for role in plan.fields:
            for kind in role.kinds:
                if kind is DefectType.TYPE_DRIFT:
                    # Not expressible in Parquet, so never injected there.
                    continue
                checked += 1
                if (kind.value, role.name) not in covered:
                    gaps.append(
                        f"  {plan.tenant}/{plan.dataset}.{role.name} "
                        f"is corrupted with {kind.value} but no rule covers it"
                    )

        # Temporal pairs name the end column, which is what the rule fires on.
        for _start, end in plan.temporal_pairs:
            checked += 1
            if ("temporal_inconsistency", end) not in covered:
                gaps.append(
                    f"  {plan.tenant}/{plan.dataset}.{end} is corrupted with "
                    "temporal_inconsistency but no rule covers it"
                )

    if gaps:
        print(f"plan/rule coverage FAILED ({len(gaps)} of {checked} pairs uncovered)")
        print("\n".join(sorted(gaps)))
        print()
        print("Either add a rule for that column and family, or stop injecting the")
        print("defect there. An injected defect with no rule is a permanent false")
        print("negative that makes the quality layer look worse than it is.")
        return 1

    print(f"plan/rule coverage holds ({checked} injected family/field pairs, all covered).")
    print(f"Runtime-added families: {', '.join(sorted(RUNTIME_FAMILIES))}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

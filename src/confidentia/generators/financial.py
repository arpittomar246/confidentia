"""Financial tenant generators — the streaming side of the platform.

The important modelling decision here is that **transactions carry no fraud
label**. A payment system does not know at authorisation time whether a
transaction is fraudulent; it finds out days or weeks later when a chargeback
arrives. Putting an ``is_fraud`` column in the raw stream would be a data leak
that makes every downstream analysis trivially and unrealistically easy.

So fraud is a *latent* process:

* a small, deterministic subset of transactions is generated with fraud-like
  characteristics (unusual merchant category, card-not-present, foreign
  geography, atypical amount, night-time clustering);
* nothing in the transaction record says so;
* ``chargeback`` events arrive 1-45 days later confirming a subset of them, plus a
  few disputes on legitimate transactions ("friendly fraud");
* the true labels go into the ground-truth manifest, never into the data.

This produces a genuine late-arriving-data problem — a chargeback for a
transaction whose daily aggregate was closed a week ago — which is exactly what
the streaming watermark and the Gold restatement logic have to cope with.

Fraudulent transaction indices are chosen from a single seeded draw over the whole
population, so the transaction generator and the chargeback generator can each
compute the same set independently without sharing state or materialising the
stream.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache

import numpy as np
import pandas as pd

from confidentia.generators import reference as ref
from confidentia.generators.base import (
    Chunk,
    GeneratorConfig,
    Tenant,
    apply_late_arrival,
    derive_rng,
    diurnal_offsets,
    to_timestamps,
    zipf_choice,
)
from confidentia.generators.defects import DefectPlan, DefectType, FieldRole, injector_for

# Card fraud is rare. Realistically rare, which matters: a 20% fraud rate would
# make the class balance trivial and any detection result meaningless.
FRAUD_RATE = 0.0021
# Not every fraudulent transaction is disputed -- small ones often go unnoticed.
FRAUD_CHARGEBACK_PROBABILITY = 0.82
# Legitimate transactions occasionally get disputed too.
FRIENDLY_FRAUD_RATE = 0.00035


def account_id(index: np.ndarray | int) -> np.ndarray | str:
    if isinstance(index, int):
        return f"ACC-{index:010d}"
    return np.char.add("ACC-", np.char.zfill(index.astype(str), 10))


def merchant_id(index: np.ndarray | int) -> np.ndarray | str:
    if isinstance(index, int):
        return f"MER-{index:08d}"
    return np.char.add("MER-", np.char.zfill(index.astype(str), 8))


def transaction_id(index: np.ndarray) -> np.ndarray:
    return np.char.add("TXN-", np.char.zfill(index.astype(str), 12))


@dataclass(frozen=True)
class FinancialScale:
    transactions: int
    accounts: int
    merchants: int

    @classmethod
    def for_rows(cls, transactions: int) -> FinancialScale:
        # ~55 transactions per account over six months is a plausible card usage
        # rate and keeps the account dimension in memory at any fact volume.
        accounts = max(200, min(transactions // 55, 3_000_000))
        merchants = max(50, min(accounts // 8, 250_000))
        return cls(transactions=transactions, accounts=accounts, merchants=merchants)


@lru_cache(maxsize=8)
def fraud_indices(seed: int, total: int) -> np.ndarray:
    """Deterministic set of fraudulent transaction indices.

    Computed from one draw over the entire population so that any generator can
    reproduce it without coordination. Sorted, so membership tests within a chunk
    are a ``searchsorted`` slice rather than a scan.
    """
    rng = derive_rng(seed, "fraud_population")
    count = max(1, int(total * FRAUD_RATE))
    return np.sort(rng.choice(total, size=count, replace=False))


def _chunk_fraud_mask(indices: np.ndarray, start: int, size: int) -> np.ndarray:
    left, right = np.searchsorted(indices, [start, start + size])
    mask = np.zeros(size, dtype=bool)
    mask[indices[left:right] - start] = True
    return mask


# ------------------------------------------------------------------ dimensions


def generate_merchants(config: GeneratorConfig, scale: FinancialScale) -> pd.DataFrame:
    rng = derive_rng(config.seed, "merchant")
    n = scale.merchants
    mcc_idx = rng.integers(0, len(ref.MCC_CODES), size=n)
    cities = ref.CITIES
    weights = np.array([c[5] for c in cities], dtype=float)
    weights /= weights.sum()
    city_idx = rng.choice(len(cities), size=n, p=weights)
    is_foreign = rng.uniform(size=n) < 0.07

    return pd.DataFrame(
        {
            "merchant_id": merchant_id(np.arange(n)),
            "merchant_name": [
                f"{ref.LAST_NAMES[i % len(ref.LAST_NAMES)]} "
                f"{ref.MCC_CODES[m][1].split()[0]} {i % 97}"
                for i, m in enumerate(mcc_idx)
            ],
            "mcc": [ref.MCC_CODES[i][0] for i in mcc_idx],
            "mcc_category": [ref.MCC_CODES[i][1] for i in mcc_idx],
            "country": np.where(
                is_foreign, rng.choice(ref.FOREIGN_COUNTRIES, size=n), "US"
            ),
            "city": [cities[i][0] for i in city_idx],
            "state": [cities[i][1] for i in city_idx],
            "risk_tier": rng.choice(["low", "medium", "high"], size=n, p=[0.72, 0.23, 0.05]),
            "onboarded_at": pd.Timestamp(config.start_date, tz="UTC")
            - pd.to_timedelta(rng.integers(30, 2200, size=n), unit="D"),
        }
    )


def generate_accounts(config: GeneratorConfig, scale: FinancialScale) -> Iterator[Chunk]:
    """Account dimension. Mutable (status, risk score, address) — the financial
    tenant's SCD2 subject, and PII-bearing."""
    plan = _account_defect_plan()
    injector = injector_for(plan, config)

    types, type_weights = ref.weighted(ref.ACCOUNT_TYPES)
    statuses, status_weights = ref.weighted(ref.ACCOUNT_STATUSES)

    emitted = 0
    chunk_index = 0
    while emitted < scale.accounts:
        size = min(config.chunk_rows, scale.accounts - emitted)
        rng = derive_rng(config.seed, "account", chunk_index)
        offset = np.arange(emitted, emitted + size)

        first = rng.choice(ref.FIRST_NAMES, size=size)
        last = rng.choice(ref.LAST_NAMES, size=size)
        cities = ref.CITIES
        weights = np.array([c[5] for c in cities], dtype=float)
        weights /= weights.sum()
        city_idx = rng.choice(len(cities), size=size, p=weights)

        frame = pd.DataFrame(
            {
                "account_id": account_id(offset),
                # Synthetic PAN-like value in a non-issued IIN range, last four only.
                "account_number_last4": [f"{rng.integers(0, 10000):04d}" for _ in range(size)],
                "customer_name": [f"{f} {ln}" for f, ln in zip(first, last, strict=True)],
                "email": [
                    f"{f.lower()}.{ln.lower()}{i}@{rng.choice(ref.EMAIL_DOMAINS)}"
                    for f, ln, i in zip(first, last, offset, strict=True)
                ],
                "phone": [
                    f"({rng.integers(200, 990):03d}) {rng.integers(200, 990):03d}-{rng.integers(1000, 10000):04d}"
                    for _ in range(size)
                ],
                "account_type": rng.choice(types, size=size, p=type_weights),
                "status": rng.choice(statuses, size=size, p=status_weights),
                # Beta distribution: most customers low risk, a thin high-risk tail.
                "risk_score": np.round(rng.beta(2.0, 8.0, size=size) * 100, 1),
                "credit_limit": np.round(
                    rng.choice([1000, 2500, 5000, 10000, 25000], size=size,
                               p=[0.22, 0.28, 0.28, 0.16, 0.06]), 2
                ),
                "home_city": [cities[i][0] for i in city_idx],
                "home_state": [cities[i][1] for i in city_idx],
                "home_country": "US",
                "opened_at": pd.Timestamp(config.start_date, tz="UTC")
                - pd.to_timedelta(rng.integers(60, 5400, size=size), unit="D"),
                "updated_at": pd.Timestamp(config.start_date, tz="UTC"),
            }
        )

        frame, manifest = injector.inject(frame, rng, chunk_index)
        yield Chunk("account", Tenant.FINANCIAL, chunk_index, frame, manifest)

        emitted += size
        chunk_index += 1


# ---------------------------------------------------------------- transactions


def generate_transactions(config: GeneratorConfig, scale: FinancialScale) -> Iterator[Chunk]:
    plan = _transaction_defect_plan()
    injector = injector_for(plan, config)

    channels, channel_weights = ref.weighted(ref.CHANNELS)
    currencies, currency_weights = ref.weighted(ref.CURRENCIES)
    mcc_median = np.array([m[2] for m in ref.MCC_CODES], dtype=float)
    mcc_sigma = np.array([m[3] for m in ref.MCC_CODES], dtype=float)
    # Categories over-represented in card fraud.
    high_risk_mcc = np.array(
        [i for i, m in enumerate(ref.MCC_CODES) if m[0] in {"7995", "5732", "4511", "5722", "6011"}]
    )

    fraud_set = fraud_indices(config.seed, scale.transactions)

    emitted = 0
    chunk_index = 0
    while emitted < scale.transactions:
        size = min(config.chunk_rows, scale.transactions - emitted)
        rng = derive_rng(config.seed, "transaction", chunk_index)
        offset = np.arange(emitted, emitted + size)
        is_fraud = _chunk_fraud_mask(fraud_set, emitted, size)

        account_index = zipf_choice(rng, scale.accounts, size, a=1.2)
        merchant_index = zipf_choice(rng, scale.merchants, size, a=1.4)
        mcc_idx = rng.integers(0, len(ref.MCC_CODES), size=size)
        # Fraud clusters in high-risk merchant categories.
        if is_fraud.any() and high_risk_mcc.size:
            mcc_idx[is_fraud] = rng.choice(high_risk_mcc, size=int(is_fraud.sum()))

        event_seconds = diurnal_offsets(rng, size, days=config.days)
        # Fraud skews to the small hours, when the cardholder is asleep.
        if is_fraud.any():
            night = rng.uniform(0, 5.5, size=int(is_fraud.sum())) * 3600.0
            day_index = np.floor(event_seconds[is_fraud] / 86400.0)
            event_seconds[is_fraud] = day_index * 86400.0 + night
        event_ts = to_timestamps(event_seconds, config.start_date)

        amount = np.round(
            rng.lognormal(mean=np.log(mcc_median[mcc_idx]), sigma=mcc_sigma[mcc_idx], size=size), 2
        )
        # Fraud amounts sit high in the category's distribution but not absurdly so;
        # an obvious outlier would make detection trivial.
        if is_fraud.any():
            amount[is_fraud] = np.round(amount[is_fraud] * rng.uniform(1.8, 4.5, size=int(is_fraud.sum())), 2)
        amount = np.clip(amount, 0.5, 50_000.0)

        channel = rng.choice(channels, size=size, p=channel_weights)
        if is_fraud.any():
            channel[is_fraud] = rng.choice(
                ["ecommerce", "card_present"], size=int(is_fraud.sum()), p=[0.86, 0.14]
            )

        cities = ref.CITIES
        city_idx = rng.integers(0, len(cities), size=size)
        country = np.full(size, "US", dtype=object)
        if is_fraud.any():
            foreign = rng.uniform(size=int(is_fraud.sum())) < 0.42
            fraud_positions = np.flatnonzero(is_fraud)
            country[fraud_positions[foreign]] = rng.choice(
                ref.FOREIGN_COUNTRIES, size=int(foreign.sum())
            )

        ingest_seconds, is_late = apply_late_arrival(
            rng,
            event_seconds,
            late_rate=config.late_arrival_rate,
            max_late_hours=config.max_late_hours,
        )

        frame = pd.DataFrame(
            {
                "transaction_id": transaction_id(offset),
                "account_id": account_id(account_index),
                "merchant_id": merchant_id(merchant_index),
                "mcc": [ref.MCC_CODES[i][0] for i in mcc_idx],
                "amount": amount,
                "currency": rng.choice(currencies, size=size, p=currency_weights),
                "channel": channel,
                "entry_mode": rng.choice(
                    ["chip", "contactless", "manual", "token"], size=size,
                    p=[0.42, 0.34, 0.09, 0.15]
                ),
                "auth_response": rng.choice(
                    ["approved", "declined", "referred"], size=size, p=[0.938, 0.055, 0.007]
                ),
                "terminal_country": country,
                "terminal_city": [cities[i][0] for i in city_idx],
                "device_id": [f"DEV-{rng.integers(0, 10**9):09d}" for _ in range(size)],
                "ip_address": [
                    f"{rng.integers(1, 224)}.{rng.integers(0, 256)}."
                    f"{rng.integers(0, 256)}.{rng.integers(1, 255)}"
                    for _ in range(size)
                ],
                "latitude": np.round([cities[i][3] for i in city_idx], 4),
                "longitude": np.round([cities[i][4] for i in city_idx], 4),
                "event_ts": event_ts,
                "ingest_ts": to_timestamps(ingest_seconds, config.start_date),
                "is_late_arrival": is_late,
                "source_system": "CARD-AUTH",
            }
        )

        frame, manifest = injector.inject(frame, rng, chunk_index)

        # The fraud labels are ground truth for evaluation only. They are appended
        # to the manifest, never to the frame -- the pipeline must not see them.
        if is_fraud.any():
            labels = pd.DataFrame(
                {
                    "dataset": "transaction",
                    "tenant": Tenant.FINANCIAL.value,
                    "chunk_index": chunk_index,
                    "record_key": transaction_id(offset[is_fraud]),
                    "defect_type": "latent_fraud",
                    "severity": "label",
                    "field": "",
                    "original_value": "",
                    "corrupted_value": "",
                }
            )
            manifest = pd.concat([manifest, labels], ignore_index=True)

        yield Chunk("transaction", Tenant.FINANCIAL, chunk_index, frame, manifest)

        emitted += size
        chunk_index += 1


def generate_chargebacks(config: GeneratorConfig, scale: FinancialScale) -> Iterator[Chunk]:
    """Chargebacks — the delayed, out-of-band confirmation of fraud.

    Arrives 1-45 days after the original transaction. This is deliberately brutal
    for the serving layer: a chargeback landing today can invalidate a daily
    aggregate closed six weeks ago, which forces Gold to support restatement
    rather than assuming append-only facts.
    """
    fraud_set = fraud_indices(config.seed, scale.transactions)
    rng = derive_rng(config.seed, "chargeback")

    confirmed = fraud_set[rng.uniform(size=fraud_set.size) < FRAUD_CHARGEBACK_PROBABILITY]
    friendly_count = max(0, int(scale.transactions * FRIENDLY_FRAUD_RATE))
    friendly = rng.choice(scale.transactions, size=friendly_count, replace=False)

    source = np.sort(np.concatenate([confirmed, friendly]))
    is_true_fraud = np.isin(source, fraud_set)

    emitted = 0
    chunk_index = 0
    while emitted < source.size:
        size = min(config.chunk_rows, source.size - emitted)
        window = slice(emitted, emitted + size)
        chunk_rng = derive_rng(config.seed, "chargeback", chunk_index)

        # The original transaction time is not recoverable without regenerating the
        # stream, so the dispute date is anchored to the window and the pipeline
        # must join on transaction_id to recover the original event time. That join
        # is precisely what makes this a late-arriving-data problem.
        filed_seconds = chunk_rng.uniform(
            86400.0, (config.days + 45) * 86400.0, size=size
        )

        frame = pd.DataFrame(
            {
                "chargeback_id": [f"CB-{emitted + i:011d}" for i in range(size)],
                "transaction_id": transaction_id(source[window]),
                "reason_code": chunk_rng.choice(ref.CHARGEBACK_REASONS, size=size),
                "disputed_amount": np.round(chunk_rng.uniform(5.0, 4000.0, size=size), 2),
                "filed_ts": to_timestamps(np.sort(filed_seconds), config.start_date),
                "status": chunk_rng.choice(
                    ["opened", "represented", "resolved_merchant", "resolved_issuer"],
                    size=size, p=[0.28, 0.19, 0.21, 0.32],
                ),
                "is_confirmed_fraud": is_true_fraud[window],
                "source_system": "DISPUTES",
            }
        )
        yield Chunk("chargeback", Tenant.FINANCIAL, chunk_index, frame)

        emitted += size
        chunk_index += 1


# --------------------------------------------------------------- defect plans


def _account_defect_plan() -> DefectPlan:
    return DefectPlan(
        dataset="account",
        tenant=Tenant.FINANCIAL.value,
        key_field="account_id",
        fields=(
            # No CASE_INCONSISTENCY: the casing convention is for categorical
            # columns Silver lower-cases, not for personal names.
            FieldRole("customer_name", (DefectType.NULL_VIOLATION, DefectType.WHITESPACE_PADDING,
                                        DefectType.ENCODING_CORRUPTION)),
            FieldRole("email", (DefectType.NULL_VIOLATION, DefectType.FORMAT_VIOLATION)),
            # Nullable in the contract, so a missing phone is not a defect.
            FieldRole("phone", (DefectType.FORMAT_VIOLATION,)),
            FieldRole("status", (DefectType.CASE_INCONSISTENCY,)),
            FieldRole("risk_score", (DefectType.RANGE_VIOLATION, DefectType.TYPE_DRIFT)),
            FieldRole("credit_limit", (DefectType.RANGE_VIOLATION,
                                       DefectType.UNIT_INCONSISTENCY)),
            # A state code is upper-case by convention; upper-casing it was a
            # no-op the manifest nonetheless recorded as a defect.
            FieldRole("home_state", (DefectType.WHITESPACE_PADDING,)),
        ),
    )


def _transaction_defect_plan() -> DefectPlan:
    return DefectPlan(
        dataset="transaction",
        tenant=Tenant.FINANCIAL.value,
        key_field="transaction_id",
        fields=(
            FieldRole("account_id", (DefectType.REFERENTIAL_ORPHAN,)),
            FieldRole("merchant_id", (DefectType.REFERENTIAL_ORPHAN,)),
            FieldRole("amount", (DefectType.RANGE_VIOLATION, DefectType.UNIT_INCONSISTENCY,
                                 DefectType.NULL_VIOLATION, DefectType.TYPE_DRIFT)),
            # ISO 4217 codes are upper-case, so upper-casing `currency` changed
            # nothing at all -- 400 of the 600 case defects Stage 7b recorded on
            # this dataset were byte-identical to the original.
            FieldRole("currency", (DefectType.NULL_VIOLATION,)),
            FieldRole("channel", (DefectType.CASE_INCONSISTENCY,)),
            FieldRole("mcc", (DefectType.FORMAT_VIOLATION, DefectType.NULL_VIOLATION)),
            # Nullable in the contract: card-present transactions have no IP.
            FieldRole("ip_address", (DefectType.FORMAT_VIOLATION,)),
            # Same as `currency`: an ISO country code is already upper-case.
            FieldRole("terminal_country", (DefectType.WHITESPACE_PADDING,)),
        ),
    )


def defect_plans() -> list[DefectPlan]:
    """Every financial defect plan. See healthcare.defect_plans."""
    return [_account_defect_plan(), _transaction_defect_plan()]

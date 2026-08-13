"""The experiment harness.

Every number this project puts in a report comes through here, and the harness
exists to stop three specific ways of producing a wrong one.

**A single run is not a measurement.** The Stage 1 spike reported PME writing
*faster* than plaintext, which is impossible and was JVM warm-up leaking across
arms. So every experiment warms up and repeats, reports p50 and p90 rather than a
mean, and records the spread — a result whose max is triple its median is a result
that needs explaining before it is quoted.

**Arm order matters more than it should.** On a cold JVM the first arm pays for
class loading the others reuse. Arms are therefore interleaved across repeats
rather than run in blocks, so a warm-up artefact hits every arm equally instead of
whichever went first.

**A number without its environment is not reproducible.** The machine, the Spark
configuration and the TEE backend are recorded with every result, because a figure
six months from now that nobody can situate is a figure nobody can argue with.
"""

from __future__ import annotations

import json
import platform
import statistics
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from confidentia.common.logging import get_logger
from confidentia.config.settings import Settings, get_settings

logger = get_logger(__name__)

# Wall-clock, not CPU time. What matters is how long the pipeline takes, and Spark
# spends much of it waiting on IO that CPU time would not show.
Timer = Callable[[], "dict[str, Any] | None"]


@dataclass
class Sample:
    """One execution of one arm."""

    seconds: float
    rows: int = 0
    bytes_: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ArmResult:
    experiment: str
    arm: str
    samples: list[Sample]
    notes: str = ""

    @property
    def seconds(self) -> list[float]:
        return [s.seconds for s in self.samples]

    @property
    def p50(self) -> float:
        return statistics.median(self.seconds) if self.samples else 0.0

    @property
    def p90(self) -> float:
        if not self.samples:
            return 0.0
        ordered = sorted(self.seconds)
        # With three repeats this is the maximum, and saying so is more honest than
        # implying a percentile the sample size cannot support.
        index = min(len(ordered) - 1, round(0.9 * (len(ordered) - 1)))
        return ordered[index]

    @property
    def rows(self) -> int:
        return max((s.rows for s in self.samples), default=0)

    @property
    def bytes_(self) -> int:
        return max((s.bytes_ for s in self.samples), default=0)

    @property
    def rows_per_second(self) -> float:
        return self.rows / self.p50 if self.p50 > 0 and self.rows else 0.0

    @property
    def dispersion(self) -> float:
        """max/min. Above about 2 on a quiet machine means something is wrong."""
        values = self.seconds
        return (max(values) / min(values)) if values and min(values) > 0 else 1.0


def environment(settings: Settings | None = None) -> dict[str, Any]:
    """What was true of the machine when the numbers were taken."""
    settings = settings or get_settings()
    import os

    try:
        with open("/proc/meminfo") as meminfo:
            total_kb = int(
                next(line.split()[1] for line in meminfo if line.startswith("MemTotal"))
            )
        memory_mb = total_kb // 1024
    except Exception:  # noqa: BLE001 - environment capture must never fail a run
        memory_mb = 0

    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "memory_mb": memory_mb,
        "spark_master": settings.spark.master,
        "spark_driver_memory": settings.spark.driver_memory,
        "shuffle_partitions": settings.spark.shuffle_partitions,
        "target_file_size_mb": settings.spark.target_file_size_mb,
        "tee_backend": settings.tee.backend.value,
        # False under `sim`. Any claim about enclave cost derived from these
        # timings is MODELLED, and this is the field that says why.
        "tee_hardware_rooted": settings.tee.is_measured,
        "column_encryption": settings.crypto.column_encryption,
    }


def run_arms(
    experiment: str,
    # Mapping, not dict: dict is invariant in its value type, so a caller passing
    # `dict[str, Callable[[], dict]]` -- which every experiment does, because its
    # arms always return detail -- would not type-check against the Timer alias.
    arms: Mapping[str, Timer],
    *,
    warmup: int = 1,
    repeats: int = 3,
    interleave: bool = True,
) -> list[ArmResult]:
    """Run every arm, warmed up and repeated.

    ``interleave`` runs one repeat of each arm in turn rather than all repeats of
    one arm before the next. On a cold JVM the first arm pays for class loading
    that the others then reuse — which is exactly how the Stage 1 spike concluded
    that encryption made writes faster.
    """
    results = {arm: ArmResult(experiment=experiment, arm=arm, samples=[]) for arm in arms}

    for arm, fn in arms.items():
        for i in range(warmup):
            logger.info("benchmark_warmup", experiment=experiment, arm=arm, iteration=i + 1)
            _execute(fn)

    order: Sequence[tuple[int, str]]
    if interleave:
        order = [(r, arm) for r in range(repeats) for arm in arms]
    else:
        order = [(r, arm) for arm in arms for r in range(repeats)]

    for repeat, arm in order:
        logger.info("benchmark_run", experiment=experiment, arm=arm, repeat=repeat + 1)
        sample = _execute(arms[arm])
        results[arm].samples.append(sample)

    for result in results.values():
        if result.dispersion > 2.0:
            # Reported, never silently smoothed. A spread this wide usually means
            # something else was running on the machine, and the honest response is
            # to say so next to the number rather than take the minimum.
            logger.warning(
                "benchmark_dispersion_high",
                experiment=experiment,
                arm=result.arm,
                ratio=round(result.dispersion, 2),
                seconds=[round(s, 2) for s in result.seconds],
            )
            result.notes = (
                f"max/min = {result.dispersion:.1f} across {len(result.samples)} repeats; "
                "treat the median as indicative rather than precise"
            )

    return list(results.values())


def _execute(fn: Timer) -> Sample:
    started = time.perf_counter()
    detail = fn() or {}
    elapsed = time.perf_counter() - started
    return Sample(
        seconds=elapsed,
        rows=int(detail.get("rows", 0)),
        bytes_=int(detail.get("bytes", 0)),
        extra={k: v for k, v in detail.items() if k not in ("rows", "bytes")},
    )


def record(
    results: Sequence[ArmResult],
    *,
    metric: str = "duration_seconds",
    unit: str = "s",
    label: str = "MEASURED",
    settings: Settings | None = None,
    notes: str = "",
) -> None:
    """Persist to ``platform.benchmark_result``."""
    from confidentia.store import db

    env = json.dumps(environment(settings))
    for result in results:
        db.execute(
            """
            INSERT INTO platform.benchmark_result
                (experiment, arm, metric, value, unit, label, rows_processed,
                 bytes_processed, repeats, p50, p90, minimum, maximum,
                 environment, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            """,
            (
                result.experiment,
                result.arm,
                metric,
                result.p50,
                unit,
                label,
                result.rows or None,
                result.bytes_ or None,
                len(result.samples),
                result.p50,
                result.p90,
                min(result.seconds) if result.samples else None,
                max(result.seconds) if result.samples else None,
                env,
                (notes + (" " if notes and result.notes else "") + result.notes) or None,
            ),
        )
    logger.info("benchmark_recorded", experiment=results[0].experiment if results else "", arms=len(results))


def table(results: Sequence[ArmResult], baseline: str | None = None) -> str:
    """A fixed-width table for the terminal and for pasting into a report."""
    if not results:
        return "no results"

    base = next((r for r in results if r.arm == baseline), None)
    lines = [
        f"{'arm':22s} {'p50':>9s} {'p90':>9s} {'rows/s':>11s} {'vs base':>9s}  notes",
        "-" * 78,
    ]
    for result in results:
        relative = (
            f"x{result.p50 / base.p50:.3f}" if base and base.p50 > 0 and base is not result else "—"
        )
        lines.append(
            f"{result.arm:22s} {result.p50:>8.2f}s {result.p90:>8.2f}s "
            f"{result.rows_per_second:>11,.0f} {relative:>9s}  {result.notes}"
        )
    return "\n".join(lines)

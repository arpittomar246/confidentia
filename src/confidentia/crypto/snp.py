"""AMD SEV-SNP attestation on an Azure confidential VM.

===============================================================================
THIS MODULE HAS NEVER RUN ON CONFIDENTIAL COMPUTING HARDWARE.
===============================================================================

Every other measurement in this project is labelled MEASURED or MODELLED, and the
labelling is enforced rather than promised. This module needs a third label, so it
declares one: :data:`VALIDATED_ON_HARDWARE` is ``False``, it is stamped into every
piece of evidence this backend produces as the ``snp_implementation_validated``
claim, it is written into the key-release audit trail through those claims, and a
unit test asserts it is ``False`` so that flipping it is a deliberate act with a
diff attached.

It was written from AMD's SEV-SNP ABI specification, the Linux ``sev-guest`` uapi
header, Azure's HCL report layout and the Microsoft Azure Attestation REST
contract. It was not written from a machine that could execute it. **Structure
offsets, ioctl numbers and the MAA request shape are the parts most likely to be
wrong**, and they are wrong in a way no test on this developer's laptop can detect,
because the failure is "the kernel returns a different struct than I parsed".

What that means in practice, and it is worth being blunt: if this returns a
measurement, that measurement may be the right 48 bytes read from the wrong offset
of a real hardware report. It would look entirely plausible and be entirely wrong.
Do not put a number from this module into the dissertation until it has run on an
Azure confidential VM and the launch measurement it reports has been cross-checked
against the one in the MAA token, which :func:`attest_via_maa` does automatically
and which is the single most valuable check here.

**What is *not* provisional:** the failure behaviour. Every path that cannot obtain
a genuine hardware report raises. Nothing here falls back to a self-measured value,
because a fallback would let a misconfigured deployment claim hardware rooting it
does not have — the one failure this project could not survive.

References the code was written against, so a reviewer can check the constants
rather than trust them:

* AMD, *SEV Secure Nested Paging Firmware ABI Specification*, table for
  ``ATTESTATION_REPORT`` — field offsets in :class:`AttestationReport`.
* Linux ``include/uapi/linux/sev-guest.h`` — ``SNP_GET_REPORT`` and
  ``struct snp_guest_request_ioctl``.
* Azure confidential VM guest attestation — the ``HCLA`` report envelope in the
  vTPM NV index, which wraps the SNP report together with runtime data.
* Microsoft Azure Attestation REST API, ``POST /attest/SevSnpVm``.
"""

from __future__ import annotations

import base64
import fcntl
import json
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from confidentia.common.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# The marker. See the module docstring.
#
# Flip this to True only after: (1) this code has produced a report on an Azure
# confidential VM, (2) MAA accepted that report and returned a token, and (3) the
# launch measurement parsed here matched `x-ms-sevsnpvm-launchmeasurement` in that
# token. Step (3) is what catches a struct offset that is plausible and wrong.
VALIDATED_ON_HARDWARE = False

# ---------------------------------------------------------------------------
# SEV-SNP attestation report, per the AMD ABI specification.

REPORT_SIZE = 1184
"""Bytes in an ATTESTATION_REPORT structure."""

MEASUREMENT_OFFSET = 0x90
"""Offset of LAUNCH_MEASUREMENT. 48 bytes of SHA-384 over the launch digest."""

MEASUREMENT_SIZE = 48

REPORT_DATA_OFFSET = 0x50
"""Offset of REPORT_DATA: 64 guest-supplied bytes, echoed by the firmware."""

REPORT_DATA_SIZE = 64

# Linux sev-guest uapi. SNP_GET_REPORT = _IOWR('S', 0x0, struct
# snp_guest_request_ioctl). _IOWR encodes direction, size, type and number; the
# size is that of the ioctl struct below, not of the report.
_IOC_WRITE, _IOC_READ = 1, 2
SNP_GUEST_REQ_IOCTL_SIZE = 32  # msg_version u8 + pad, req_data u64, resp_data u64, fw_err u64
SEV_GUEST_DEVICE = Path("/dev/sev-guest")

# Azure wraps the SNP report in an HCL report inside a vTPM NV index. The envelope
# starts with this signature; the SNP report follows the 32-byte header.
HCL_SIGNATURE = b"HCLA"
HCL_HEADER_SIZE = 0x20
AZURE_VTPM_NV_INDEX = "0x01400001"


def _ioc(direction: int, type_char: str, number: int, size: int) -> int:
    """Recreate Linux's _IOC macro. Sizes and shifts are the asm-generic layout."""
    return (
        (direction << 30) | (size << 16) | (ord(type_char) << 8) | number
    )


SNP_GET_REPORT = _ioc(_IOC_WRITE | _IOC_READ, "S", 0x0, SNP_GUEST_REQ_IOCTL_SIZE)


class SnpError(RuntimeError):
    """No genuine hardware report could be obtained. Never falls back."""


@dataclass(frozen=True)
class AttestationReport:
    """The fields of an SNP report this project uses.

    Deliberately partial. Parsing all forty-odd fields would be more code with more
    offsets to get wrong, and the platform needs exactly two: the launch
    measurement, which is what policy is written against, and the report data,
    which is how the nonce is bound to the report.
    """

    raw: bytes
    launch_measurement: str
    report_data: bytes

    @classmethod
    def parse(cls, raw: bytes) -> AttestationReport:
        if len(raw) < REPORT_SIZE:
            raise SnpError(
                f"attestation report is {len(raw)} bytes, expected at least {REPORT_SIZE}. "
                "Either the ioctl returned something other than an ATTESTATION_REPORT, "
                "or the layout assumed here is wrong."
            )
        measurement = raw[MEASUREMENT_OFFSET : MEASUREMENT_OFFSET + MEASUREMENT_SIZE]
        report_data = raw[REPORT_DATA_OFFSET : REPORT_DATA_OFFSET + REPORT_DATA_SIZE]

        if measurement == bytes(MEASUREMENT_SIZE):
            # An all-zero launch measurement is not a valid report. It is, however,
            # exactly what reading the wrong offset of a mostly-zero struct
            # produces, which is why this is checked rather than returned.
            raise SnpError(
                f"launch measurement at offset {MEASUREMENT_OFFSET:#x} is all zeroes. "
                "This is what a wrong struct offset looks like, not a valid report."
            )

        return cls(
            raw=raw,
            launch_measurement=measurement.hex(),
            report_data=report_data,
        )

    def binds_nonce(self, nonce_bytes: bytes) -> bool:
        """Did the firmware echo the challenge this report was requested with?

        The whole value of the nonce is that a report cannot be replayed. Checking
        it here rather than trusting the request means a firmware or driver that
        ignored REPORT_DATA is caught instead of silently producing replayable
        evidence.
        """
        expected = nonce_bytes.ljust(REPORT_DATA_SIZE, b"\x00")[:REPORT_DATA_SIZE]
        return self.report_data == expected


# ---------------------------------------------------------------------------
# Obtaining the report. Two sources, tried in order.


def report_via_sev_guest(report_data: bytes) -> bytes:
    """Ask the PSP for a report through ``/dev/sev-guest``.

    The direct path: the guest hands 64 bytes of report data to the firmware and
    receives a signed report binding them. Requires the ``sev-guest`` driver and,
    on most distributions, membership of a group that can open the device.
    """
    if not SEV_GUEST_DEVICE.exists():
        raise SnpError(
            f"{SEV_GUEST_DEVICE} does not exist. This host is not an SEV-SNP guest, "
            "or the sev-guest driver is not loaded."
        )

    import ctypes

    padded = report_data.ljust(REPORT_DATA_SIZE, b"\x00")[:REPORT_DATA_SIZE]

    # The kernel writes into these through pointers held in the ioctl struct, so
    # they are ctypes buffers bound to locals rather than bytearrays whose address
    # is taken and discarded. A temporary whose only reference is inside a struct
    # of integers is a use-after-free the interpreter cannot see, and it would
    # present as intermittent garbage in the report rather than as a crash.
    #
    # struct snp_report_req { __u8 user_data[64]; __u32 vmpl; __u8 rsvd[28]; }
    request = ctypes.create_string_buffer(struct.pack("<64sI28s", padded, 0, bytes(28)), 96)
    # struct snp_report_resp { __u8 data[4000]; }
    response = ctypes.create_string_buffer(4000)

    # struct snp_guest_request_ioctl { __u8 msg_version; __u64 req_data;
    #                                  __u64 resp_data; __u64 fw_err; }
    # Natural alignment pads msg_version out to 8 bytes.
    ioctl_struct = bytearray(
        struct.pack(
            "<BxxxxxxxQQQ",
            1,
            ctypes.addressof(request),
            ctypes.addressof(response),
            0,
        )
    )

    try:
        with SEV_GUEST_DEVICE.open("rb") as device:
            fcntl.ioctl(device.fileno(), SNP_GET_REPORT, ioctl_struct, True)
    except OSError as exc:
        raise SnpError(
            f"SNP_GET_REPORT ioctl on {SEV_GUEST_DEVICE} failed: {exc}. "
            "The ioctl number or request struct may not match this kernel's "
            "sev-guest uapi -- see the module docstring."
        ) from exc

    _, _, _, firmware_error = struct.unpack("<BxxxxxxxQQQ", bytes(ioctl_struct))
    if firmware_error:
        raise SnpError(f"SEV firmware returned error {firmware_error:#x}")

    # Keep both buffers alive until after the ioctl and the read below. Without
    # this the compiler-visible last use is above, and CPython is free to collect
    # them while the kernel still holds their addresses.
    raw = bytes(response.raw[0x20 : 0x20 + REPORT_SIZE])
    del request
    # struct snp_report_resp is a 32-byte header followed by the report.
    return raw


def report_via_vtpm() -> tuple[bytes, bytes]:
    """Read Azure's HCL report from the vTPM NV index.

    Returns ``(snp_report, runtime_data)``. This is the source Azure's own guest
    attestation library uses, and unlike ``/dev/sev-guest`` it carries the runtime
    data MAA wants alongside the report — but the nonce cannot be chosen, because
    the report was generated at boot. Freshness for this path therefore comes from
    the surrounding evidence's nonce and timestamp, not from REPORT_DATA.

    Requires ``tpm2-tools``.
    """
    try:
        blob = subprocess.run(
            ["tpm2_nvread", "-C", "o", AZURE_VTPM_NV_INDEX],
            capture_output=True,
            check=True,
            timeout=30,
        ).stdout
    except FileNotFoundError as exc:
        raise SnpError("tpm2_nvread not found; install tpm2-tools") from exc
    except subprocess.CalledProcessError as exc:
        raise SnpError(
            f"reading vTPM NV index {AZURE_VTPM_NV_INDEX} failed: "
            f"{exc.stderr.decode(errors='replace')[:200]}"
        ) from exc

    if not blob.startswith(HCL_SIGNATURE):
        raise SnpError(
            f"vTPM NV index does not hold an HCL report (signature "
            f"{blob[:4]!r}, expected {HCL_SIGNATURE!r})"
        )

    report = blob[HCL_HEADER_SIZE : HCL_HEADER_SIZE + REPORT_SIZE]
    runtime_data = blob[HCL_HEADER_SIZE + REPORT_SIZE :]
    return report, runtime_data


def obtain_report(report_data: bytes) -> tuple[AttestationReport, bytes]:
    """The hardware report, from whichever source works.

    ``/dev/sev-guest`` first because it binds the nonce; the vTPM second because it
    is what Azure's own tooling uses and works where the device node is not exposed
    to the workload. If neither yields a report, this raises — there is no third
    option and there is deliberately no fallback.
    """
    errors: list[str] = []

    try:
        raw = report_via_sev_guest(report_data)
        report = AttestationReport.parse(raw)
        if not report.binds_nonce(report_data):
            raise SnpError(
                "the firmware did not echo the challenge in REPORT_DATA; this "
                "report cannot be bound to the current nonce"
            )
        logger.info("snp_report_obtained", source="sev-guest", nonce_bound=True)
        return report, b""
    except SnpError as exc:
        errors.append(f"sev-guest: {exc}")

    try:
        raw, runtime_data = report_via_vtpm()
        report = AttestationReport.parse(raw)
        # Not nonce-bound: the vTPM report was produced at boot. Said out loud in
        # the log so an operator reading it knows which freshness guarantee applies.
        logger.warning(
            "snp_report_obtained",
            source="vtpm",
            nonce_bound=False,
            detail="boot-time report; freshness comes from the evidence timestamp",
        )
        return report, runtime_data
    except SnpError as exc:
        errors.append(f"vtpm: {exc}")

    raise SnpError(
        "no SEV-SNP attestation report could be obtained. Tried: "
        + "; ".join(errors)
    )


# ---------------------------------------------------------------------------
# Microsoft Azure Attestation.


def attest_via_maa(
    attestation_uri: str,
    report: AttestationReport,
    runtime_data: bytes,
    *,
    timeout_seconds: float = 30.0,
) -> tuple[str, dict[str, Any]]:
    """Exchange a report for an MAA token, and check MAA agrees with our parsing.

    Returns ``(token, claims)``. The claims are read from the token without
    verifying its signature here — :mod:`confidentia.crypto.attestation` verifies it
    against MAA's JWKS, and doing it in one place keeps the trust decision in one
    place.

    **The cross-check is the point.** MAA parses the same report independently and
    reports the launch measurement in ``x-ms-sevsnpvm-launchmeasurement``. If that
    disagrees with what :class:`AttestationReport` parsed, the offsets in this
    module are wrong — which is exactly the failure a laptop cannot detect, so it is
    checked here every time rather than once during bring-up.
    """
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - depends on the install extras
        # httpx lives in the `ingestion` extra. crypto is a core package, so it is
        # imported lazily rather than at module scope: a minimal install can hold
        # keys and verify evidence without being able to reach MAA.
        raise SnpError(
            "httpx is required to reach Microsoft Azure Attestation and is not "
            "installed. It ships in the 'ingestion' extra: pip install -e '.[ingestion]'"
        ) from exc

    endpoint = f"{attestation_uri.rstrip('/')}/attest/SevSnpVm?api-version=2022-08-01"
    payload: dict[str, Any] = {"report": _b64url(report.raw)}
    if runtime_data:
        payload["runtimeData"] = {"data": _b64url(runtime_data), "dataType": "JSON"}

    try:
        response = httpx.post(endpoint, json=payload, timeout=timeout_seconds)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise SnpError(f"MAA rejected or could not be reached at {endpoint}: {exc}") from exc

    token = response.json().get("token")
    if not token:
        raise SnpError("MAA returned no token")

    claims = _unverified_claims(token)

    reported = str(claims.get("x-ms-sevsnpvm-launchmeasurement", "")).lower()
    if reported and reported != report.launch_measurement.lower():
        raise SnpError(
            "MAA and this module disagree about the launch measurement: MAA says "
            f"{reported[:16]}..., this module parsed "
            f"{report.launch_measurement[:16]}... from offset "
            f"{MEASUREMENT_OFFSET:#x}. Trust MAA and fix the offset."
        )

    logger.info(
        "maa_token_obtained",
        attestation_type=claims.get("x-ms-attestation-type"),
        compliance=claims.get("x-ms-compliance-status"),
        measurement_agrees=bool(reported),
    )
    return token, claims


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unverified_claims(token: str) -> dict[str, Any]:
    """Claims from a JWT without checking its signature.

    Used only to read the launch measurement for the cross-check above. The
    signature is verified in :mod:`confidentia.crypto.attestation` against MAA's
    JWKS before any of these claims is trusted for a policy decision.
    """
    try:
        _, payload, _ = token.split(".")
    except ValueError as exc:
        raise SnpError("MAA token is not a well-formed JWT") from exc
    padded = payload + "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))

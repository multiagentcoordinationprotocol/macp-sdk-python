"""RFC-MACP-0013 Appendix A decision receipts (``macp-receipt/1``).

A receipt is a portable record of one binding Commitment that a third party
can check offline. Two properties are reported separately (A.1):

- **integrity**: the recorded ``commitment`` has the recorded
  ``commitment_hash`` (and, when ``commitment_wire_b64`` is present, the wire
  passes §6.1 verification and decodes to ``commitment``). Needs no keys.
- **authenticity**: some attestation over the receipt body is valid for a
  key the verifier trusts. Only ``jws`` attestations with ``alg: EdDSA`` are
  implemented; ``vendor`` attestations are always ``unsupported``.

Receipts are plain dicts with the schema's snake_case keys
(``schemas/json/macp-decision-receipt.schema.json`` in the spec repo), so a
receipt read with `json.load` can be passed straight to `verify_receipt`.

Ed25519 needs the optional ``cryptography`` package
(``pip install 'macp-sdk-python[receipts]'``). It is imported only when a
signature is made or checked: building an unsigned receipt, and verifying
integrity, work without it.

Kept in lockstep with macp-sdk-typescript's ``receipt`` module: the same
outcomes for the same input, including malformed receipts.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, NotRequired, TypedDict, cast

from macp.v1 import core_pb2

from .commitment_hash import _escape_json_string, commitment_hash, verify_commitment_hash
from .errors import MacpSdkError

#: The only receipt format this module issues and verifies.
RECEIPT_FORMAT = "macp-receipt/1"


class SupersedesRef(TypedDict):
    session_id: str
    commitment_hash: str


class ReceiptCommitment(TypedDict):
    """RFC-MACP-0013 §3 projection of a ``CommitmentPayload``, as recorded in a receipt."""

    commitment_id: str
    action: str
    authority_scope: str
    reason: str
    mode_version: str
    policy_version: str
    configuration_version: str
    outcome_positive: bool
    supersedes: NotRequired[SupersedesRef]


class SemanticsRev(TypedDict):
    implementation: str
    rev: int


class TranscriptDigest(TypedDict):
    digest_alg: str
    digest: str
    message_count: NotRequired[int]


class JwsAttestation(TypedDict):
    type: Literal["jws"]
    #: Compact JWS with a detached payload: ``<protected header>..<signature>``.
    jws: str


class VendorAttestation(TypedDict):
    type: Literal["vendor"]
    format: str
    binds: Literal["commitment_hash", "receipt", "other"]
    value: str


ReceiptAttestation = JwsAttestation | VendorAttestation


class DecisionReceipt(TypedDict):
    """A ``macp-receipt/1`` decision receipt (RFC-MACP-0013 Appendix A.2)."""

    receipt_format: str
    session_id: str
    mode: str
    commitment: ReceiptCommitment
    commitment_hash: str
    commitment_wire_b64: NotRequired[str]
    semantics_rev: NotRequired[SemanticsRev]
    transcript: NotRequired[TranscriptDigest]
    issuer: str
    issued_at: str
    attestations: list[ReceiptAttestation]


class ReceiptKeySet(TypedDict):
    """A JWK Set [RFC 7517] of receipt verification keys (RFC-MACP-0013 §13).

    What ``GetReceiptKeys`` returns. Each key is a plain JWK dict; pass the set
    to `trusted_keys_from_jwks` to get the map `verify_receipt` takes.
    """

    keys: list[dict[str, Any]]


#: A.4 step 2. ``None`` only when the receipt is refused at step 1.
ReceiptIntegrity = Literal["match", "mismatch", "cannot_verify"] | None
#: A.4 step 3, one per attestation.
AttestationOutcome = Literal["valid", "invalid", "unknown_key", "unsupported"]
#: A.4 step 4 (step 1 for ``refused``).
ReceiptVerdict = Literal["attested", "integrity_only", "unverified", "refused"]


class ReceiptVerification(TypedDict):
    """The outcome of `verify_receipt`, in the spec vectors' ``expected`` shape."""

    integrity: ReceiptIntegrity
    #: One outcome per entry of ``attestations``, in order; ``[]`` unless integrity is ``match``.
    attestations: list[AttestationOutcome]
    verdict: ReceiptVerdict


@dataclass(frozen=True, slots=True)
class ReceiptSigner:
    """Signs a receipt with Ed25519 under ``kid``.

    Give exactly one of ``private_key``, a 32-byte Ed25519 seed, or ``sign``,
    a callback returning the 64-byte signature over the signing input (for a
    key held elsewhere, such as an HSM).
    """

    kid: str
    private_key: bytes | None = None
    sign: Callable[[bytes], bytes] | None = None

    def __post_init__(self) -> None:
        if (self.private_key is None) == (self.sign is None):
            raise MacpSdkError("ReceiptSigner: give exactly one of private_key or sign")


# --- encodings -------------------------------------------------------------

_STRICT_B64 = re.compile(r"(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?")
_STRICT_B64URL = re.compile(r"[A-Za-z0-9_-]+")
#: Integers in a body must survive a JavaScript verifier's JSON.parse unchanged.
_MAX_SAFE_INTEGER = 2**53 - 1


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _decode_b64(text: object) -> bytes | None:
    """Strict standard base64, padding required."""
    if not isinstance(text, str) or not _STRICT_B64.fullmatch(text):
        return None
    return base64.b64decode(text)


def _decode_b64url(text: object) -> bytes | None:
    """Strict unpadded base64url, non-empty."""
    if not isinstance(text, str) or not _STRICT_B64URL.fullmatch(text):
        return None
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):  # one leftover digit (length % 4 == 1)
        return None


def _jcs(value: object) -> str:
    """RFC 8785 serialization of a receipt body.

    Unlike the §3 projection, a body carries integers (``rev``,
    ``message_count``). Only safe integers have a JCS form here; a float, or
    an integer a JavaScript verifier would already have rounded, raises.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        if abs(value) > _MAX_SAFE_INTEGER:
            raise MacpSdkError("a receipt body cannot contain an integer outside ±(2^53-1)")
        return str(value)
    if isinstance(value, str):
        return _escape_json_string(value)
    if isinstance(value, list):
        return "[" + ",".join(_jcs(v) for v in value) + "]"
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value):
            raise MacpSdkError("a receipt body's member names must be strings")
        # RFC 8785 sorts member names by UTF-16 code units.
        keys = sorted(value, key=lambda k: k.encode("utf-16-be"))
        return "{" + ",".join(f"{_escape_json_string(k)}:{_jcs(value[k])}" for k in keys) + "}"
    raise MacpSdkError(f"a receipt body cannot contain a {type(value).__name__}")


def _receipt_body(receipt: Mapping[str, Any]) -> bytes:
    """The receipt body (A.2): the receipt without ``attestations``, in JCS."""
    return _jcs({k: v for k, v in receipt.items() if k != "attestations"}).encode("utf-8")


def _ed25519() -> Any:
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ImportError(
            "Ed25519 receipt signatures need the 'cryptography' package: "
            "pip install 'macp-sdk-python[receipts]'"
        ) from exc
    return ed25519


# --- projection ------------------------------------------------------------

_STRING_FIELDS = (
    "commitment_id",
    "action",
    "authority_scope",
    "reason",
    "mode_version",
    "policy_version",
    "configuration_version",
)
_ALLOWED_FIELDS = frozenset({*_STRING_FIELDS, "outcome_positive", "supersedes"})
_SUPERSEDES_FIELDS = frozenset({"session_id", "commitment_hash"})


def _project(payload: core_pb2.CommitmentPayload) -> ReceiptCommitment:
    """The §3 projection, with ``supersedes`` only when present."""
    out = ReceiptCommitment(
        commitment_id=payload.commitment_id,
        action=payload.action,
        authority_scope=payload.authority_scope,
        reason=payload.reason,
        mode_version=payload.mode_version,
        policy_version=payload.policy_version,
        configuration_version=payload.configuration_version,
        outcome_positive=payload.outcome_positive,
    )
    if payload.HasField("supersedes"):
        out["supersedes"] = SupersedesRef(
            session_id=payload.supersedes.session_id,
            commitment_hash=payload.supersedes.commitment_hash,
        )
    return out


def _read_commitment(value: object) -> core_pb2.CommitmentPayload | None:
    """A receipt's ``commitment`` as a ``CommitmentPayload``, or ``None``.

    ``None`` when it is not a complete, well-typed §3 projection: an unknown
    member at either level, a missing member, or a wrongly typed one. That is
    outside what ``macp-commitment-hash/1`` covers, so it is ``cannot_verify``.
    """
    if not isinstance(value, dict) or not set(value) <= _ALLOWED_FIELDS:
        return None
    if not all(isinstance(value.get(f), str) for f in _STRING_FIELDS):
        return None
    if not isinstance(value.get("outcome_positive"), bool):
        return None
    payload = core_pb2.CommitmentPayload(
        **{f: value[f] for f in _STRING_FIELDS}, outcome_positive=value["outcome_positive"]
    )
    if "supersedes" in value:
        sup = value["supersedes"]
        if (
            not isinstance(sup, dict)
            or set(sup) != _SUPERSEDES_FIELDS
            or not all(isinstance(sup[f], str) for f in _SUPERSEDES_FIELDS)
        ):
            return None
        payload.supersedes.SetInParent()
        payload.supersedes.session_id = sup["session_id"]
        payload.supersedes.commitment_hash = sup["commitment_hash"]
    return payload


def commitment_from_projection(projection: Mapping[str, Any]) -> core_pb2.CommitmentPayload:
    """Convert a snake_case §3 projection into a ``CommitmentPayload``.

    The projection is a receipt's ``commitment``, or the same shape from a
    control plane. Raises `MacpSdkError` when it is not a complete,
    well-typed projection.
    """
    payload = _read_commitment(dict(projection) if isinstance(projection, Mapping) else projection)
    if payload is None:
        raise MacpSdkError("commitment_from_projection: not a complete RFC-MACP-0013 §3 projection")
    return payload


# --- build -----------------------------------------------------------------


def _require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or value == "":
        raise MacpSdkError(f"build_receipt: {name} must be a non-empty string")
    return value


def _require_count(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= _MAX_SAFE_INTEGER:
        raise MacpSdkError(f"build_receipt: {name} must be a non-negative integer")
    return value


def build_receipt(
    commitment: core_pb2.CommitmentPayload,
    *,
    session_id: str,
    mode: str,
    issuer: str,
    issued_at: str,
    signer: ReceiptSigner | None = None,
    semantics_rev: Mapping[str, Any] | None = None,
    transcript: Mapping[str, Any] | None = None,
    commitment_wire: bytes | None = None,
) -> DecisionReceipt:
    """Build a ``macp-receipt/1`` receipt for ``commitment`` (RFC-MACP-0013 Appendix A).

    Computes ``commitment_hash``; with a ``signer``, adds one detached-payload
    EdDSA ``jws`` attestation over the receipt body. Ed25519 is
    deterministic, so the same inputs always give the same receipt.

    ``semantics_rev`` is ``{"implementation", "rev"}`` and ``transcript`` is
    ``{"digest_alg", "digest", "message_count"?}``, both snake_case as in the
    receipt. ``issued_at`` (RFC 3339 UTC) is recorded verbatim.

    Raises `MacpSdkError` on a missing or malformed argument, and when
    ``commitment_wire`` does not verify against, or does not decode to,
    ``commitment``, so no receipt is issued that its own verifier would
    reject.
    """
    digest = commitment_hash(commitment)
    receipt: dict[str, Any] = {
        "receipt_format": RECEIPT_FORMAT,
        "session_id": _require_text(session_id, "session_id"),
        "mode": _require_text(mode, "mode"),
        "commitment": _project(commitment),
        "commitment_hash": digest,
    }
    # Key order follows the spec's example and vectors: optional members after issued_at.
    if semantics_rev is not None:
        receipt["semantics_rev"] = SemanticsRev(
            implementation=_require_text(
                semantics_rev.get("implementation"), "semantics_rev.implementation"
            ),
            rev=_require_count(semantics_rev.get("rev"), "semantics_rev.rev"),
        )
    receipt["issuer"] = _require_text(issuer, "issuer")
    receipt["issued_at"] = _require_text(issued_at, "issued_at")
    if commitment_wire is not None:
        # A §6.1 match means the wire projects to exactly this commitment.
        if (
            not isinstance(commitment_wire, bytes | bytearray)
            or verify_commitment_hash(bytes(commitment_wire), digest) != "match"
        ):
            raise MacpSdkError(
                "build_receipt: commitment_wire is not the wire encoding of commitment"
            )
        receipt["commitment_wire_b64"] = base64.b64encode(commitment_wire).decode("ascii")
    if transcript is not None:
        digest_record = TranscriptDigest(
            digest_alg=_require_text(transcript.get("digest_alg"), "transcript.digest_alg"),
            digest=_require_text(transcript.get("digest"), "transcript.digest"),
        )
        if "message_count" in transcript:
            digest_record["message_count"] = _require_count(
                transcript["message_count"], "transcript.message_count"
            )
        receipt["transcript"] = digest_record
    attestations: list[ReceiptAttestation] = []
    receipt["attestations"] = attestations
    if signer is not None:
        attestations.append(_sign(receipt, signer))
    return receipt  # type: ignore[return-value]


def _sign(receipt: Mapping[str, Any], signer: ReceiptSigner) -> JwsAttestation:
    kid = _require_text(signer.kid, "signer.kid")
    # The protected header in JCS form: {"alg":"EdDSA","kid":"..."}.
    header = _b64url(f'{{"alg":"EdDSA","kid":{_escape_json_string(kid)}}}'.encode())
    signing_input = f"{header}.{_b64url(_receipt_body(receipt))}".encode("ascii")
    if signer.private_key is not None:
        if not isinstance(signer.private_key, bytes) or len(signer.private_key) != 32:
            raise MacpSdkError("build_receipt: signer.private_key must be a 32-byte Ed25519 seed")
        key = _ed25519().Ed25519PrivateKey.from_private_bytes(signer.private_key)
        signature = key.sign(signing_input)
    else:
        assert signer.sign is not None  # ReceiptSigner holds exactly one
        signature = signer.sign(signing_input)
        if not isinstance(signature, bytes) or len(signature) != 64:
            raise MacpSdkError("build_receipt: signer.sign must return a 64-byte Ed25519 signature")
    return JwsAttestation(type="jws", jws=f"{header}..{_b64url(signature)}")


# --- verify ----------------------------------------------------------------


def _integrity(receipt: Mapping[str, Any]) -> Literal["match", "mismatch", "cannot_verify"]:
    payload = _read_commitment(receipt.get("commitment"))
    if payload is None:
        return "cannot_verify"
    claimed = receipt.get("commitment_hash")
    if not isinstance(claimed, str) or commitment_hash(payload) != claimed:
        return "mismatch"
    if "commitment_wire_b64" in receipt:
        wire = _decode_b64(receipt["commitment_wire_b64"])
        if wire is None:
            return "cannot_verify"
        outcome = verify_commitment_hash(wire, claimed)
        if outcome != "match":
            return outcome
        decoded = core_pb2.CommitmentPayload()
        decoded.ParseFromString(wire)  # §6.1 parsed it already
        if _project(decoded) != _project(payload):
            return "mismatch"
    return "match"


def _attestation(att: object, body: str, trusted_keys: Mapping[str, Any]) -> AttestationOutcome:
    # ``vendor`` (whatever it binds) and unknown types: no other format is implemented.
    if not isinstance(att, dict) or att.get("type") != "jws":
        return "unsupported"
    jws = att.get("jws")
    parts = jws.split(".") if isinstance(jws, str) else []
    if len(parts) != 3 or parts[1] != "":
        return "invalid"
    header_bytes = _decode_b64url(parts[0])
    signature = _decode_b64url(parts[2])
    if header_bytes is None or signature is None:
        return "invalid"
    try:
        header = json.loads(header_bytes.decode("utf-8"))
    except ValueError:  # UnicodeDecodeError and JSONDecodeError
        return "invalid"
    if not isinstance(header, dict) or header.get("alg") != "EdDSA":
        return "unsupported"
    kid = header.get("kid")
    if not isinstance(kid, str) or kid not in trusted_keys:
        return "unknown_key"
    public_key = _decode_b64url(trusted_keys[kid])
    if public_key is None or len(public_key) != 32 or len(signature) != 64:
        return "invalid"
    ed25519 = _ed25519()
    from cryptography.exceptions import InvalidSignature

    try:
        ed25519.Ed25519PublicKey.from_public_bytes(public_key).verify(
            signature, f"{parts[0]}.{body}".encode("ascii")
        )
    except (InvalidSignature, ValueError):
        return "invalid"
    return "valid"


def verify_receipt(receipt: object, trusted_keys: Mapping[str, str]) -> ReceiptVerification:
    """Verify a decision receipt (RFC-MACP-0013 Appendix A.4).

    ``trusted_keys`` maps a ``kid`` to an Ed25519 public key as a JWK ``x``
    (base64url). Returns the integrity outcome, one outcome per attestation,
    and the overall verdict, in the spec vectors' vocabulary, so the result
    compares equal to a vector's ``expected``. Malformed input yields an
    outcome, not an exception.
    """
    if not isinstance(receipt, dict) or receipt.get("receipt_format") != RECEIPT_FORMAT:
        return ReceiptVerification(integrity=None, attestations=[], verdict="refused")
    integrity = _integrity(receipt)
    if integrity != "match":
        return ReceiptVerification(integrity=integrity, attestations=[], verdict="unverified")
    body: str | None
    try:
        body = _b64url(_receipt_body(receipt))
    except MacpSdkError:
        body = None  # a body with no JCS form: no signature over it can be valid
    keys = trusted_keys if isinstance(trusted_keys, Mapping) else {}
    raw = receipt.get("attestations")
    outcomes: list[AttestationOutcome] = []
    for att in raw if isinstance(raw, list) else []:
        if body is None:
            jws = isinstance(att, dict) and att.get("type") == "jws"
            outcomes.append("invalid" if jws else "unsupported")
        else:
            outcomes.append(_attestation(att, body, keys))
    return ReceiptVerification(
        integrity="match",
        attestations=outcomes,
        verdict="attested" if "valid" in outcomes else "integrity_only",
    )


#: JWK private members (RFC 7518 §6). §13 item 8 forbids them in a published key set.
_JWK_PRIVATE_MEMBERS = ("d", "p", "q", "dp", "dq", "qi", "oth", "k")


def _usable_ed25519_key(key: object) -> tuple[str, str] | None:
    if not isinstance(key, Mapping):
        return None
    kid, x = key.get("kid"), key.get("x")
    if (
        key.get("kty") != "OKP"
        or key.get("crv") != "Ed25519"
        or not isinstance(kid, str)
        or kid == ""
        or not isinstance(x, str)
        or x == ""
        or key.get("alg", "EdDSA") != "EdDSA"
        or key.get("use", "sig") != "sig"
        or any(member in key for member in _JWK_PRIVATE_MEMBERS)
    ):
        return None
    return kid, x


def trusted_keys_from_jwks(jwks: object) -> dict[str, str]:
    """Return the ``kid`` → ``x`` map `verify_receipt` takes, from a JWK Set.

    Pass the key set from ``client.get_receipt_keys()``. A key is kept only if
    it is an Ed25519 public key that can verify a receipt's ``jws`` attestation:
    ``kty`` ``OKP``, ``crv`` ``Ed25519``, a non-empty ``kid`` and ``x``,
    ``alg`` absent or ``EdDSA``, ``use`` absent or ``sig``, and no private
    member (``d``, ``p``, ``q``, ``dp``, ``dq``, ``qi``, ``oth``, ``k``; RFC-MACP-0013
    §13 item 8). Other keys are skipped. A ``kid`` that occurs more than once
    is dropped entirely, because §13 item 8 makes ``kid`` unique and an
    ambiguous one names no single key.

    Like `verify_receipt`, it never raises: input that is not a key set gives
    ``{}``, so every receipt verifies at most ``integrity_only``.

    Trusting keys fetched from a runtime establishes that the runtime vouches
    for its receipts (§13 item 9), not that any third party does.
    """
    raw = jwks.get("keys") if isinstance(jwks, Mapping) else None
    if not isinstance(raw, list):
        return {}
    trusted: dict[str, str] = {}
    seen: set[str] = set()
    ambiguous: set[str] = set()
    for key in raw:
        kid = key.get("kid") if isinstance(key, Mapping) else None
        if isinstance(kid, str):
            if kid in seen:
                ambiguous.add(kid)
            seen.add(kid)
        usable = _usable_ed25519_key(key)
        if usable is not None:
            trusted[usable[0]] = usable[1]
    for kid in ambiguous:
        trusted.pop(kid, None)
    return trusted


@dataclass(frozen=True, slots=True)
class DecisionReceiptResult:
    """What ``GetDecisionReceipt`` returned (RFC-MACP-0013 §13).

    ``raw`` is the runtime's ``receipt_json`` verbatim: §13 serves an attested
    receipt byte-identical on every call, so keep ``raw`` to store or forward
    it. ``receipt`` is ``raw`` parsed, ready for `verify_receipt`. The
    dataclass is frozen, but ``receipt`` is a plain dict: it is not deep-frozen.
    """

    receipt: dict[str, Any]
    raw: str


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not valid JSON")


def _loads_strict(raw: str, what: str) -> Any:
    """Parse runtime JSON, raising `MacpSdkError` on anything that is not JSON.

    Rejects ``NaN``/``Infinity`` (Python's json accepts them; JSON does not)
    and nesting deep enough to exhaust the recursion limit.
    """
    try:
        return json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:
        raise MacpSdkError(f"{what} is not valid JSON: {exc}") from exc


def _parse_receipt_json(raw: str) -> DecisionReceiptResult:
    """``GetDecisionReceiptResponse.receipt_json`` to a `DecisionReceiptResult`.

    Only the JSON shape is checked (an object). ``receipt_format`` and the
    rest are left to `verify_receipt`, which reports instead of raising.
    """
    receipt = _loads_strict(raw, "GetDecisionReceipt receipt_json")
    if not isinstance(receipt, dict):
        raise MacpSdkError("GetDecisionReceipt receipt_json is not a JSON object")
    return DecisionReceiptResult(receipt=receipt, raw=raw)


def _parse_receipt_keys_json(raw: str) -> ReceiptKeySet:
    """``GetReceiptKeysResponse.jwks_json`` to a `ReceiptKeySet`.

    Checks only that it is an object with a ``keys`` list; individual keys
    are judged by `trusted_keys_from_jwks`.
    """
    jwks = _loads_strict(raw, "GetReceiptKeys jwks_json")
    if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
        raise MacpSdkError("GetReceiptKeys jwks_json is not a JWK Set (no keys list)")
    return cast(ReceiptKeySet, jwks)


__all__ = [
    "RECEIPT_FORMAT",
    "AttestationOutcome",
    "DecisionReceipt",
    "DecisionReceiptResult",
    "JwsAttestation",
    "ReceiptAttestation",
    "ReceiptCommitment",
    "ReceiptIntegrity",
    "ReceiptKeySet",
    "ReceiptSigner",
    "ReceiptVerdict",
    "ReceiptVerification",
    "VendorAttestation",
    "build_receipt",
    "commitment_from_projection",
    "trusted_keys_from_jwks",
    "verify_receipt",
]

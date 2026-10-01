from __future__ import annotations

from dataclasses import dataclass

from .errors import MacpSessionError


@dataclass(frozen=True)
class AuthConfig:
    """Authentication configuration for a MACP client or session.

    The ``expected_sender`` field is a client-side guardrail: when set, the
    *resolved* sender — an explicit ``sender=`` passed to a session helper,
    or the fallback to ``sender_hint`` when no explicit ``sender=`` is
    given — must match it, otherwise the SDK raises
    :class:`MacpIdentityMismatchError` before the envelope reaches the wire.
    Per RFC-MACP-0004 §4 the runtime derives ``sender`` from authenticated
    identity, so mismatches are always rejected — this check just surfaces
    the problem earlier and more clearly.
    """

    bearer_token: str | None = None
    sender_hint: str | None = None
    expected_sender: str | None = None

    def __post_init__(self) -> None:
        if not self.bearer_token:
            raise MacpSessionError("bearer_token is required")

    @classmethod
    def for_dev_agent(cls, agent_id: str, *, expected_sender: str | None = None) -> AuthConfig:
        """Build an AuthConfig for local development without a real token.

        Emits ``Authorization: Bearer <agent_id>``. The runtime's
        ``dev_authenticate`` fallback binds the bearer token value
        verbatim as the authenticated sender, so passing the raw agent
        id keeps participant lists like ``["coordinator", "alice"]``
        working unchanged.

        For production deployments issue real tokens via the auth
        resolver chain and use :meth:`for_bearer` directly.
        """
        return cls(
            bearer_token=agent_id,
            sender_hint=agent_id,
            expected_sender=expected_sender or agent_id,
        )

    @classmethod
    def for_bearer(
        cls,
        token: str,
        *,
        sender_hint: str | None = None,
        expected_sender: str | None = None,
    ) -> AuthConfig:
        """Build a Bearer-token AuthConfig.

        :param token: the Bearer token issued by the runtime
        :param sender_hint: the sender string the SDK places on envelopes when
            no explicit ``sender=`` is provided. Defaults to ``expected_sender``
            when only the latter is supplied.
        :param expected_sender: the identity the runtime will bind this token
            to. When set, the SDK rejects any explicit ``sender=`` that does
            not match with :class:`MacpIdentityMismatchError`.
        """
        return cls(
            bearer_token=token,
            sender_hint=sender_hint or expected_sender,
            expected_sender=expected_sender,
        )

    @property
    def sender(self) -> str | None:
        return self.sender_hint

    def metadata(self) -> list[tuple[str, str]]:
        return [("authorization", f"Bearer {self.bearer_token}")]

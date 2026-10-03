"""Gateway error taxonomy (plan §16/§19): typed, auditable, fail-closed.

Every denial/error class maps to a distinct type so tool handlers can raise
precisely and the server layer can map to MCP surfaces + audit entries
consistently. Token-value leakage is impossible: messages never interpolate
token material.
"""
from __future__ import annotations

from enum import StrEnum


class GatewayErrorType(StrEnum):
    UNAUTHENTICATED = "UNAUTHENTICATED"
    INVALID_AGENT_TOKEN = "INVALID_AGENT_TOKEN"
    EXPIRED_AGENT_TOKEN = "EXPIRED_AGENT_TOKEN"
    REVOKED_AGENT_TOKEN = "REVOKED_AGENT_TOKEN"
    INVALID_ASSIGNMENT_TOKEN = "INVALID_ASSIGNMENT_TOKEN"
    EXPIRED_ASSIGNMENT_TOKEN = "EXPIRED_ASSIGNMENT_TOKEN"
    ROLE_REQUIRED = "ROLE_REQUIRED"
    SCOPE_REQUIRED = "SCOPE_REQUIRED"
    NOT_FOUND = "NOT_FOUND"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"
    DESTRUCTIVE_DENY = "DESTRUCTIVE_DENY"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    DOMAIN_UNAVAILABLE = "DOMAIN_UNAVAILABLE"
    VALIDATION = "VALIDATION"
    CONFLICT = "CONFLICT"
    QUARANTINED = "QUARANTINED"
    INTERNAL = "INTERNAL"


class GatewayError(RuntimeError):
    """Base: every gateway-visible failure carries type + operator-safe detail."""

    error_type = GatewayErrorType.INTERNAL

    def __init__(self, detail: str = ""):
        super().__init__(detail or self.error_type.value)
        self.detail = detail or self.error_type.value


class UnauthenticatedError(GatewayError):
    error_type = GatewayErrorType.UNAUTHENTICATED


class InvalidAgentTokenError(GatewayError):
    error_type = GatewayErrorType.INVALID_AGENT_TOKEN


class ExpiredAgentTokenError(GatewayError):
    error_type = GatewayErrorType.EXPIRED_AGENT_TOKEN


class RevokedAgentTokenError(GatewayError):
    error_type = GatewayErrorType.REVOKED_AGENT_TOKEN


class InvalidAssignmentTokenError(GatewayError):
    error_type = GatewayErrorType.INVALID_ASSIGNMENT_TOKEN


class ExpiredAssignmentTokenError(GatewayError):
    error_type = GatewayErrorType.EXPIRED_ASSIGNMENT_TOKEN


class RoleRequiredError(GatewayError):
    error_type = GatewayErrorType.ROLE_REQUIRED


class ScopeRequiredError(GatewayError):
    error_type = GatewayErrorType.SCOPE_REQUIRED


class NotFoundError(GatewayError):
    error_type = GatewayErrorType.NOT_FOUND


class OutOfScopeError(GatewayError):
    error_type = GatewayErrorType.OUT_OF_SCOPE


class DestructiveDenyError(GatewayError):
    """Hard deny for DESTRUCTIVE risk-class calls by non-privileged callers."""
    error_type = GatewayErrorType.DESTRUCTIVE_DENY


class ApprovalRequiredError(GatewayError):
    error_type = GatewayErrorType.APPROVAL_REQUIRED


class DomainUnavailableError(GatewayError):
    error_type = GatewayErrorType.DOMAIN_UNAVAILABLE


class ValidationError_(GatewayError):
    error_type = GatewayErrorType.VALIDATION


class ConflictError(GatewayError):
    error_type = GatewayErrorType.CONFLICT


class QuarantinedContentError(GatewayError):
    """Secret-like content detected in record body — no retry or sanitization."""
    error_type = GatewayErrorType.QUARANTINED

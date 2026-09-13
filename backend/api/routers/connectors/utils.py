"""Shared credential mapping for the connector endpoints."""

from api.models.connectors import Credential, SubjectType
from api.routers.connectors.schemas import CredentialStatus


def credential_to_status(cred: Credential) -> CredentialStatus:
    """Project a stored credential to what the frontend may see — never the secret."""
    return CredentialStatus(
        id=cred.id,
        org_id=cred.org_id,
        subject_type=SubjectType(cred.subject_type),
        subject_id=cred.subject_id,
        auth_type=cred.auth_type,  # type: ignore[arg-type]
        settings=cred.settings or {},
        created_at=cred.created_at,
        updated_at=cred.updated_at,
    )

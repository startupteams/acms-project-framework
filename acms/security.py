from fastapi import Header, HTTPException, status

from .settings import get_settings


def require_admin_token(authorization: str | None = Header(default=None)) -> None:
    expected = get_settings().admin_token
    if expected == "change-me":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ACMS_ADMIN_TOKEN must be configured",
        )
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    supplied = authorization.removeprefix("Bearer ")
    if supplied != expected:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid bearer token")

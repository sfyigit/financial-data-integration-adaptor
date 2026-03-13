"""
Authentication Module for External Bank API
============================================
Provides API Key authentication for securing endpoints.
Validates X-API-KEY header against the api_keys table in the database.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from fastapi import HTTPException, Security, Request
from fastapi.security import APIKeyHeader

from db import get_session
from models import ApiKey

logger = logging.getLogger("external_bank.auth")

# API Key header configuration
API_KEY_HEADER_NAME = "X-API-KEY"
api_key_header = APIKeyHeader(name=API_KEY_HEADER_NAME, auto_error=False)


@dataclass
class AuthenticatedService:
    """
    Represents an authenticated service after API key validation.
    This is a simple dataclass to avoid SQLAlchemy detached session issues.
    """
    id: int
    service_name: str
    key_prefix: str
    description: Optional[str] = None


def validate_api_key_from_db(api_key: str) -> Optional[AuthenticatedService]:
    """
    Validate API key against the database.
    
    Args:
        api_key: The API key to validate.
        
    Returns:
        AuthenticatedService object if valid and active, None otherwise.
    """
    session = get_session()
    try:
        key_record = (
            session.query(ApiKey)
            .filter(ApiKey.api_key == api_key, ApiKey.is_active == True)
            .first()
        )
        
        if key_record:
            # Extract data while session is still open
            authenticated_service = AuthenticatedService(
                id=key_record.id,
                service_name=key_record.service_name,
                key_prefix=key_record.key_prefix,
                description=key_record.description,
            )
            
            # Update last_used_at timestamp
            key_record.last_used_at = datetime.utcnow()
            session.commit()
            
            return authenticated_service
        
        return None
    except Exception as e:
        logger.error(f"Database error during API key validation: {e}")
        session.rollback()
        return None
    finally:
        session.close()


async def verify_api_key(
    request: Request,
    api_key: Optional[str] = Security(api_key_header),
) -> AuthenticatedService:
    """
    Dependency to verify the X-API-KEY header against the database.
    
    Raises:
        HTTPException: If API key is missing, invalid, or inactive.
        
    Returns:
        AuthenticatedService object containing service information.
    """
    # TODO: We can add IP whitelisting here
    # client_ip = request.client.host
    # allowed_ips = os.getenv("ALLOWED_IPS", "").split(",")
    # if allowed_ips and client_ip not in allowed_ips:
    #     logger.warning(f"Request from non-whitelisted IP: {client_ip}")
    #     raise HTTPException(status_code=403, detail="IP not allowed")
    
    if not api_key:
        logger.warning(
            f"Missing API key in request from {request.client.host}"
        )
        raise HTTPException(
            status_code=401,
            detail="Missing X-API-KEY header",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    
    # Validate against database
    authenticated_service = validate_api_key_from_db(api_key)
    
    if not authenticated_service:
        logger.warning(
            f"Invalid or inactive API key attempt from {request.client.host} "
            f"(key prefix: {api_key[:8]}...)"
        )
        raise HTTPException(
            status_code=403,
            detail="Invalid or inactive API key",
        )
    
    logger.debug(
        f"Authenticated request from service '{authenticated_service.service_name}' "
        f"(key: {authenticated_service.key_prefix}...)"
    )
    
    return authenticated_service

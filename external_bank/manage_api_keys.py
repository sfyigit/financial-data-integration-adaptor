#!/usr/bin/env python3
"""
API Key Management CLI for External Bank API
=============================================
Utility script to create, list, and manage API keys for SaaS services.

Usage:
    python manage_api_keys.py create --service "django_adapter" --description "Main SaaS sync service"
    python manage_api_keys.py list
    python manage_api_keys.py deactivate --key "fsec_abc123..."
    python manage_api_keys.py delete --key "fsec_abc123..."
"""

import argparse
import secrets
import sys
from datetime import datetime

from db import get_session, init_db
from models import ApiKey


def generate_api_key(prefix: str = "fsec") -> str:
    """
    Generate a secure random API key.
    
    Format: {prefix}_{32_random_hex_chars}
    Example: fsec_a1b2c3d4e5f6g7h8i9j0k1l2m3n4o5p6
    """
    random_part = secrets.token_hex(32)
    return f"{prefix}_{random_part}"


def create_api_key(service_name: str, description: str = None) -> str:
    """
    Create a new API key for a service.
    
    Args:
        service_name: Name of the service (e.g., "django_adapter")
        description: Optional description
        
    Returns:
        The generated API key (show this to the user only once!)
    """
    session = get_session()
    try:
        api_key = generate_api_key()
        key_prefix = api_key[:12]  # "fsec_" + first 7 chars
        
        new_key = ApiKey(
            api_key=api_key,
            key_prefix=key_prefix,
            service_name=service_name,
            description=description,
            is_active=True,
            created_at=datetime.utcnow(),
        )
        
        session.add(new_key)
        session.commit()
        
        print(f"\n{'='*60}")
        print("API KEY CREATED SUCCESSFULLY")
        print(f"{'='*60}")
        print(f"Service Name: {service_name}")
        print(f"Key Prefix:   {key_prefix}...")
        print(f"Description:  {description or 'N/A'}")
        print(f"\n⚠️  IMPORTANT: Save this API key now! It won't be shown again.")
        print(f"\n🔑 API Key: {api_key}")
        print(f"{'='*60}\n")
        
        return api_key
        
    except Exception as e:
        session.rollback()
        print(f"❌ Error creating API key: {e}")
        sys.exit(1)
    finally:
        session.close()


def list_api_keys():
    """List all API keys (showing only prefixes for security)."""
    session = get_session()
    try:
        keys = session.query(ApiKey).order_by(ApiKey.created_at.desc()).all()
        
        if not keys:
            print("\nNo API keys found.\n")
            return
        
        print(f"\n{'='*80}")
        print(f"{'ID':<5} {'Prefix':<15} {'Service':<25} {'Active':<8} {'Last Used':<20}")
        print(f"{'='*80}")
        
        for key in keys:
            last_used = key.last_used_at.strftime("%Y-%m-%d %H:%M") if key.last_used_at else "Never"
            status = "✅ Yes" if key.is_active else "❌ No"
            print(f"{key.id:<5} {key.key_prefix:<15} {key.service_name:<25} {status:<8} {last_used:<20}")
        
        print(f"{'='*80}")
        print(f"Total: {len(keys)} key(s)\n")
        
    finally:
        session.close()


def deactivate_api_key(key_or_prefix: str):
    """Deactivate an API key (can be reactivated later)."""
    session = get_session()
    try:
        # Search by full key or prefix
        key_record = (
            session.query(ApiKey)
            .filter(
                (ApiKey.api_key == key_or_prefix) | 
                (ApiKey.key_prefix == key_or_prefix[:12])
            )
            .first()
        )
        
        if not key_record:
            print(f"❌ API key not found: {key_or_prefix[:12]}...")
            sys.exit(1)
        
        key_record.is_active = False
        session.commit()
        
        print(f"✅ API key deactivated: {key_record.key_prefix}... ({key_record.service_name})")
        
    except Exception as e:
        session.rollback()
        print(f"❌ Error deactivating API key: {e}")
        sys.exit(1)
    finally:
        session.close()


def activate_api_key(key_or_prefix: str):
    """Reactivate a deactivated API key."""
    session = get_session()
    try:
        key_record = (
            session.query(ApiKey)
            .filter(
                (ApiKey.api_key == key_or_prefix) | 
                (ApiKey.key_prefix == key_or_prefix[:12])
            )
            .first()
        )
        
        if not key_record:
            print(f"❌ API key not found: {key_or_prefix[:12]}...")
            sys.exit(1)
        
        key_record.is_active = True
        session.commit()
        
        print(f"✅ API key activated: {key_record.key_prefix}... ({key_record.service_name})")
        
    except Exception as e:
        session.rollback()
        print(f"❌ Error activating API key: {e}")
        sys.exit(1)
    finally:
        session.close()


def delete_api_key(key_or_prefix: str):
    """Permanently delete an API key."""
    session = get_session()
    try:
        key_record = (
            session.query(ApiKey)
            .filter(
                (ApiKey.api_key == key_or_prefix) | 
                (ApiKey.key_prefix == key_or_prefix[:12])
            )
            .first()
        )
        
        if not key_record:
            print(f"❌ API key not found: {key_or_prefix[:12]}...")
            sys.exit(1)
        
        service_name = key_record.service_name
        prefix = key_record.key_prefix
        
        session.delete(key_record)
        session.commit()
        
        print(f"✅ API key deleted permanently: {prefix}... ({service_name})")
        
    except Exception as e:
        session.rollback()
        print(f"❌ Error deleting API key: {e}")
        sys.exit(1)
    finally:
        session.close()


def main():
    parser = argparse.ArgumentParser(
        description="Manage API keys for External Bank API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  Create a new API key:
    python manage_api_keys.py create --service "django_adapter" --description "SaaS sync service"
    
  List all API keys:
    python manage_api_keys.py list
    
  Deactivate an API key:
    python manage_api_keys.py deactivate --key "fsec_abc123"
    
  Delete an API key:
    python manage_api_keys.py delete --key "fsec_abc123"
        """
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Command to execute")
    
    # Create command
    create_parser = subparsers.add_parser("create", help="Create a new API key")
    create_parser.add_argument(
        "--service", "-s",
        required=True,
        help="Name of the service (e.g., 'django_adapter')"
    )
    create_parser.add_argument(
        "--description", "-d",
        help="Optional description for the API key"
    )
    
    # List command
    subparsers.add_parser("list", help="List all API keys")
    
    # Deactivate command
    deactivate_parser = subparsers.add_parser("deactivate", help="Deactivate an API key")
    deactivate_parser.add_argument(
        "--key", "-k",
        required=True,
        help="API key or key prefix to deactivate"
    )
    
    # Activate command
    activate_parser = subparsers.add_parser("activate", help="Activate a deactivated API key")
    activate_parser.add_argument(
        "--key", "-k",
        required=True,
        help="API key or key prefix to activate"
    )
    
    # Delete command
    delete_parser = subparsers.add_parser("delete", help="Permanently delete an API key")
    delete_parser.add_argument(
        "--key", "-k",
        required=True,
        help="API key or key prefix to delete"
    )
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        sys.exit(1)
    
    # Initialize database (ensures schema and tables exist)
    try:
        init_db()
    except Exception as e:
        print(f"❌ Database initialization failed: {e}")
        print("Make sure EXTERNAL_BANK_DB_URL environment variable is set.")
        sys.exit(1)
    
    # Execute command
    if args.command == "create":
        create_api_key(args.service, args.description)
    elif args.command == "list":
        list_api_keys()
    elif args.command == "deactivate":
        deactivate_api_key(args.key)
    elif args.command == "activate":
        activate_api_key(args.key)
    elif args.command == "delete":
        delete_api_key(args.key)


if __name__ == "__main__":
    main()

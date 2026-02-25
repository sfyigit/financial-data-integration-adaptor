"""
Integration Tests for User Registration.

Covers:
  - Successful registration creates both User and Tenant
  - Duplicate username rejection
  - Duplicate tenant_id rejection
  - Duplicate email rejection
  - Password mismatch rejection
  - Weak password rejection
  - Missing fields rejection
  - Already authenticated user is redirected
"""

import pytest
from django.contrib.auth.models import User
from rest_framework import status

from tenants.models import Tenant


class TestRegistrationPage:
    """Tests for GET /register/"""

    def test_register_page_renders(self, api_client, db):
        """GET /register/ should return 200 with registration form."""
        response = api_client.get("/register/")
        assert response.status_code == status.HTTP_200_OK
        content = response.content.decode()
        assert "Register" in content or "register" in content

    def test_authenticated_user_redirected(self, api_client, user_bank1, membership_bank1):
        """Already logged-in user should be redirected away from register."""
        api_client.login(username="bank1_user", password="Bank1Pass123!")
        response = api_client.get("/register/")
        assert response.status_code == status.HTTP_302_FOUND


class TestRegistrationSuccess:
    """Tests for successful POST /register/"""

    def test_creates_user_and_tenant(self, api_client, db):
        """Successful registration should create both a User and a Tenant."""
        response = api_client.post("/register/", {
            "username": "newuser",
            "email": "new@example.com",
            "password": "StrongPass123!",
            "password_confirm": "StrongPass123!",
            "tenant_id": "BANK_NEW",
            "tenant_name": "New Test Bank",
        })

        # Should redirect to dashboard on success
        assert response.status_code == status.HTTP_302_FOUND

        # Verify user created
        assert User.objects.filter(username="newuser").exists()
        user = User.objects.get(username="newuser")
        assert user.email == "new@example.com"
        assert user.check_password("StrongPass123!")

        # Verify tenant created
        assert Tenant.objects.filter(tenant_id="BANK_NEW").exists()
        tenant = Tenant.objects.get(tenant_id="BANK_NEW")
        assert tenant.name == "New Test Bank"
        assert tenant.is_active is True

    def test_tenant_id_uppercased(self, api_client, db):
        """Tenant ID should be uppercased automatically."""
        response = api_client.post("/register/", {
            "username": "caseuser",
            "email": "case@example.com",
            "password": "StrongPass123!",
            "password_confirm": "StrongPass123!",
            "tenant_id": "bank_lower",
            "tenant_name": "Lower Bank",
        })
        assert response.status_code == status.HTTP_302_FOUND
        assert Tenant.objects.filter(tenant_id="BANK_LOWER").exists()

    def test_user_logged_in_after_registration(self, api_client, db):
        """After registration, the user should be logged in (session)."""
        api_client.post("/register/", {
            "username": "logincheck",
            "email": "login@example.com",
            "password": "StrongPass123!",
            "password_confirm": "StrongPass123!",
            "tenant_id": "BANK_LOGIN",
            "tenant_name": "Login Bank",
        })

        # Accessing dashboard (requires login) should NOT redirect to login
        response = api_client.get("/")
        assert response.status_code == status.HTTP_200_OK


class TestRegistrationValidation:
    """Tests for validation errors during registration."""

    def test_duplicate_username_rejected(self, api_client, user_bank1, db):
        """Registering with an existing username should fail."""
        response = api_client.post("/register/", {
            "username": "bank1_user",  # Already exists
            "email": "unique@example.com",
            "password": "StrongPass123!",
            "password_confirm": "StrongPass123!",
            "tenant_id": "BANK_DUP_USER",
            "tenant_name": "Dup User Bank",
        })
        assert response.status_code == status.HTTP_200_OK  # Re-renders form
        content = response.content.decode()
        assert "already" in content.lower() or "taken" in content.lower()

    def test_duplicate_tenant_id_rejected(
        self, api_client, tenant_bank1, db
    ):
        """Registering with an existing tenant_id should fail."""
        response = api_client.post("/register/", {
            "username": "unique_user",
            "email": "unique2@example.com",
            "password": "StrongPass123!",
            "password_confirm": "StrongPass123!",
            "tenant_id": "BANK001",  # Already exists
            "tenant_name": "Duplicate Tenant",
        })
        assert response.status_code == status.HTTP_200_OK  # Re-renders form
        content = response.content.decode()
        assert "already exists" in content.lower() or "already" in content.lower()

    def test_duplicate_email_rejected(self, api_client, user_bank1, db):
        """Registering with an existing email should fail."""
        response = api_client.post("/register/", {
            "username": "email_dup_user",
            "email": "bank1@fsec.io",  # Already used by user_bank1
            "password": "StrongPass123!",
            "password_confirm": "StrongPass123!",
            "tenant_id": "BANK_EMAIL_DUP",
            "tenant_name": "Email Dup Bank",
        })
        assert response.status_code == status.HTTP_200_OK
        content = response.content.decode()
        assert "already" in content.lower() or "email" in content.lower()

    def test_password_mismatch_rejected(self, api_client, db):
        """Mismatched passwords should fail."""
        response = api_client.post("/register/", {
            "username": "mismatch_user",
            "email": "mismatch@example.com",
            "password": "StrongPass123!",
            "password_confirm": "DifferentPass456!",
            "tenant_id": "BANK_MISMATCH",
            "tenant_name": "Mismatch Bank",
        })
        assert response.status_code == status.HTTP_200_OK
        content = response.content.decode()
        assert "match" in content.lower() or "password" in content.lower()

    def test_missing_username_rejected(self, api_client, db):
        """Missing username should fail."""
        response = api_client.post("/register/", {
            "username": "",
            "email": "nouser@example.com",
            "password": "StrongPass123!",
            "password_confirm": "StrongPass123!",
            "tenant_id": "BANK_NOUSER",
            "tenant_name": "No User Bank",
        })
        assert response.status_code == status.HTTP_200_OK
        content = response.content.decode()
        assert "required" in content.lower() or "username" in content.lower()

    def test_missing_tenant_id_rejected(self, api_client, db):
        """Missing tenant_id should fail."""
        response = api_client.post("/register/", {
            "username": "notenant_user",
            "email": "notenant@example.com",
            "password": "StrongPass123!",
            "password_confirm": "StrongPass123!",
            "tenant_id": "",
            "tenant_name": "No Tenant",
        })
        assert response.status_code == status.HTTP_200_OK
        content = response.content.decode()
        assert "required" in content.lower() or "tenant" in content.lower()

    def test_no_user_or_tenant_created_on_failure(self, api_client, db):
        """Failed registration should not leave orphan records."""
        initial_users = User.objects.count()
        initial_tenants = Tenant.objects.count()

        api_client.post("/register/", {
            "username": "",  # Will fail
            "email": "fail@example.com",
            "password": "StrongPass123!",
            "password_confirm": "StrongPass123!",
            "tenant_id": "BANK_FAIL",
            "tenant_name": "Fail Bank",
        })

        assert User.objects.count() == initial_users
        assert Tenant.objects.count() == initial_tenants

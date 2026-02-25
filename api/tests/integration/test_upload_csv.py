"""
Integration Tests for CSV Upload Endpoints.

Covers:
  - API upload with tenant existence check
  - Upload to non-existent tenant returns 404
  - Upload to existing tenant forwards to External Bank (mocked)
  - Missing fields produce 400 errors
  - Web UI upload page renders correctly
"""

import io
import pytest
from unittest.mock import patch, MagicMock
from rest_framework import status

from tenants.models import Tenant


# =============================================================================
# API Upload CSV — POST /api/upload-csv/
# =============================================================================

class TestAPIUploadCSV:
    """Tests for the API-based CSV upload endpoint."""

    def _create_csv_file(self, content="loan_id,amount\nL001,50000\n"):
        """Helper to create an in-memory CSV file."""
        f = io.BytesIO(content.encode("utf-8"))
        f.name = "test.csv"
        return f

    def test_upload_requires_authentication(self, api_client, tenant_bank1):
        """Unauthenticated upload should be denied."""
        csv_file = self._create_csv_file()
        response = api_client.post("/api/upload-csv/", {
            "tenant_id": "BANK001",
            "file_type": "loans",
            "loan_type": "RETAIL",
            "file": csv_file,
        }, format="multipart")
        assert response.status_code in (
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
        )

    def test_upload_to_nonexistent_tenant_returns_404(
        self, api_client_bank1, tenant_bank1
    ):
        """Upload referencing a non-existent tenant should return 404."""
        csv_file = self._create_csv_file()
        response = api_client_bank1.post("/api/upload-csv/", {
            "tenant_id": "BANK999",
            "file_type": "loans",
            "loan_type": "RETAIL",
            "file": csv_file,
        }, format="multipart")
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert "not exist" in response.json()["error"].lower() or \
               "does not exist" in response.json()["error"].lower()

    @patch("tenants.views.http_requests.post")
    def test_upload_to_existing_tenant_forwards_to_bank(
        self, mock_post, api_client_bank1, tenant_bank1
    ):
        """Upload to existing tenant should forward CSV to External Bank."""
        # Mock External Bank response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "message": "Upload successful",
            "tenant_id": "BANK001",
            "file_type": "loans",
            "loan_type": "RETAIL",
            "records_processed": 1,
            "version": 1,
        }
        mock_post.return_value = mock_response

        csv_file = self._create_csv_file()
        response = api_client_bank1.post("/api/upload-csv/", {
            "tenant_id": "BANK001",
            "file_type": "loans",
            "loan_type": "RETAIL",
            "file": csv_file,
        }, format="multipart")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["message"] == "Upload successful"
        mock_post.assert_called_once()

    def test_upload_missing_tenant_id(self, api_client_bank1, tenant_bank1):
        """Missing tenant_id should return 400."""
        csv_file = self._create_csv_file()
        response = api_client_bank1.post("/api/upload-csv/", {
            "file_type": "loans",
            "loan_type": "RETAIL",
            "file": csv_file,
        }, format="multipart")
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_upload_invalid_file_type(self, api_client_bank1, tenant_bank1):
        """Invalid file_type should return 400."""
        csv_file = self._create_csv_file()
        response = api_client_bank1.post("/api/upload-csv/", {
            "tenant_id": "BANK001",
            "file_type": "invalid_type",
            "loan_type": "RETAIL",
            "file": csv_file,
        }, format="multipart")
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_upload_invalid_loan_type(self, api_client_bank1, tenant_bank1):
        """Invalid loan_type should return 400."""
        csv_file = self._create_csv_file()
        response = api_client_bank1.post("/api/upload-csv/", {
            "tenant_id": "BANK001",
            "file_type": "loans",
            "loan_type": "INVALID",
            "file": csv_file,
        }, format="multipart")
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_upload_missing_file(self, api_client_bank1, tenant_bank1):
        """Missing CSV file should return 400."""
        response = api_client_bank1.post("/api/upload-csv/", {
            "tenant_id": "BANK001",
            "file_type": "loans",
            "loan_type": "RETAIL",
        }, format="multipart")
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_upload_non_csv_file(self, api_client_bank1, tenant_bank1):
        """Non-CSV file extension should return 400."""
        f = io.BytesIO(b"some content")
        f.name = "test.txt"
        response = api_client_bank1.post("/api/upload-csv/", {
            "tenant_id": "BANK001",
            "file_type": "loans",
            "loan_type": "RETAIL",
            "file": f,
        }, format="multipart")
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    @patch("tenants.views.http_requests.post")
    def test_upload_external_bank_connection_error(
        self, mock_post, api_client_bank1, tenant_bank1
    ):
        """Connection error to External Bank should return 502."""
        import requests as req_lib
        mock_post.side_effect = req_lib.exceptions.ConnectionError("Connection refused")

        csv_file = self._create_csv_file()
        response = api_client_bank1.post("/api/upload-csv/", {
            "tenant_id": "BANK001",
            "file_type": "loans",
            "loan_type": "RETAIL",
            "file": csv_file,
        }, format="multipart")
        assert response.status_code == status.HTTP_502_BAD_GATEWAY


# =============================================================================
# Web UI Upload Page
# =============================================================================

class TestUploadCSVPage:
    """Tests for GET /upload/ (server-rendered page)."""

    def test_upload_page_requires_login(self, api_client, db):
        """Unauthenticated GET /upload/ should redirect to login."""
        response = api_client.get("/upload/")
        assert response.status_code == status.HTTP_302_FOUND
        assert "/login/" in response.url

    def test_upload_page_renders_for_authenticated(
        self, api_client, user_bank1, membership_bank1, tenant_bank1
    ):
        """Authenticated user should see the upload page."""
        api_client.login(username="bank1_user", password="Bank1Pass123!")
        response = api_client.get("/upload/")
        assert response.status_code == status.HTTP_200_OK
        content = response.content.decode()
        assert "Upload" in content or "upload" in content

    def test_upload_page_lists_only_own_tenants(
        self, api_client, user_bank1, membership_bank1, tenant_bank1, tenant_bank2
    ):
        """Upload page should list only tenants the user is a member of."""
        api_client.login(username="bank1_user", password="Bank1Pass123!")
        response = api_client.get("/upload/")
        content = response.content.decode()
        # Only BANK001 should be shown (user is only member of BANK001)
        assert "BANK001" in content
        assert "BANK002" not in content

    def test_upload_page_superuser_sees_all_tenants(
        self, api_client, superuser, tenant_bank1, tenant_bank2
    ):
        """Superuser should see all tenants on the upload page."""
        api_client.login(username="superadmin", password="SuperPass123!")
        response = api_client.get("/upload/")
        content = response.content.decode()
        assert "BANK001" in content
        assert "BANK002" in content

    def test_upload_page_no_membership_shows_warning(
        self, api_client, db
    ):
        """User with no tenant membership should see a warning message."""
        from django.contrib.auth import get_user_model
        User = get_user_model()
        User.objects.create_user(
            username="lonely_user", password="LonelyPass123!", email="lonely@test.com"
        )
        api_client.login(username="lonely_user", password="LonelyPass123!")
        response = api_client.get("/upload/")
        content = response.content.decode()
        assert "No tenants" in content or "no tenant" in content.lower()

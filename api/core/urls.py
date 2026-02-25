"""
Root URL Configuration for FSec SaaS Platform.
"""

from django.contrib import admin
from django.urls import path, include
from django.http import JsonResponse


def health_check(request):
    """Simple health check endpoint for Docker and load balancers."""
    return JsonResponse({"status": "healthy", "service": "adapter-api"})


urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/health/", health_check, name="health-check"),
    path("", include("django_prometheus.urls")),
    path("", include("tenants.urls")),
]

"""
Celery Application Configuration.
Handles periodic background tasks such as data sync checks.
"""

import os
from celery import Celery

# Set default Django settings module
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")

app = Celery("fsec")

# Load settings from Django conf with CELERY_ namespace
app.config_from_object("django.conf:settings", namespace="CELERY")

# Auto-discover tasks in installed apps
app.autodiscover_tasks()

# Explicitly include adapter task modules
app.conf.include = [
    "adapter.tasks.sync_tasks",
]

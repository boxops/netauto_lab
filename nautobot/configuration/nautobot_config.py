"""
Nautobot configuration file for netauto_lab.
Starts from Nautobot's defaults and overrides what we need.
Environment variables are injected via Docker Compose .env
"""

import os

from nautobot.core.settings import *  # noqa F401,F403
from nautobot.core.settings_funcs import is_truthy

##############################################################################
# Required settings
##############################################################################

ALLOWED_HOSTS = os.environ.get("NAUTOBOT_ALLOWED_HOSTS", "*").split(",")
SECRET_KEY = os.environ["NAUTOBOT_SECRET_KEY"]

# ── Database ──────────────────────────────────────────────────────────────────
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("NAUTOBOT_DB_NAME", "nautobot"),
        "USER": os.environ.get("NAUTOBOT_DB_USER", "nautobot"),
        "PASSWORD": os.environ["NAUTOBOT_DB_PASSWORD"],
        "HOST": os.environ.get("NAUTOBOT_DB_HOST", "nautobot-postgres"),
        "PORT": os.environ.get("NAUTOBOT_DB_PORT", "5432"),
    }
}

# ── Redis ─────────────────────────────────────────────────────────────────────
REDIS_HOST = os.environ.get("NAUTOBOT_REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("NAUTOBOT_REDIS_PORT", "6379"))
REDIS_PASSWORD = os.environ.get("NAUTOBOT_REDIS_PASSWORD", "")

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/0",
        "TIMEOUT": 300,
        "OPTIONS": {"CLIENT_CLASS": "django_redis.client.DefaultClient"},
    }
}

CELERY_BROKER_URL = f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/3"
CELERY_RESULT_BACKEND = f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/4"

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL = os.environ.get("NAUTOBOT_LOG_LEVEL", "INFO")
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "normal": {
            "format": "%(asctime)s.%(msecs)03d %(levelname)-8s %(name)-20s %(message)s"
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "normal",
        }
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django": {"handlers": ["console"], "level": LOG_LEVEL},
    },
}

# ── Plugins ───────────────────────────────────────────────────────────────────
# These plugins require installation in the Nautobot image before enabling.
# To enable, build a custom Nautobot image that pip-installs the plugins,
# then uncomment the desired entries below.
PLUGINS = [
    # "nautobot_golden_config",
    # "nautobot_device_lifecycle_mgmt",
    # "nautobot_bgp_models",
    # "nautobot_data_validation_engine",
]

PLUGINS_CONFIG = {
    # "nautobot_golden_config": { ... },
    # "nautobot_device_lifecycle_mgmt": { ... },
}

# ── Misc ──────────────────────────────────────────────────────────────────────
DEBUG = is_truthy(os.environ.get("NAUTOBOT_DEBUG", "False"))
EXEMPT_VIEW_PERMISSIONS = ["*"]

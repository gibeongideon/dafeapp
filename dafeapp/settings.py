from pathlib import Path

import environ

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
)
environ.Env.read_env(BASE_DIR / ".env")

# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------
SECRET_KEY = env("SECRET_KEY", default="django-insecure-change-me-in-production")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

# OAuth callbacks rely on the browser sending the same session cookie back to
# /accounts/<provider>/login/callback/. In local development browsers can be
# picky about cross-site redirects, so keep the cookie policy explicit.
SESSION_COOKIE_SAMESITE = env("SESSION_COOKIE_SAMESITE", default="Lax")
CSRF_COOKIE_SAMESITE = env("CSRF_COOKIE_SAMESITE", default="Lax")
SESSION_COOKIE_SECURE = env.bool("SESSION_COOKIE_SECURE", default=False)
CSRF_COOKIE_SECURE = env.bool("CSRF_COOKIE_SECURE", default=False)

# ---------------------------------------------------------------------------
# Installed Apps
# ---------------------------------------------------------------------------
INSTALLED_APPS = [
    # Daphne must be first so runserver uses ASGI (enables WebSockets in dev)
    "daphne",
    # Django built-ins
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    # Third-party
    "rest_framework",
    "rest_framework_simplejwt",
    "channels",
    "django_celery_beat",
    "django_celery_results",
    # Social Auth
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    "allauth.socialaccount.providers.github",
    "allauth.socialaccount.providers.gitlab",
    # Local apps
    "core.apps.CoreConfig",
    "users.apps.UsersConfig",
    "subscriptions.apps.SubscriptionsConfig",
    "tenants.apps.TenantsConfig",
    "cloud.apps.CloudConfig",
    "deployments.apps.DeploymentsConfig",
    "dns.apps.DnsConfig",
    "backups.apps.BackupsConfig",
    "monitoring.apps.MonitoringConfig",
    "audit.apps.AuditConfig",
    "organizations.apps.OrganizationsConfig",
]

SITE_ID = 1

# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "allauth.account.middleware.AccountMiddleware",        # must be after auth
    "organizations.middleware.OrganizationMiddleware",    # must be after auth
    "subscriptions.middleware.SubscriptionMiddleware",    # must be after OrganizationMiddleware
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "dafeapp.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "organizations.context_processors.organization",   # current_org, current_role, user_orgs
                "subscriptions.context_processors.subscription",  # subscription, plan, plan_limits
            ],
        },
    },
]

# ---------------------------------------------------------------------------
# ASGI / WSGI
# ---------------------------------------------------------------------------
ASGI_APPLICATION = "dafeapp.asgi.application"
WSGI_APPLICATION = "dafeapp.wsgi.application"

# ---------------------------------------------------------------------------
# Database – PostgreSQL
# ---------------------------------------------------------------------------
DATABASES = {
    "default": env.db(
        "DATABASE_URL",
        default="postgres://dafeapp:dafeapp@localhost:5432/dafeapp",
    )
}

# ---------------------------------------------------------------------------
# Redis / Channel Layers
# ---------------------------------------------------------------------------
REDIS_URL = env("REDIS_URL", default="redis://localhost:6379/0")

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [REDIS_URL],
        },
    }
}

# ---------------------------------------------------------------------------
# Celery
# ---------------------------------------------------------------------------
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = "django-db"          # stored via django_celery_results
CELERY_CACHE_BACKEND = "default"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"
CELERY_WORKER_HIJACK_ROOT_LOGGER = False
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
CELERY_BEAT_SCHEDULE = {
    "check-server-connectivity": {
        "task": "deployments.tasks.check_server_connectivity",
        "schedule": 60.0,  # every 1 minute
    },
    "check-instance-health": {
        "task": "deployments.tasks.check_instance_health",
        "schedule": 300.0,  # every 5 minutes
    },
    "auto-sync-instance-repos": {
        "task": "deployments.tasks.auto_sync_instance_repos",
        "schedule": 600.0,  # every 10 minutes
    },
    "reconcile-instance-domains": {
        "task": "deployments.tasks.reconcile_instance_domains",
        "schedule": 300.0,  # every 5 minutes
    },
}

TRAEFIK_DYNAMIC_CONFIG_DIR = env("TRAEFIK_DYNAMIC_CONFIG_DIR", default="/etc/traefik/dynamic")
TRAEFIK_ACME_STORAGE = env("TRAEFIK_ACME_STORAGE", default="/var/lib/traefik/acme.json")
TRAEFIK_ACME_EMAIL = env("TRAEFIK_ACME_EMAIL", default=env("ODOO_ADMIN_EMAIL", default="odoo@example.com"))
TRAEFIK_LOG_LEVEL = env("TRAEFIK_LOG_LEVEL", default="INFO")
TRAEFIK_VERSION = env("TRAEFIK_VERSION", default="3.1.2")
TRAEFIK_DEFAULT_TLS_MODE = env("TRAEFIK_DEFAULT_TLS_MODE", default="LETS_ENCRYPT")
PLATFORM_BASE_DOMAIN = env("PLATFORM_BASE_DOMAIN", default="dafeapp.com")
PLATFORM_DNS_PROVIDER = env("PLATFORM_DNS_PROVIDER", default="")
PLATFORM_DNS_API_TOKEN = env("PLATFORM_DNS_API_TOKEN", default="")
PLATFORM_DNS_ZONE_ID = env("PLATFORM_DNS_ZONE_ID", default="")
PLATFORM_DNS_PROXIED = env.bool("PLATFORM_DNS_PROXIED", default=False)

# ---------------------------------------------------------------------------
# Authentication Backends
# ---------------------------------------------------------------------------
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

# ---------------------------------------------------------------------------
# django-allauth configuration
# ---------------------------------------------------------------------------
ACCOUNT_LOGIN_METHODS = {"email"}         # replaces deprecated ACCOUNT_AUTHENTICATION_METHOD
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]  # replaces EMAIL_REQUIRED + USERNAME_REQUIRED
ACCOUNT_EMAIL_VERIFICATION = "none"       # DafeApp handles its own email verify
ACCOUNT_ADAPTER = "users.adapters.AccountAdapter"
SOCIALACCOUNT_ADAPTER = "users.adapters.SocialAccountAdapter"
SOCIALACCOUNT_STORE_TOKENS = True         # Keep OAuth tokens for API use
SOCIALACCOUNT_AUTO_SIGNUP = True
# GitHub `process=connect` only needs enough profile data to link the account
# and persist the token into users.VCSAccount. Avoid a hard dependency on
# /user/emails, which some tokens/accounts reject with 403.
SOCIALACCOUNT_QUERY_EMAIL = False

SOCIALACCOUNT_PROVIDERS = {
    "google": {
        "APP": {
            "client_id": env("GOOGLE_CLIENT_ID", default=""),
            "secret": env("GOOGLE_SECRET", default=""),
            "key": "",
        },
        "SCOPE": ["profile", "email"],
        "AUTH_PARAMS": {"access_type": "online"},
    },
    "github": {
        "APP": {
            "client_id": env("GITHUB_CLIENT_ID", default=""),
            "secret": env("GITHUB_SECRET", default=""),
            "key": "",
        },
        # We do not query /user/emails during connect, so profile read +
        # repository access is sufficient.
        "SCOPE": ["read:user", "repo"],
    },
    "gitlab": {
        "APP": {
            "client_id": env("GITLAB_CLIENT_ID", default=""),
            "secret": env("GITLAB_SECRET", default=""),
            "key": "",
        },
        "SCOPE": ["read_user", "api"],
        "GITLAB_URL": env("GITLAB_URL", default="https://gitlab.com"),
    },
}

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ---------------------------------------------------------------------------
# REST Framework
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
}

# ---------------------------------------------------------------------------
# Internationalisation
# ---------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------------
# Static / Media
# ---------------------------------------------------------------------------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "mediafiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Custom User Model
# ---------------------------------------------------------------------------
AUTH_USER_MODEL = "users.User"

# ---------------------------------------------------------------------------
# Authentication URLs
# ---------------------------------------------------------------------------
LOGIN_URL = "/auth/login/"
LOGIN_REDIRECT_URL = "/dashboard/"
LOGOUT_REDIRECT_URL = "/auth/login/"

# ---------------------------------------------------------------------------
# Email (console backend for development)
# ---------------------------------------------------------------------------
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
DEFAULT_FROM_EMAIL = "noreply@dafeapp.com"
SITE_URL = env("SITE_URL", default="http://localhost:8000")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "loggers": {
        "deployments.tasks": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "deployments.views": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "cloud.tasks": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "cloud.pyos": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "deployments": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "cloud": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "core": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "WARNING",
    },
}

# ---------------------------------------------------------------------------
# Field-level encryption (Fernet) — cloud credentials + VCS tokens
# ---------------------------------------------------------------------------
FIELD_ENCRYPTION_KEY = env("FIELD_ENCRYPTION_KEY", default="")

# ---------------------------------------------------------------------------
# OAuth provider credentials (env-only, never hardcoded)
# ---------------------------------------------------------------------------
GOOGLE_CLIENT_ID = env("GOOGLE_CLIENT_ID", default="")
GOOGLE_SECRET = env("GOOGLE_SECRET", default="")
GITHUB_CLIENT_ID = env("GITHUB_CLIENT_ID", default="")
GITHUB_SECRET = env("GITHUB_SECRET", default="")
GITLAB_CLIENT_ID = env("GITLAB_CLIENT_ID", default="")
GITLAB_SECRET = env("GITLAB_SECRET", default="")
GITLAB_URL = env("GITLAB_URL", default="https://gitlab.com")

# ---------------------------------------------------------------------------
# Extra built-ins
# ---------------------------------------------------------------------------
INSTALLED_APPS += ["django.contrib.humanize"]

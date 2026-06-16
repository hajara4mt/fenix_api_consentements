"""
Configuration centralisée du pipeline GRDF.

Toutes les variables sont lues depuis les variables d'environnement (App Settings
côté Azure Function App, .env côté local). Chaque variable a une valeur par défaut
raisonnable pour permettre le démarrage en local sans configuration.

Conventions :
  - Les noms en MAJUSCULES = constantes lues une fois à l'import du module
  - Les classmethods = URLs / chemins construits dynamiquement
  - validate() doit être appelée au démarrage de function_app.py

Pour changer une valeur en production :
  → portail Azure > Function App > Configuration > Application settings
  → pas besoin de redéployer.
"""

import os
from typing import Optional


# ----------------------------------------------------------------------
# Helpers de parsing des variables d'environnement
# ----------------------------------------------------------------------

def _get_bool(key: str, default: bool) -> bool:
    """Lit un booléen depuis l'env ('true'/'false', insensible à la casse)."""
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("true", "1", "yes", "on")


def _get_int(key: str, default: int) -> int:
    """Lit un entier depuis l'env, fallback sur la valeur par défaut si invalide."""
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_float(key: str, default: float) -> float:
    """Lit un float depuis l'env, fallback si invalide."""
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# ----------------------------------------------------------------------
# Classe Config
# ----------------------------------------------------------------------

class Config:
    """Configuration globale du pipeline GRDF."""

    # === Environnement ===
    ENVIRONMENT = os.environ.get("ENVIRONMENT", "preprod")  # preprod | prod | local

    # === Azure : ressources ===
    KEY_VAULT_NAME = os.environ.get("KEY_VAULT_NAME", "kv-fenix-preprod")
    STORAGE_ACCOUNT_NAME = os.environ.get("STORAGE_ACCOUNT_NAME", "stfenixforecast")
    CONTAINER_NAME = os.environ.get("CONTAINER_NAME", "fenixlake")
    GRDF_ROOT_FOLDER = os.environ.get("GRDF_ROOT_FOLDER", "grdf")

    # === GRDF : URLs et endpoints ===
    GRDF_OAUTH_URL = os.environ.get(
        "GRDF_OAUTH_URL",
        "https://adict-connexion.grdf.fr/oauth2/aus5y2ta2uEHjCWIR417/v1/token",
    )
    GRDF_API_BASE_URL = os.environ.get("GRDF_API_BASE_URL", "https://api.grdf.fr/adict/v2")
    # ⚠️ GRDF_SCOPE : à vérifier dans le flow NiFi. Le path actuel ressemble plus
    # à une URL qu'à un scope OAuth2 standard. Valeur provisoire.
    GRDF_SCOPE = os.environ.get("GRDF_SCOPE", "/adict/v2")

    # === GRDF : identité Energisme (constante côté partenaire) ===
    GRDF_PARTNER = os.environ.get("GRDF_PARTNER", "IFPEB")

    # === Key Vault : noms des secrets ===
    GRDF_CLIENT_ID_SECRET = os.environ.get("GRDF_CLIENT_ID_SECRET", "grdf-client-id")
    GRDF_CLIENT_SECRET_SECRET = os.environ.get("GRDF_CLIENT_SECRET_SECRET", "grdf-client-secret")

    # === Token OAuth2 : pattern lease ADLS anti-thundering-herd ===
    TOKEN_REFRESH_MARGIN_SECONDS = _get_int("TOKEN_REFRESH_MARGIN_SECONDS", 30)
    TOKEN_LEASE_DURATION_SECONDS = _get_int("TOKEN_LEASE_DURATION_SECONDS", 60)
    TOKEN_LEASE_RETRY_DELAY_SECONDS = _get_int("TOKEN_LEASE_RETRY_DELAY_SECONDS", 1)

    # === GRDF : throttling et retries (décisions GRDF-1) ===
    GRDF_RATE_LIMIT_PER_MIN = _get_int("GRDF_RATE_LIMIT_PER_MIN", 20)
    GRDF_REQUEST_PAUSE_MS = _get_int("GRDF_REQUEST_PAUSE_MS", 250)
    GRDF_HTTP_TIMEOUT_CONNECT_SECONDS = _get_float("GRDF_HTTP_TIMEOUT_CONNECT_SECONDS", 15.0)
    GRDF_HTTP_TIMEOUT_READ_DEFAULT_SECONDS = _get_float("GRDF_HTTP_TIMEOUT_READ_DEFAULT_SECONDS", 60.0)
    GRDF_HTTP_TIMEOUT_READ_LARGE_SECONDS = _get_float("GRDF_HTTP_TIMEOUT_READ_LARGE_SECONDS", 180.0)
    GRDF_HTTP_MAX_RETRIES = _get_int("GRDF_HTTP_MAX_RETRIES", 3)
    GRDF_HTTP_BACKOFF_BASE_SECONDS = _get_int("GRDF_HTTP_BACKOFF_BASE_SECONDS", 60)

    # === Paramètres batches (décisions atelier 20 mai) ===
    COLLECT_PUBLISHED_MONTHS_LOOKBACK = _get_int("COLLECT_PUBLISHED_MONTHS_LOOKBACK", 3)
    ONBOARD_HISTORY_YEARS = _get_int("ONBOARD_HISTORY_YEARS", 3)
    REPLAY_QUARTERLY_YEARS = _get_int("REPLAY_QUARTERLY_YEARS", 5)
    # Limites anti-timeout pour collect_published sur Flex Consumption (timeout dur ~30 min)
    COLLECT_PUBLISHED_MAX_PER_RUN = int(os.environ.get("COLLECT_PUBLISHED_MAX_PER_RUN", "400"))
    COLLECT_PUBLISHED_HARD_TIMEOUT_S = int(os.environ.get("COLLECT_PUBLISHED_HARD_TIMEOUT_S", "1500"))  # 25 min

    # === Paramètres batch declare_pce ===
    # 🔐 PREPROD : DRY_RUN=True (sécurisé, pas d'envoi réel d'email)
    # 🚀 PROD    : passer à False via App Setting au moment de la bascule
    DECLARE_PCE_DRY_RUN = _get_bool("DECLARE_PCE_DRY_RUN", True)
    DECLARE_PCE_DEFAULT_LIMIT = _get_int("DECLARE_PCE_DEFAULT_LIMIT", 10)
    DECLARE_PCE_MAX_RETRY_ATTEMPTS = _get_int("DECLARE_PCE_MAX_RETRY_ATTEMPTS", 3)
    DECLARE_PCE_PAUSE_MS = _get_int("DECLARE_PCE_PAUSE_MS", 1000)

    # === Paramètres batch onboard_history ===
    ONBOARD_HISTORY_DEFAULT_LIMIT = _get_int("ONBOARD_HISTORY_DEFAULT_LIMIT", 50)
    ONBOARD_HISTORY_PAUSE_MS = _get_int("ONBOARD_HISTORY_PAUSE_MS", 1000)

    # === Paramètres registry_dao (lease + cache parquet) ===
    REGISTRY_LEASE_DURATION_SECONDS = _get_int("REGISTRY_LEASE_DURATION_SECONDS", 60)
    REGISTRY_LEASE_RETRY_DELAY_SECONDS = _get_int("REGISTRY_LEASE_RETRY_DELAY_SECONDS", 1)
    REGISTRY_LEASE_MAX_WAIT_SECONDS = _get_int("REGISTRY_LEASE_MAX_WAIT_SECONDS", 40)
    REGISTRY_CACHE_TTL_SECONDS = _get_int("REGISTRY_CACHE_TTL_SECONDS", 60)

    # === URLs construites ===

    @classmethod
    def key_vault_url(cls) -> str:
        return f"https://{cls.KEY_VAULT_NAME}.vault.azure.net"

    @classmethod
    def storage_account_url(cls) -> str:
        return f"https://{cls.STORAGE_ACCOUNT_NAME}.blob.core.windows.net"

    # === Chemins ADLS ===

    @classmethod
    def token_blob_path(cls) -> str:
        """Chemin du fichier de cache du token GRDF (system/)."""
        return f"{cls.GRDF_ROOT_FOLDER}/system/token.json"

    @classmethod
    def droits_acces_blob_path(cls) -> str:
        """Chemin du registre des droits d'accès (silver/)."""
        return f"{cls.GRDF_ROOT_FOLDER}/silver/droits_acces.parquet"

    # === Helpers environnement ===

    @classmethod
    def is_prod(cls) -> bool:
        return cls.ENVIRONMENT.lower() == "prod"

    @classmethod
    def is_preprod(cls) -> bool:
        return cls.ENVIRONMENT.lower() == "preprod"

    # === Validation au démarrage ===

    @classmethod
    def validate(cls) -> None:
        """
        Valide la config au démarrage. Lève RuntimeError si une variable
        critique manque. À appeler depuis function_app.py au boot.
        """
        required = {
            "STORAGE_ACCOUNT_NAME": cls.STORAGE_ACCOUNT_NAME,
            "KEY_VAULT_NAME": cls.KEY_VAULT_NAME,
            "CONTAINER_NAME": cls.CONTAINER_NAME,
            "GRDF_OAUTH_URL": cls.GRDF_OAUTH_URL,
            "GRDF_API_BASE_URL": cls.GRDF_API_BASE_URL,
            "GRDF_PARTNER": cls.GRDF_PARTNER,
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise RuntimeError(
                f"Variables d'environnement manquantes ou vides : {missing}. "
                f"Vérifier les App Settings de la Function App."
            )
"""
shared/settings.py
──────────────────────────────────────────────────────────────────────────
Module central de configuration.

Lit les valeurs dans cet ordre de priorité :
  1. Variables d'environnement (PROD Azure → App Settings)
  2. Fichier config.py à la racine (LOCAL → dev)
  3. Valeur par défaut (si fournie)
  4. RuntimeError sinon

→ Le même code fonctionne en local et en prod, sans condition d'env.

USAGE :
    from shared.settings import (
        AZURE_STORAGE_CONNECTION_STRING,
        STORAGE_ACCOUNT_NAME,
        CONTAINER_NAME,
        SILVER_PATH,
        get_silver_table_uri,
        get_storage_options,
    )
"""

import os
import sys
from pathlib import Path
from typing import Any, Optional


# ─── Force la racine du projet dans le path Python ────────────────────
# (pour pouvoir trouver config.py qui est à la racine)
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ─── Chargement optionnel de config.py (dev local uniquement) ─────────
_local_config = None
try:
    import config as _local_config  # type: ignore
except ImportError:
    # Pas de config.py : on est en prod, ou config.py n'a pas encore été créé
    _local_config = None


def _get(name: str, default: Any = None, required: bool = True) -> Any:
    """
    Lit une variable selon l'ordre de priorité :
      1. os.environ[name]              (PROD Azure App Settings)
      2. config.<name>                 (LOCAL dev)
      3. default                       (fallback)
      4. RuntimeError                  (si required=True et rien trouvé)
    """
    # 1. Variable d'environnement
    value = os.environ.get(name)
    if value not in (None, ""):
        return value

    # 2. config.py local
    if _local_config is not None:
        value = getattr(_local_config, name, None)
        if value not in (None, ""):
            return value

    # 3. Default
    if default is not None:
        return default

    # 4. Erreur explicite
    if required:
        raise RuntimeError(
            f"❌ Variable de configuration '{name}' introuvable.\n"
            f"   • En PROD Azure : configure-la dans les App Settings de la Function App\n"
            f"   • En LOCAL      : ajoute-la dans config.py (à partir de config.example.py)"
        )

    return None


# ═══════════════════════════════════════════════════════════════════════════
# Variables exposées
# ═══════════════════════════════════════════════════════════════════════════

# ─── Azure Storage ────────────────────────────────────────────────────
AZURE_STORAGE_CONNECTION_STRING: str = _get("AZURE_STORAGE_CONNECTION_STRING")
STORAGE_ACCOUNT_NAME: str = _get("STORAGE_ACCOUNT_NAME", default="stfenixforecast")
CONTAINER_NAME: str = _get("CONTAINER_NAME", default="fenixlake")
BRONZE_PATH: str = _get("BRONZE_PATH", default="enedis/bronze")
SILVER_PATH: str = _get("SILVER_PATH", default="enedis/silver")

# ─── ENEDIS SOAP ──────────────────────────────────────────────────────
ENEDIS_LOGIN_UTILISATEUR: str = _get("ENEDIS_LOGIN_UTILISATEUR", required=False) or ""
ENEDIS_CONTRAT_ID: str = _get("ENEDIS_CONTRAT_ID", required=False) or ""
ENEDIS_SGE_BASE_URL: str = _get(
    "ENEDIS_SGE_BASE_URL",
    default="https://sge-b2b.enedis.fr",
)
ENEDIS_MTLS_CERT_PATH: Optional[str] = _get("ENEDIS_MTLS_CERT_PATH", required=False)
ENEDIS_MTLS_KEY_PATH: Optional[str] = _get("ENEDIS_MTLS_KEY_PATH", required=False)


# ─── Key Vault : Cert/Key servis sous forme de CONTENU (string PEM) ──
# Sur Azure, les certificats sont stockés dans Key Vault et référencés
# via les App Settings :
#   ENEDIS_MTLS_CERT_CONTENT = @Microsoft.KeyVault(SecretUri=https://kv-fenix-enedis.vault.azure.net/secrets/enedis-cert)
#   ENEDIS_MTLS_KEY_CONTENT  = @Microsoft.KeyVault(SecretUri=https://kv-fenix-enedis.vault.azure.net/secrets/enedis-key)
#
# Si ces variables sont définies, on écrit le contenu dans /tmp/ au démarrage
# et on l'utilise comme chemin pour httpx. /tmp est éphémère (RAM), donc :
#   - les certificats ne touchent jamais le disque persistant
#   - ils sont rechargés à chaque démarrage de la Function App
#   - si tu mets à jour le secret dans Key Vault, un redémarrage suffit

_ENEDIS_MTLS_CERT_CONTENT: Optional[str] = _get("ENEDIS_MTLS_CERT_CONTENT", required=False)
_ENEDIS_MTLS_KEY_CONTENT: Optional[str] = _get("ENEDIS_MTLS_KEY_CONTENT", required=False)


def _materialize_cert_from_content() -> None:
    """Si CERT_CONTENT / KEY_CONTENT sont définis (Key Vault), les écrit dans /tmp
    et override ENEDIS_MTLS_CERT_PATH / KEY_PATH.

    Cette fonction est appelée automatiquement à l'import de ce module.
    Elle est idempotente (skip si déjà fait).
    """
    global ENEDIS_MTLS_CERT_PATH, ENEDIS_MTLS_KEY_PATH

    import os
    import tempfile

    # Cert
    if _ENEDIS_MTLS_CERT_CONTENT:
        cert_path = os.path.join(tempfile.gettempdir(), "enedis_cert.pem")
        if not os.path.exists(cert_path):
            with open(cert_path, "w", encoding="utf-8") as f:
                f.write(_ENEDIS_MTLS_CERT_CONTENT)
            # Restreindre les permissions (lecture owner only)
            try:
                os.chmod(cert_path, 0o600)
            except Exception:
                pass  # Windows ne supporte pas chmod 0o600
        ENEDIS_MTLS_CERT_PATH = cert_path

    # Key
    if _ENEDIS_MTLS_KEY_CONTENT:
        key_path = os.path.join(tempfile.gettempdir(), "enedis_key.pem")
        if not os.path.exists(key_path):
            with open(key_path, "w", encoding="utf-8") as f:
                f.write(_ENEDIS_MTLS_KEY_CONTENT)
            try:
                os.chmod(key_path, 0o600)
            except Exception:
                pass
        ENEDIS_MTLS_KEY_PATH = key_path


# Matérialiser dès l'import (utile sur Azure où Key Vault est la source)
_materialize_cert_from_content()

# ─── Runtime ──────────────────────────────────────────────────────────
LOG_LEVEL: str = _get("LOG_LEVEL", default="INFO")

# ─── F1 SOAP mode (mock / real) ───────────────────────────────────────
# Lu via env var, fallback config.py, fallback 'mock' par défaut.
# En prod Azure : mettre F1_SOAP_MODE=real dans les App Settings.
F1_SOAP_MODE: str = _get("F1_SOAP_MODE", default="mock", required=False) or "mock"

# ─── Azure Storage Queues (retry queues pour F1) ──────────────────────
# 5 queues distinctes selon la matrice retry NiFi (validée par JB)
# Visibility timeout = délai avant que le message redevienne visible
# Max receive count = nb max de retries avant abandon
RETRY_QUEUE_DTC_TECH: str = "f1-retry-dtc-tech"           # SGT500/506 sur DTC : 10 min × 3
RETRY_QUEUE_RS_TECH: str = "f1-retry-rs-tech"             # SGT500/506 sur RS : 30 min × 3
RETRY_QUEUE_ATTENTE_P1: str = "f1-retry-attente-p1"       # SGT570 phase 1 : 2h × 5
RETRY_QUEUE_ATTENTE_P2: str = "f1-retry-attente-p2"       # SGT570 phase 2 : 12h × 10
RETRY_QUEUE_ACTIVATION: str = "f1-retry-activation"       # Activation non finale : 20 min × 1


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def get_storage_options() -> dict:
    """Retourne le dict storage_options pour la lib deltalake (backend Rust).

    ⚠️ Important : deltalake (Rust) n'utilise PAS 'azure_storage_connection_string'.
    Il faut lui passer le nom de compte ET la clé SÉPARÉMENT.
    """
    # On parse la connection string pour extraire AccountKey
    account_key = _extract_account_key(AZURE_STORAGE_CONNECTION_STRING)
    return {
        "azure_storage_account_name": STORAGE_ACCOUNT_NAME,
        "azure_storage_account_key": account_key,
    }


def _extract_account_key(conn_str: str) -> str:
    """Extrait la valeur de AccountKey= depuis une connection string."""
    if "AccountKey=" not in conn_str:
        raise RuntimeError(
            "AZURE_STORAGE_CONNECTION_STRING ne contient pas AccountKey="
        )
    return conn_str.split("AccountKey=")[1].split(";")[0]


def get_silver_table_uri(table_name: str) -> str:
    """Construit l'URI Delta complète pour une table Silver."""
    return (
        f"abfss://{CONTAINER_NAME}@{STORAGE_ACCOUNT_NAME}.dfs.core.windows.net/"
        f"{SILVER_PATH}/{table_name}"
    )


def get_bronze_path(subpath: str = "") -> str:
    """Construit un chemin Bronze (ex: pour stocker une réponse SOAP brute)."""
    base = (
        f"abfss://{CONTAINER_NAME}@{STORAGE_ACCOUNT_NAME}.dfs.core.windows.net/"
        f"{BRONZE_PATH}"
    )
    return f"{base}/{subpath}" if subpath else base


def print_config_summary() -> None:
    """Affiche un récap masqué de la config — utile pour debug."""
    source = "config.py (local)" if _local_config is not None else "variables d'env (prod)"
    print(f"📋 Config chargée depuis : {source}")
    print(f"   STORAGE_ACCOUNT_NAME = {STORAGE_ACCOUNT_NAME}")
    print(f"   CONTAINER_NAME       = {CONTAINER_NAME}")
    print(f"   BRONZE_PATH          = {BRONZE_PATH}")
    print(f"   SILVER_PATH          = {SILVER_PATH}")
    print(f"   F1_SOAP_MODE         = {F1_SOAP_MODE}")

    # Connection string masquée
    conn = AZURE_STORAGE_CONNECTION_STRING
    if "AccountKey=" in conn:
        parts = conn.split("AccountKey=")
        key_value = parts[1].split(";")[0]
        masked = f"{key_value[:6]}...{key_value[-6:]}"
        print(f"   AccountKey (masquée) = {masked} ({len(key_value)} car.)")
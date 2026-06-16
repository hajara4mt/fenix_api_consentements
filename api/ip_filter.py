"""
Filtrage IP applicatif de l'API FENIX (whitelist), qui renvoie le 403 JSON documenté.

Pourquoi un filtre dans l'app et pas seulement les Access Restrictions Azure ?
  → Les Access Restrictions plateforme renvoient un 403 générique NON
    personnalisable. Pour produire le corps JSON {"erreur":"IP_NON_AUTORISEE"...}
    du contrat, le rejet doit se faire ici (ou via APIM/Front Door au bord).

⚠️ Sécurité : X-Forwarded-For est spoofable. Un client peut préfixer son propre
   XFF ; Azure ajoute la VRAIE IP à la FIN. On lit donc la DERNIÈRE entrée, pas
   la première. Ce filtre n'est donc PAS un rempart de sécurité dur : pour ça,
   utiliser les Access Restrictions Azure (ou APIM/Front Door) en complément.
   Hypothèse : pas de reverse-proxy de confiance entre le client et la Function
   App (appel direct CUBE → Azure). Si on ajoute APIM/Front Door, revoir l'entrée
   XFF à retenir.

Config (App Settings) :
  - ALLOWED_IPS              : IPs et/ou CIDR séparés par des virgules
                               (ex: "52.10.0.1, 90.80.0.0/24")
  - ALLOW_ALL_WHEN_UNSET     : "true" (défaut) → whitelist vide = tout autorisé
                               (pratique en dev). "false" → whitelist vide = tout
                               refusé (prudent en prod).
"""

import ipaddress
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


def _parse_allowlist() -> list[ipaddress._BaseNetwork]:
    """Parse ALLOWED_IPS en réseaux (une IP seule devient un /32 ou /128)."""
    raw = os.environ.get("ALLOWED_IPS", "")
    networks: list[ipaddress._BaseNetwork] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            networks.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            logger.warning("Entrée ALLOWED_IPS ignorée (ni IP ni CIDR valide) : %r", token)
    return networks


def _allow_all_when_unset() -> bool:
    raw = os.environ.get("ALLOW_ALL_WHEN_UNSET", "true").strip().lower()
    return raw in ("true", "1", "yes", "on")


def _client_ip(req) -> Optional[str]:
    """
    Extrait l'IP cliente fiable depuis X-Forwarded-For.

    Format Azure : "spoofé?, ..., <vraie_ip>:<port>" — la dernière entrée est
    celle qu'Azure a ajoutée (la vraie IP source). On retire le port éventuel.
    """
    try:
        xff = req.headers.get("X-Forwarded-For", "") or ""
    except Exception:
        return None
    if not xff:
        return None

    last = xff.split(",")[-1].strip()
    if not last:
        return None

    # Retire ':port' pour un IPv4 'a.b.c.d:port' (un seul ':'). IPv6 littéral
    # entre crochets '[...]:port' géré aussi.
    if last.startswith("["):
        last = last[1:].split("]")[0]
    elif last.count(":") == 1:
        last = last.rsplit(":", 1)[0]

    try:
        return str(ipaddress.ip_address(last))
    except ValueError:
        logger.warning("IP cliente non parsable depuis X-Forwarded-For : %r", last)
        return None


def is_ip_allowed(req) -> bool:
    """True si l'IP cliente est autorisée par la whitelist (IP ou CIDR)."""
    allowlist = _parse_allowlist()

    if not allowlist:
        if _allow_all_when_unset():
            logger.warning(
                "ALLOWED_IPS non configuré — toutes les IP sont autorisées "
                "(dev only ; passer ALLOW_ALL_WHEN_UNSET=false en prod)."
            )
            return True
        logger.warning("ALLOWED_IPS vide et ALLOW_ALL_WHEN_UNSET=false — tout refusé.")
        return False

    ip_str = _client_ip(req)
    if ip_str is None:
        logger.warning("IP cliente indéterminée — accès refusé.")
        return False

    ip = ipaddress.ip_address(ip_str)
    if any(ip in net for net in allowlist):
        return True

    logger.warning("Accès refusé : IP %s non whitelistée", ip_str)
    return False

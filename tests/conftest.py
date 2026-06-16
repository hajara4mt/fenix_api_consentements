"""
Config pytest : pose une connection string factice AVANT tout import.

Le code Enedis vendoré (shared/settings.py) exige AZURE_STORAGE_CONNECTION_STRING
à l'import (sinon RuntimeError). Les tests mockent le stockage (adls_client), donc
la valeur n'est jamais réellement utilisée — il suffit qu'elle soit présente.
"""

import os

os.environ.setdefault(
    "AZURE_STORAGE_CONNECTION_STRING",
    "DefaultEndpointsProtocol=https;AccountName=dummy;AccountKey=ZHVtbXk=;EndpointSuffix=core.windows.net",
)

# FENIX API

API **FastAPI** (ASGI) entre l'application métier IFPEB (**CUBE**) et les
distributeurs d'énergie (**GRDF ADICT**, **SGE Enedis**). Déployée sur un
**Azure App Service Plan** (Linux, Python).

Cette API **ne contacte pas** les distributeurs : elle enregistre les
consentements dans le data lake (`droits_acces.parquet`) et expose les
consommations publiées (Silver). C'est le pipeline de batches
(`pipeline-grdf-preprod`) qui fait le travail réel (appels GRDF) la nuit.

## Lancer en local

```bash
python -m venv venv && source venv/Scripts/activate     # Windows Git Bash
pip install -r requirements.txt
az login                                                 # auth ADLS (DefaultAzureCredential)
python -m uvicorn main:app --reload
```
→ **`http://127.0.0.1:8000/docs`** (Swagger : teste toutes les routes) ·
`http://127.0.0.1:8000/api/grdf/droits-acces` (GET direct).

> Sans `az login`, la validation (400/403/404) répond déjà ; les routes ADLS
> (200 avec données réelles) nécessitent `az login` + RBAC **Storage Blob Data
> Contributor** sur `stfenixforecast`.

## Déploiement Azure App Service (Linux, Python 3.11)

1. **Startup Command** (Configuration → General settings) :
   ```
   gunicorn -w 4 -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:8000 --timeout 600 main:app
   ```
2. **Managed Identity** activée sur l'App Service + rôle RBAC **Storage Blob
   Data Contributor** sur `stfenixforecast` (c'est ce que `DefaultAzureCredential`
   utilise en prod, à la place de `az login`).
3. **App Settings** (variables d'environnement) :
   `ENVIRONMENT`, `STORAGE_ACCOUNT_NAME`, `CONTAINER_NAME`, `GRDF_ROOT_FOLDER`,
   `ALLOWED_IPS`, `ALLOW_ALL_WHEN_UNSET=false` (prod).
4. Déploiement du code via la méthode habituelle (zip deploy / GitHub Actions /
   az webapp up). Oryx installe `requirements.txt` automatiquement.

## Itération courante

| Méthode | Route | Statut |
|---|---|---|
| `POST` | `/api/grdf/droits-acces` | ✅ implémenté |
| `GET` | `/api/grdf/droits-acces/{id_pce}` | ✅ implémenté |
| `GET` | `/api/grdf/droits-acces` (liste) | ✅ implémenté |
| `PATCH` | `/api/grdf/droits-acces/{id_pce}` | ✅ implémenté |
| `POST` | `/api/grdf/droits-acces/{id_pce}/retry` | ✅ implémenté |
| `DELETE` | `/api/grdf/droits-acces/{id_pce}` | ✅ implémenté |

> CRUD GRDF complet. ✅

| `GET` | `/api/consommations` | ✅ implémenté (GRDF + Enedis) |
| `POST` | `/api/enedis/consent` | ✅ implémenté (table Delta `pdl`) |
| `GET` | `/api/enedis/consent/{id_pdl}` | ✅ implémenté |
| `GET` | `/api/enedis/consents` (liste) | ✅ implémenté |
| `PATCH` | `/api/enedis/consent/{id_pdl}` | ✅ implémenté |
| `POST` | `/api/enedis/consent/{id_pdl}/retry` | ✅ implémenté |
| `DELETE` | `/api/enedis/consent/{id_pdl}` | ✅ implémenté (révocation → `revoque`) |

> **Enedis** (en cours, route par route) : stockage **Delta Lake** (`deltalake`),
> registre = table `pdl` (`enedis/silver/pdl`). DAO vendoré
> (`shared/adls_client.py` + `schemas.py` + `settings.py`). Statuts exposés = les
> 7 internes (`nouveau`/`processing`/`traite`/`erreur`/`partiel`/`revoque`/`résilié`).
> Booléens stockés en `"true"`/`"false"`. `POST` fait : validation → 409 si
> `id_pdl` existe → `append_rows("pdl", …)` (ligne complète 22 colonnes, dont
> `platform_code` varchar(10) obligatoire & non modifiable, comme GRDF).

## GET /consommations

Lit les consommations d'un compteur depuis le Silver et renvoie les **périodes
brutes** (PAS d'agrégation mensuelle). **Forme de réponse IDENTIQUE quel que soit
le provider.**

- Query params : `provider` (**`grdf` ou `enedis`** → sinon `400`), `sensor_id`,
  `from`, `to` (YYYY-MM-DD, `to > from`).
- Sources selon `provider` :
  - **grdf** : `sensor_id` = `id_pce` brut → parquet `silver/consos_publiees/id_pce={sensor_id}/data.parquet` (intervalles bruts).
  - **enedis** : `sensor_id` = `id_pdl` (14 chiffres) → table Delta `enedis/silver/donnees_mesures`, lignes **`label == "TOTAL"` uniquement**, `val` mappée en `consommation` (**valeur brute**, aucune transformation). Filtre sur `id_pdl` (clé de partition).
- Filtre **« Contenu »** (identique) : on garde une période si `date_debut >= from`
  ET `date_fin <= to`.
- Réponse : `{ provider, sensor_id, from, to, data: [ {date_debut, date_fin,
  consommation, unite}, ... ] }`. `unite` = **kWh** (défaut).
- `403` · `404` `SENSOR_INTROUVABLE` (aucune donnée pour ce compteur) · données
  présentes mais fenêtre vide → `200` avec `data: []`.

```bash
curl -i "http://localhost:8000/api/consommations?provider=grdf&sensor_id=GI_TEST_0001&from=2024-01-01&to=2024-06-01"
curl -i "http://localhost:8000/api/consommations?provider=enedis&sensor_id=00000000001965&from=2024-01-01&to=2024-06-01"
```

## PATCH /grdf/droits-acces/{id_pce}

Mise à jour **partielle** d'un droit d'accès. **Whitelist stricte** de 12 champs
modifiables (les champs système/pipeline, `id_pce`, `partner`, `platform_code`,
`role_tiers` sont **non-modifiables**). Une modif remet le PCE en file :
`etat="nouveau"`, compteur
réinitialisé.

- **Modifiables** : `courriel_titulaire`, `code_postal`, `date_debut_droit_acces`,
  `date_fin_droit_acces`, `perim_donnees_conso_debut`, `perim_donnees_conso_fin`,
  `raison_sociale_du_titulaire`, `nom_titulaire`, les 4 `perim_donnees_*`.
- Validation **partielle + croisée** sur le merge (ex: patcher `date_fin` vérifie
  qu'elle reste `> date_debut` existant — aucun plafond de durée).
- `200` : `{ id_pce, statut:"nouveau", message, derniere_maj }`.
- `400` `CHAMP_NON_MODIFIABLE` (champ interdit) · `400` `CHAMP_INVALIDE` (format/règle)
  · `403` · `404` `PCE_INTROUVABLE`.

```bash
curl -i -X PATCH http://localhost:8000/api/grdf/droits-acces/GI_TEST_0001 \
  -H "Content-Type: application/json" -d '{"date_fin_droit_acces":"2028-05-01"}'
```

## POST /grdf/droits-acces/{id_pce}/retry

Relance un droit d'accès **en erreur** (sans body) : remet `etat_droit_acces` à
`nouveau`, **réinitialise** `nb_tentatives_declare` à 0 et efface
`message_erreur_declare` → vraie relance (budget de 3 tentatives propre dans
`declare_pce`). Le batch de nuit retentera.

- Relançable si l'état interne ∈ { `A revérifier`, `Refusée` } — sinon **409**.
- `200` : `{ id_pce, statut:"nouveau", message, derniere_maj }`.
- `403` `IP_NON_AUTORISEE` · `404` `PCE_INTROUVABLE` · `409` `STATUT_INCOMPATIBLE`.

```bash
curl -i -X POST http://localhost:8000/api/grdf/droits-acces/GI_TEST_0001/retry
```

## GET /grdf/droits-acces (liste paginée)

Query params (optionnels) : `statut` (un des 8 internes), `limit` (1–100, défaut 50),
`offset` (≥0, défaut 0). Réponse : `{ total, limit, offset, resultats: [...] }`.
Items en **forme allégée** (9 champs : sans `date_debut_droit_acces`/`date_creation`).

- Filtre `statut` = **égalité exacte** sur l'un des 8 statuts internes bruts
  (plus de reverse-mapping : `?statut=Révoquée` ne ramène QUE `Révoquée`).
- `400` `CHAMP_INVALIDE` (champ `statut`/`limit`/`offset`) — pagination **stricte**.
- Tri stable par `id_pce`. Pagination en mémoire (lecture complète du parquet).

```bash
curl -i "http://localhost:8000/api/grdf/droits-acces?statut=Active&limit=20&offset=0"
```

## GET /grdf/droits-acces/{id_pce}

Consulte le statut d'un droit d'accès. **Forme de réponse constante** (mêmes
champs toujours présents, `null` si non applicable).

`statut` ∈ les **8 états internes BRUTS** : `nouveau`, `A valider`, `A revérifier`,
`Active`, `Refusée`, `Révoquée`, `Obsolète`, `résilié` (exposés tels quels, **sans mapping**).

`message_erreur` : erreur GRDF **brute**, renvoyée telle quelle **sans aucun
mapping** (cf. `api/adict_messages.py`). C'est soit l'objet GRDF complet
(`{"code_statut_traitement": "...", "message_retour_traitement": "..."}`), soit
un message texte simple, soit `null` si pas d'erreur.

| Code | Cas |
|---|---|
| `200` | PCE trouvé |
| `403` | `IP_NON_AUTORISEE` |
| `404` | `PCE_INTROUVABLE` |

```json
{
  "id_pce": "GI12345678901234",
  "partner": "ifpeb",
  "platform_code": "PF01",
  "statut": "Active",
  "perim_donnees_contractuelles": true,
  "perim_donnees_techniques": true,
  "perim_donnees_informatives": false,
  "perim_donnees_publiees": true,
  "date_debut_droit_acces": "2026-05-01",
  "date_fin_droit_acces": "2029-05-01",
  "date_creation": "2026-04-20T10:00:00Z",
  "derniere_maj": "2026-04-21T04:01:30Z",
  "message_erreur": {
    "code_statut_traitement": "2000000010",
    "message_retour_traitement": "Une erreur technique est survenue."
  }
}
```

## POST /grdf/droits-acces

Dépose un nouveau droit d'accès PCE avec `etat_droit_acces="nouveau"`.

### Schéma de nommage (important)

Les noms de champs sont **identiques entre la route, le pipeline et la table de
stockage**. On n'utilise donc PAS les alias « consentement » du brouillon de doc
API mais les noms canoniques du storage :

| Concept | Nom canonique (route = pipeline = table) |
|---|---|
| Début / fin du droit | `date_debut_droit_acces` / `date_fin_droit_acces` |
| Raison sociale | `raison_sociale_du_titulaire` |

### Champs du body

**Obligatoires** : `id_pce` (≤ 20 car.), `partner` (≤ 50), `platform_code`
(≤ 10 car., **non modifiable** ensuite), `courriel_titulaire`
(email, ≤ 100), `code_postal` (5 chiffres), `date_debut_droit_acces`,
`date_fin_droit_acces` (> début), `perim_donnees_conso_debut`,
`perim_donnees_conso_fin`, et **au moins un** de `raison_sociale_du_titulaire`
(≤ 200) / `nom_titulaire` (≤ 40).

**Optionnels** (défauts) : `perim_donnees_contractuelles=true`,
`perim_donnees_techniques=true`, `perim_donnees_informatives=false`,
`perim_donnees_publiees=true` — avec la règle « au moins un périmètre à true ».

**Posés automatiquement par la route** (non saisis) :
- `role_tiers = "AUTORISE_CONTRAT_FOURNITURE"`
- `etat_droit_acces = "nouveau"`
- `date_creation`

### Réponses

| Code | Cas |
|---|---|
| `201` | Droit d'accès enregistré |
| `400` | `CHAMP_INVALIDE` (champ manquant/invalide, avec `champ`) |
| `403` | `IP_NON_AUTORISEE` |
| `409` | `PCE_EXISTANT` (avec le `statut` métier actuel) |

## Architecture

```
main.py                    # app FastAPI (ASGI) — entrée App Service / uvicorn
api/
  grdf_droits_acces.py     # handlers droits-acces (create/get/list/patch/retry/revoke)
  consommations.py         # handler GET /consommations (lecture Silver)
  consos_reader.py         # lecture consos GRDF (parquet) + Enedis (Delta donnees_mesures)
  validation.py            # règles de validation + normalisation (create + patch)
  ip_filter.py             # whitelist IP applicative (ALLOWED_IPS)
  adict_messages.py        # message_erreur GRDF brut (aucun mapping)
shared/                    # VENDORÉ depuis pipeline-grdf-preprod
  config.py
  registry_dao.py          # écriture lease-safe de droits_acces.parquet
```

Les routes FastAPI de `main.py` appellent les **mêmes handlers** (`api/`) que les
tests : `main.py` ne fait que l'adaptation HTTP (Request → handler → JSONResponse).

> ⚠️ `shared/` est une **copie** de `pipeline-grdf-preprod/shared/`. Toute
> évolution de `registry_dao` / `config` côté pipeline doit être reportée ici
> (ou, à terme, extraite en package partagé).

L'écriture du parquet passe **obligatoirement** par `registry_dao.insert`
(verrou lease ADLS) : c'est le même chemin que les batches, ce qui évite toute
corruption en cas de concurrence.

## DELETE /grdf/droits-acces/{id_pce}

**Révocation logique** — ne supprime PAS la ligne : passe `etat_droit_acces` à
`Révoquée` (via `registry_dao.upsert`, lease-safe). Statut exposé → `Révoquée` (brut).

> ⚠️ **Aucun batch ne résilie chez GRDF à ce jour.** La route enregistre la
> révocation dans le registre ; la résiliation effective côté ADICT devra être
> faite par un futur batch du pipeline. (À ne pas confondre avec
> `scripts/delete_pce.py` qui, lui, supprime physiquement la ligne — outil de
> nettoyage dev uniquement.)

- Révocable si l'état ∈ { `Active`, `nouveau`, `A valider` }.
- `200` : `{ id_pce, statut:"Révoquée", message, derniere_maj }`.
- `403` `IP_NON_AUTORISEE` · `404` `PCE_INTROUVABLE` · `404` `PCE_DEJA_REVOQUE`
  (état `Révoquée`/`résilié`) · `409` `STATUT_INCOMPATIBLE` (autre état).

```bash
curl -i -X DELETE http://localhost:8000/api/grdf/droits-acces/GI_TEST_0001
```

## Où s'écrit une déclaration ?

Il n'y a **pas de base SQL** : le registre est un fichier **Parquet** dans ADLS.
Un POST ajoute une ligne (`etat_droit_acces="nouveau"`) dans :

```
compte    : stfenixforecast        (STORAGE_ACCOUNT_NAME)
conteneur : fenixlake              (CONTAINER_NAME)
blob      : {GRDF_ROOT_FOLDER}/silver/droits_acces.parquet
```

C'est le **même fichier** que lit le pipeline. L'écriture est protégée par un
**lease ADLS** (`registry_dao._write_parquet_with_lease`).

## Développement local (preprod réel)

On teste contre le **vrai** compte `stfenixforecast` et le **vrai** dossier
`grdf/silver/` (`GRDF_ROOT_FOLDER=grdf`). Les PCE de test sont créés directement
dans le registre réel, puis **supprimés** après test (`scripts/delete_pce.py`).

> ⚠️ Un PCE de test laissé en `etat="nouveau"` sera ramassé par le batch
> `declare_pce` du pipeline (00h UTC). En preprod `DECLARE_PCE_DRY_RUN=True` →
> simple dry-run (pas d'email réel), mais utilise un `id_pce` clairement bidon et
> **supprime-le avant le prochain batch**.

**Prérequis** : `az login` avec un compte ayant le rôle RBAC
**Storage Blob Data Contributor** sur `stfenixforecast` (c'est ce que
`DefaultAzureCredential` utilise en local).

```bash
python -m venv venv && source venv/Scripts/activate     # Windows Git Bash
pip install -r requirements.txt
cp local.settings.json.example local.settings.json      # config locale (optionnel)
az login                                                # auth pour DefaultAzureCredential

python -m uvicorn main:app --reload                     # → http://127.0.0.1:8000/docs
```

### Tester le POST

```bash
curl -i -X POST http://localhost:8000/api/grdf/droits-acces \
  -H "Content-Type: application/json" \
  -d '{
    "id_pce": "GI_TEST_0001",
    "partner": "ifpeb",
    "platform_code": "PF01",
    "courriel_titulaire": "test@exemple.fr",
    "code_postal": "75001",
    "date_debut_droit_acces": "2026-05-01",
    "date_fin_droit_acces": "2029-05-01",
    "perim_donnees_conso_debut": "2023-01-01",
    "perim_donnees_conso_fin": "2029-05-01",
    "raison_sociale_du_titulaire": "Test SAS",
    "nom_titulaire": "Dupont"
  }'
```

Attendu : `201` avec `{"id_pce":"GI_TEST_0001","statut":"nouveau", ...}`.
Rejouer la même requête → `409 PCE_EXISTANT`.

```bash
curl -i http://localhost:8000/api/grdf/droits-acces/GI_TEST_0001   # → 200 (statut "nouveau")
curl -i http://localhost:8000/api/grdf/droits-acces/INCONNU        # → 404 PCE_INTROUVABLE
```

### Vérifier l'écriture réelle dans le parquet

```bash
python scripts/dump_droits_acces.py                 # liste tout le registre silver
python scripts/dump_droits_acces.py GI_TEST_0001    # détail du PCE créé
```

### Nettoyer après test

```bash
python scripts/delete_pce.py GI_TEST_0001           # retire la ligne du parquet silver
```

> `dump_droits_acces.py` (lecture) et `delete_pce.py` (suppression) opèrent sur
> le **même** `grdf/silver/droits_acces.parquet` que la route — la suppression
> passe par `registry_dao.delete` (écriture lease-safe).

## Tests

```bash
pytest tests/ -v
```

Les tests de validation ne nécessitent aucune dépendance Azure. Les tests du
handler mockent `registry_dao` (aucun accès ADLS réel).

## Sécurité — filtrage IP

Le filtrage IP est fait **dans l'app** (`api/ip_filter.py`) afin de renvoyer le
**403 JSON documenté** (`{"erreur":"IP_NON_AUTORISEE", ...}`) — les *Access
Restrictions* d'Azure, elles, renvoient un 403 générique non personnalisable.

Config (App Settings) :
- `ALLOWED_IPS` : IPs et/ou plages **CIDR**, séparées par des virgules
  (ex: `52.10.0.1, 90.80.0.0/24`).
- `ALLOW_ALL_WHEN_UNSET` : `true` (défaut) → whitelist vide = tout autorisé
  (dev). `false` → whitelist vide = tout refusé (**prod**).

⚠️ **`X-Forwarded-For` est spoofable** : on lit la **dernière** entrée (la vraie
IP ajoutée par Azure), pas la première. Ce filtre n'est donc **pas** un rempart
de sécurité dur. Pour une vraie barrière, le compléter avec les *Access
Restrictions* Azure, ou mettre **APIM / Front Door** au bord (qui peuvent aussi
filtrer par IP **et** renvoyer un JSON custom). Hypothèse actuelle : appel direct
CUBE → Azure, sans reverse-proxy de confiance intermédiaire ; si on ajoute
APIM/Front Door, revoir l'entrée XFF retenue dans `ip_filter._client_ip`.

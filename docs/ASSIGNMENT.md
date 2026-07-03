# Test technique — Backend Senior

## Contexte

Primmo ingère beaucoup de documents : un utilisateur upload un PDF dans l'environnement de son organisation, afin de le passer dans plusieurs pipelines d'analyse. Ce test est une version très simplifiée de ce flow. La plateforme Primmo est unique pour toutes les organisations.

Pas de piège caché : tout ce qui compte d'un point de vue fonctionnel est dans ce document. Tout ce que tu ferais, en tant que senior, sur un projet de production doit se retrouver dans le rendu. (code ou README)

## Besoin

Une API qui permet à un utilisateur appartenant à une **organisation** de :

- uploader un fichier afin de le faire traiter ;
- suivre l'avancement du traitement de ce fichier ;
- récupérer les données extraites une fois le traitement terminé ;
- lister les documents de son organisation (nom du document, utilisateur ayant fait l'import, statut).

Un partenaire externe doit également pouvoir nous notifier d'un événement asynchrone lié à un document via webhook (cf. section dédiée plus bas).

## Auth utilisateur

Authentification par JWT.

## Multi-tenant

Plusieurs organisations cohabitent sur la même instance. Les données d'une organisation ne sont jamais visibles d'une autre.

## Pipeline de traitement

Quatre étapes :

- **OCR** s'exécute en premier.
- **metadata** et **chunking** sont indépendantes entre elles, mais dépendent toutes les deux du résultat de l'OCR.
- **external_call** dépend des résultats de **metadata** et **chunking**. Elle simule un appel HTTP sortant vers un partenaire externe qui prend en charge la suite du traitement (indexation, validation compliance, publication, etc. — à toi de choisir le cas d'usage et de le décrire en 3 lignes dans le README). Le partenaire **ne répond pas synchroniquement** : il renvoie un `job_id` opaque, puis notifie le résultat plus tard via webhook (cf. section dédiée).

Tant que le webhook du partenaire n'est pas reçu et vérifié, le document n'est pas `ready`.

Code des étapes — **à reprendre tel quel**. Ne change ni les signatures, ni les `sleep`, ni la fréquence d'échec. Tu peux en revanche les wrapper, les appeler depuis un worker, les rendre async, etc.

```python
import random
import time
import uuid

def ocr() -> str:
    time.sleep(random.uniform(1, 15))
    if random.random() < 1/3:
        raise TimeoutError("OCR provider timeout")
    return "lorem ipsum..."

def metadata(text: str) -> dict:
    time.sleep(random.uniform(1, 10))
    if random.random() < 1/3:
        raise ValueError("metadata extraction failed")
    return {"doc_type": "fake_type"}

def chunking(text: str) -> list[str]:
    time.sleep(random.uniform(1, 12))
    if random.random() < 1/3:
        raise ValueError("chunking failed")
    return ["chunk_1", "chunk_2", "..."]

def external_call(doc_id: str, ocr_text: str, meta: dict, chunks: list[str]) -> str:
    """Simule l'appel HTTP sortant vers le partenaire.
    Retourne un job_id opaque. Le résultat réel arrive plus tard via webhook."""
    time.sleep(random.uniform(1, 5))
    if random.random() < 1/3:
        raise ConnectionError("partner unreachable")
    return f"j_{uuid.uuid4().hex[:16]}"
```

## Suivi de l'avancement

Le client doit pouvoir suivre l'avancement du traitement d'un document **avec une latence perçue de l'ordre de la seconde**. Chaque changement de statut d'une étape doit être remonté au client.

Tu choisis le mécanisme de transport. Justifie-le dans le README au regard de la cible de scale (charge sur l'API, ressources côté serveur, complexité côté client, robustesse aux déconnexions).

## Webhook entrant

Une fois `external_call` exécutée, le partenaire envoie sa notification asynchrone via un POST sur un endpoint que tu exposes. Voici la forme exacte de l'appel partenaire :

```bash
curl -X POST https://your-api.example.com/webhooks/partner \
  -H "Content-Type: application/json" \
  -H "X-Partner-Signature: <hex_hmac_sha256_du_body>" \
  -d '{
    "job_id": "j_abc123def4567890",
    "status": "completed",
    "result": { "indexed_at": "2026-05-21T14:23:11Z" },
    "occurred_at": "2026-05-21T14:23:11Z"
  }'
```

Le partenaire calcule `X-Partner-Signature` comme `HMAC-SHA256(body, PARTNER_HMAC_SECRET)` en hexadécimal. Le secret est partagé hors-bande (env var de ton choix, à documenter dans le README).

Pour les tests bout-en-bout, on déclenchera ces webhooks **manuellement depuis l'UI Swagger (`/docs`)**. Tu dois donc fournir un moyen de calculer une signature valide à partir d'un body (endpoint dev, script Python, instructions README — au choix). Sans ça, le webhook n'est pas testable depuis `/docs`.

## Cible de scale

- Aujourd'hui : ~1 000 documents traités par jour, ~50 utilisateurs concurrents.
- Cible 12 mois : ~100 000 documents par jour, ~5 000 utilisateurs concurrents, **p95 du temps total de pipeline < 2 min**.

Tes choix d'architecture (DB, orchestration async, transport temps réel, isolation tenant) doivent être justifiés au regard de cette cible dans le README. On ne te demande pas de l'**atteindre**, on te demande de montrer que ton design ne s'effondre pas en route.

## Stack imposée

- Python
- FastAPI
- une base de données relationnelle

Tout autre package / outil utile à la qualité de la solution est bienvenu (ORM, orchestrateur async, broker, etc.). Justifie tes choix dans le README.

Les assistants de code par IA (Copilot, Cursor, Claude Code, etc.) sont autorisés. Ce qu'on évalue, c'est le rendu — pas la façon dont tu l'as produit.

## Livrable

Un repo Git, qui inclut :

- un démarrage simple (`docker compose up` ou équivalent, sans manip exotique) ;
- le Swagger auto-généré par FastAPI (`/docs`) directement utilisable pour exercer l'API de bout en bout ;
- un jeu d'amorçage prêt à l'emploi : au moins deux organisations, chacune avec un utilisateur, pour pouvoir exercer l'API immédiatement ;
- un `README.md` qui explique tes choix d'architecture et leurs raisons, ainsi que ce que tu aurais fait avec plus de temps.

## Ce qui ne sera PAS évalué

- L'OCR / metadata / chunking réels (les mocks fournis suffisent)
- Le déploiement, la CI/CD
- L'UI (aucune attendue)
- La perf brute (pas de benchmark)

## Restitution

1h30, après évaluation préalable de ton rendu de notre côté. Au programme : questionnement sur le rendu, discussion des prochaines versions, et tout ce qui mérite d'être creusé.

Tu as 5 jours pour rendre l'exercice.

Bonne chance.

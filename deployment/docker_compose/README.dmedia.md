# Dmedia Onyx deployment notes

Upgrade procedure (osTicket fork): **[DMEDIA_ONYX_UPGRADE.md](../../DMEDIA_ONYX_UPGRADE.md)**

## Remotes (retrogres)

- `myfork` → fork `djschoone/onyx` (dmedia branches)
- `origin` → upstream `onyx-dot-app/onyx`

Always: `git fetch myfork` + `git reset --hard myfork/<branch>`.

## Compose (v4.3+)

Use **upstream base + thin overrides**. Do not run the old full Vespa `dmedia-gpu.yml` alone.

```bash
cd ~/danswer/deployment/docker_compose

docker compose -p danswer-stack \
  -f docker-compose.yml \
  -f docker-compose.dmedia-gpu.yml \
  -f docker-compose.dmedia-limits.yml \
  up -d
```

| File | Role |
| --- | --- |
| `docker-compose.yml` | Upstream stack (OpenSearch) |
| `docker-compose.dmedia-gpu.yml` | NVIDIA GPU for model servers |
| `docker-compose.dmedia-limits.yml` | Limits, external volumes, Redis persistence, ports |

## Preserved from old prod compose

- GPU on inference + indexing model servers
- Resource / pids limits
- External volumes: `danswer-stack_db_volume`, minio, redis, model caches, logs
- Persistent Redis + `maxmemory 1536mb` LRU (upstream default is ephemeral)
- No published ports for postgres / redis / minio
- Nginx on host **3000** only
- Auth / secrets stay in `.env` (old compose defaulted `AUTH_TYPE=disabled` — keep that in `.env` if needed)

## Required `.env` checks before v4.3 up

```bash
grep -E '^(IMAGE_TAG|AUTH_TYPE|COMPOSE_PROFILES|FILE_STORE_BACKEND|OPENSEARCH_ADMIN_PASSWORD|WEB_DOMAIN)=' .env
```

Expect at least:

- `IMAGE_TAG=dmedia-osticket-v4.3.9` (or similar fixed tag)
- `COMPOSE_PROFILES=s3-filestore` (MinIO is profile-gated in v4.3)
- `FILE_STORE_BACKEND=s3`
- Strong `OPENSEARCH_ADMIN_PASSWORD`
- `AUTH_TYPE` as you use in prod (often `disabled` or `basic`)

## Vespa → OpenSearch

Old `index` (Vespa) is gone. Document search index must be **re-indexed**.  
Postgres / MinIO / Redis volumes are reused; `danswer-stack_vespa_volume` can remain unused.

## Nginx template

Upstream no longer ships `app.conf.template.dev`. Default is `app.conf.template` (already in `docker-compose.yml`).

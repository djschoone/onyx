# Dmedia — Onyx upgrade guide (osTicket fork)

How to apply a new upstream Onyx release while keeping the custom **osTicket** connector.
Use this whenever the UI shows “Onyx vX.Y.Z is available” (or when you want to bump intentionally).

## Remotes & servers

| Where | Path / remote | Notes |
| --- | --- | --- |
| Local (WSL) | `/var/www/onyx` | `origin` = fork `djschoone/onyx`, `upstream` = `onyx-dot-app/onyx` |
| Retrogres | `~/danswer` | `myfork` = fork, `origin` = upstream official |
| Deploy dir | `~/danswer/deployment/docker_compose` | Custom compose overlays may exist (untracked) |

Branch naming convention:

```text
dmedia-osticket-upgrade-vX.Y.Z
```

Examples: `dmedia-osticket-upgrade-v3.3.0`, `dmedia-osticket-upgrade-v4.3.9`.

## Custom osTicket touchpoints (must survive every merge)

Keep these when resolving conflicts:

**Backend**

- `backend/onyx/connectors/osticket/` (connector package)
- `backend/onyx/configs/constants.py` — `DocumentSource.OSTICKET` (+ description map entry)
- `backend/onyx/connectors/registry.py` — `DocumentSource.OSTICKET` mapping

**Frontend**

- `web/public/OsTicket.svg`
- `web/src/components/icons/icons.tsx` — `OsTicketIcon`
- `web/src/lib/types.ts` — `ValidSources.OsTicket`
- `web/src/lib/sources.ts` — `osticket` metadata
- `web/src/lib/connectors/connectors.tsx` — config form + `OsTicketConfig`
- `web/src/lib/connectors/credentials.ts` — `osticket_api_key`

For all **other** conflict files: prefer upstream (`--theirs` during the merge).

---

## 1) Check which version is available

```bash
cd /var/www/onyx
git fetch upstream --tags
git tag --sort=-v:refname | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | head -20
```

Pick the newest **stable** tag you want (e.g. `v4.3.9`). Prefer release tags over `-beta` / `-cloud` unless you intentionally want those.

Also useful:

```bash
gh release list --repo onyx-dot-app/onyx --limit 15
```

---

## 2) Upgrade locally (merge upstream into a new branch)

Start from the current dmedia osTicket branch (or the last successful upgrade branch).

```bash
cd /var/www/onyx
git status   # clean working tree preferred
git checkout dmedia-osticket-upgrade-<CURRENT>
git pull origin dmedia-osticket-upgrade-<CURRENT>   # if tracking fork as origin

# Optional: backup custom files before a large jump
mkdir -p /tmp/osticket-upgrade-backup
cp -a backend/onyx/connectors/osticket /tmp/osticket-upgrade-backup/
cp web/public/OsTicket.svg /tmp/osticket-upgrade-backup/

NEW=v4.3.9   # <-- set target tag
git fetch upstream tag "$NEW" --no-tags
git checkout -b "dmedia-osticket-upgrade-${NEW#v}"
git merge "$NEW"
```

### Resolve conflicts

1. List conflicts:

   ```bash
   git diff --name-only --diff-filter=U
   ```

2. For every conflicted file that is **not** in the osTicket touchpoint list above:

   ```bash
   git checkout --theirs -- path/to/file
   git add -- path/to/file
   ```

   During `git merge <tag>`, **theirs** = upstream tag, **ours** = dmedia branch.

3. For osTicket touchpoint files: keep **both** new upstream connectors/enums **and** osTicket entries.
   Typical pattern in enums/registries:

   ```text
   # keep upstream additions (e.g. BRAINTRUST)
   # AND keep OSTICKET / OsTicket / osticket
   ```

4. Re-check osTicket is still wired:

   ```bash
   rg -n 'OSTICKET|OsTicket|osticket' \
     backend/onyx/configs/constants.py \
     backend/onyx/connectors/registry.py \
     web/src/lib/types.ts \
     web/src/lib/sources.ts \
     web/src/components/icons/icons.tsx \
     web/src/lib/connectors/connectors.tsx \
     web/src/lib/connectors/credentials.ts
   ls backend/onyx/connectors/osticket/ web/public/OsTicket.svg
   ```

5. Finish the merge:

   ```bash
   git add -A
   git commit -m "$(cat <<'EOF'
   chore(osticket): merge Onyx vX.Y.Z into connector branch

   Bring the custom osTicket connector branch up to Onyx vX.Y.Z while
   preserving the connector backend registration and frontend source metadata.
   EOF
   )"
   ```

Use [Conventional Commits](https://www.conventionalcommits.org/) for the subject line.

### Smoke-test locally (recommended)

Minimum: ensure the connector still imports / registers (Python via Docker if no local venv):

```bash
# Example: build backend image from local tree, then import-check
cd deployment/docker_compose
cp -n env.template .env   # only if missing; do not overwrite prod secrets
# set IMAGE_TAG=dmedia-osticket-vX.Y.Z-test in .env if you build local tags

docker compose -f docker-compose.yml -f docker-compose.dev.yml build api_server
docker compose -f docker-compose.yml -f docker-compose.dev.yml run --rm --no-deps api_server \
  python -c "from onyx.connectors.osticket.connector import OsTicketConnector; from onyx.configs.constants import DocumentSource; assert DocumentSource.OSTICKET; print('ok', OsTicketConnector)"
```

Full `up` is optional locally (port clashes with other stacks are common). Retrogres is the real deploy target.

---

## 3) Push the branch to the fork

On local WSL (`origin` = fork):

```bash
git push -u origin "dmedia-osticket-upgrade-${NEW#v}"
```

---

## 4) Apply on retrogres

On the server, the fork remote is named **`myfork`** (not `origin`).

```bash
cd ~/danswer
git status
git fetch myfork
git checkout "dmedia-osticket-upgrade-X.Y.Z"
# First time only, if the local branch does not exist yet:
#   git checkout -b dmedia-osticket-upgrade-X.Y.Z myfork/dmedia-osticket-upgrade-X.Y.Z

git reset --hard "myfork/dmedia-osticket-upgrade-X.Y.Z"
git branch --set-upstream-to="myfork/dmedia-osticket-upgrade-X.Y.Z"

git log -1 --oneline
git status
```

**Do not** reset to `origin/...` for dmedia branches — `origin` on retrogres is upstream official and usually does not have these branches.

### Rebuild / restart containers

From v4.3 onward, use **upstream `docker-compose.yml` + thin dmedia overrides** (GPU + limits).  
Do **not** use the old full-copy `docker-compose.dmedia-gpu.yml` that still had Vespa.

```bash
cd ~/danswer/deployment/docker_compose

# Fixed tag recommended (avoid bare `latest` for the osTicket fork)
sed -i 's/^IMAGE_TAG=.*/IMAGE_TAG=dmedia-osticket-vX.Y.Z/' .env
grep '^IMAGE_TAG=\|^AUTH_TYPE=\|^OPENSEARCH_ADMIN_PASSWORD=' .env

docker compose -p danswer-stack \
  -f docker-compose.yml \
  -f docker-compose.dmedia-gpu.yml \
  -f docker-compose.dmedia-limits.yml \
  build

docker compose -p danswer-stack \
  -f docker-compose.yml \
  -f docker-compose.dmedia-gpu.yml \
  -f docker-compose.dmedia-limits.yml \
  up -d

docker compose -p danswer-stack \
  -f docker-compose.yml \
  -f docker-compose.dmedia-gpu.yml \
  -f docker-compose.dmedia-limits.yml \
  ps
```

**Search engine:** v3.x used Vespa (`index`); v4.3+ uses OpenSearch. Postgres/chat data in `danswer-stack_*` volumes is kept; the document index must be **re-indexed**.

Also verify before `up`:

- `AUTH_TYPE` in `.env` (old full dmedia compose defaulted to `disabled`; upstream defaults to `basic`)
- `COMPOSE_PROFILES=s3-filestore` and `FILE_STORE_BACKEND=s3` (MinIO is profile-gated in v4.3)
- `OPENSEARCH_ADMIN_PASSWORD` set to a strong value
- Nginx: prod used host port **3000** + `app.conf.template.dev` — overrides keep port 3000; template is now upstream `app.conf.template`
- Redis: overrides keep **persistent** Redis + LRU (upstream v4.3 is ephemeral tmpfs by default)
- Volumes: `dmedia-limits` pins external `danswer-stack_*` volumes so Postgres/MinIO/Redis/model caches survive

If you deploy prebuilt Hub tags instead of building from git, bump `IMAGE_TAG` and `docker compose pull` — but for the osTicket fork you normally **build from this branch**.

---

## 5) Post-upgrade checklist

- [ ] UI loads; login works
- [ ] Admin → connectors: **osTicket** still listed
- [ ] Existing osTicket connector credential/config still present
- [ ] Trigger a small re-index / poll and confirm no crash in API/celery logs
- [ ] Version notifications no longer claim you are far behind (or dismiss old ones)
- [ ] `git status` on retrogres clean aside from known untracked dmedia files

---

## Upgrade history (dmedia)

| Target | Branch | Notes |
| --- | --- | --- |
| v3.3.0 | `dmedia-osticket-upgrade-v3.3.0` | First recorded merge of osTicket onto v3.3 |
| v4.3.9 | `dmedia-osticket-upgrade-v4.3.9` | Large jump v3.3 → v4.3; Vespa→OpenSearch; dmedia compose becomes thin overrides |

Add a row here after each successful production upgrade.

---

## Quick reference — “new version available”

```bash
# LOCAL
cd /var/www/onyx
git fetch upstream --tags
NEW=vX.Y.Z
git checkout dmedia-osticket-upgrade-<CURRENT>
git checkout -b "dmedia-osticket-upgrade-${NEW#v}"
git merge "$NEW"
# resolve conflicts (keep osTicket touchpoints; --theirs for the rest)
git push -u origin "dmedia-osticket-upgrade-${NEW#v}"

# RETROGRES
cd ~/danswer
git fetch myfork
git checkout "dmedia-osticket-upgrade-${NEW#v}"
git reset --hard "myfork/dmedia-osticket-upgrade-${NEW#v}"
cd deployment/docker_compose
# set IMAGE_TAG=dmedia-osticket-X.Y.Z in .env
docker compose -p danswer-stack \
  -f docker-compose.yml \
  -f docker-compose.dmedia-gpu.yml \
  -f docker-compose.dmedia-limits.yml \
  build
docker compose -p danswer-stack \
  -f docker-compose.yml \
  -f docker-compose.dmedia-gpu.yml \
  -f docker-compose.dmedia-limits.yml \
  up -d
```

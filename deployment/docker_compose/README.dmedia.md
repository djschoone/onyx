# Dmedia Onyx deployment notes

Custom dmedia/osTicket upgrade procedure lives at the repo root:

→ **[DMEDIA_ONYX_UPGRADE.md](../../DMEDIA_ONYX_UPGRADE.md)**

Use that document when Onyx shows a new version notification, or when bumping the fork on purpose.

## Retrogres remotes (reminder)

- `myfork` → `https://github.com/djschoone/onyx.git` (our fork — use this for dmedia branches)
- `origin` → `https://github.com/onyx-dot-app/onyx.git` (upstream official)

Always `git fetch myfork` + `git reset --hard myfork/<branch>`, never `origin/<dmedia-branch>`.

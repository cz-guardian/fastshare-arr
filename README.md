# fastshare-arr

Use your Fastshare account from the *arr stack. One small container that is two things:

1. **A download client** that speaks the SABnzbd API - add it to Sonarr, Radarr, Lidarr and Prowlarr as "SABnzbd".
   Queue, priorities, pause/resume, Range-resume, retries, category folders, zip/rar/7z unpacking (with passwords),
   history, hand-off to the arr importer. Works like NZBGet.
2. **A Newznab indexer** backed by Fastshare's own search API, so the arr apps can actually find releases.
   Its download link returns a tiny pseudo-NZB that the client above understands.

It uses the same JSON API as the official Kodi add-on (`plugin.video.fastshare`): login returns a session hash,
files are downloaded with the header `Cookie: FASTSHARE=<hash>`. Pure Python standard library; no dependencies.

## Run
    docker compose up -d --build
    docker logs fastshare-arr                       # shows the API key (also ./config/api_key)
    docker exec fastshare-arr python -m fastshare_arr check "some search term"

`check` logs in, prints your credit, and runs a search. Add `--probe` to also open the first result's download
link (this may cost credit).

## Wire it up
**Prowlarr** -> Indexers -> Add -> *Generic Newznab*: URL `http://fastshare-arr:8085/newznab`, API path `/api`,
API key = ours. Sync it to Sonarr/Radarr/Lidarr via Apps (or add it to each app directly).

**Sonarr / Radarr / Lidarr / Prowlarr** -> Settings -> Download Clients -> + -> *SABnzbd*:
host `fastshare-arr`, port `8085`, no SSL, URL base empty, API key = ours, category `tv` / `movies` / `music`.

Mount the same data path in all containers (e.g. `/data`) so imports and hardlinks work without remote path mapping.

## API key
Generated once on first start and stored in `CONFIG_DIR/api_key` (default `/config/api_key`, mode 600); every later
start reuses it. Show it with `docker exec fastshare-arr python -m fastshare_arr apikey`. Or pin your own with `API_KEY`.
`/config` must be a bind mount or named volume - without one the key (and queue) are lost when the container is
re-created, and a warning is logged if the key cannot be saved.

## File and directory ownership
Set `PUID` / `PGID` (same values as your Sonarr/Radarr containers) and `UMASK`. The container starts as root,
creates and hands over `/config` and the download folders, then drops to that user, so downloaded files, unpacked
files, the state file and the API key all belong to PUID:PGID. Existing files in the download folders are left
alone unless you set `CHOWN_RECURSIVE=true` once. If you start the container with Docker's `user:` option instead,
PUID/PGID are ignored (the container then cannot chown anything, so the mounted folders must already be writable).

## Domain
The Kodi add-on talks to `https://fastshare.cz`, which is the default. If your account lives on fastshare.cloud and
that host serves the same API, set `FASTSHARE_API_BASE=https://fastshare.cloud` and run `check`.

## Limits worth knowing
- Each search result is one file = one release. Multi-part RAR sets are not grouped into one release.
- The search API gives no IMDb/TVDB ids, so Prowlarr falls back to text queries (title + `S01E02`); matching depends
  on how files are named. Fastshare returns no upload date, so results show as "new".
- Credit rule is the same as the Kodi add-on: remaining credit (MB) must exceed the file size, or the job fails
  with a clear message. Unlimited accounts skip the check.
- The API takes your password as a URL query parameter (that is Fastshare's design). It is URL-encoded here and never
  written to logs.

## Settings (env)
`FASTSHARE_USER`, `FASTSHARE_PASS`, `FASTSHARE_API_BASE`, `FASTSHARE_ADULT` (false), `API_KEY`, `PORT` (8085),
`PUID`, `PGID`, `UMASK`, `CHOWN_RECURSIVE`, `COMPLETE_DIR`, `INCOMPLETE_DIR`, `CATEGORIES`, `MAX_ACTIVE_JOBS` (1), `SPEED_LIMIT_KBPS`, `EXTRACT`,
`DELETE_ARCHIVES`, `RETRIES` (4), `DEBUG`.

Tests (fake Fastshare API, whole flow search -> grab -> download -> unpack): `python -m unittest discover tests`.

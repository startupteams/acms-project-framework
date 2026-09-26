#!/usr/bin/env bash
# common.sh — shared helpers for ACMS release / rollback / validation tooling.
# Sourced by release.sh, rollback.sh, validate-release.sh (feature-delivery
# plan §4–§11: safe release transaction, automated rollback).
#
# Never prints secret values; .env key NAMES only. All mutable release state
# lives in $ACMS_RELEASES_DIR (default /opt/acms/releases), outside Git.

# ---------- strict mode ----------
set -euo pipefail

# ---------- configuration ----------
COMMON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$COMMON_DIR/.." && pwd)"
ACMS_ENV_FILE="${ACMS_ENV_FILE:-/opt/acms/.env}"
ACMS_RELEASES_DIR="${ACMS_RELEASES_DIR:-/opt/acms/releases}"
ACMS_BACKUPS_DIR="${ACMS_RELEASES_DIR}/backups"
ACMS_RELEASE_META_DIR="${ACMS_RELEASES_DIR}/releases"
ACMS_HISTORY_FILE="${ACMS_RELEASES_DIR}/history.jsonl"
ACMS_TRANSACTION_FILE="${ACMS_RELEASES_DIR}/transaction.json"
ACMS_MIN_FREE_MB="${ACMS_MIN_FREE_MB:-2048}"
ACMS_SMOKE_BASE_URL="${ACMS_SMOKE_BASE_URL:-https://10.0.20.122}"
ACMS_KEEP_BACKUPS="${ACMS_KEEP_BACKUPS:-10}"
ACMS_REQUIRE_SECRETS="${ACMS_REQUIRE_SECRETS:-ACMS_ADMIN_TOKEN ACMS_POSTGRES_PASSWORD ACMS_SESSION_SECRET ACMS_LDAP_URL ACMS_LDAP_USER_BASE}"

COMPOSE=(docker compose -f "$COMMON_DIR/compose.yaml" --env-file "$ACMS_ENV_FILE")

NGINX_CONF="$COMMON_DIR/reverse-proxy/nginx.conf"
NGINX_MAINT_CONF="$COMMON_DIR/reverse-proxy/nginx.maintenance.conf.template"
NGINX_SAVED_CONF="$ACMS_RELEASES_DIR/nginx.conf.pre-maintenance"

# ---------- logging ----------
_log()  { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
info()  { _log "INFO  $*"; }
warn()  { _log "WARN  $*"; }
error() { _log "ERROR $*"; }
die()   { error "$*"; exit 1; }

# ---------- release identifiers ----------
utc_now() { date -u +%Y%m%dT%H%M%SZ; }
utc_now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }

new_release_id() { printf '%s-%s' "$(utc_now)" "$(git -C "$REPO_DIR" rev-parse --short=7 HEAD)"; }

repo_git_sha() { git -C "$REPO_DIR" rev-parse HEAD; }
repo_clean() { [ -z "$(git -C "$REPO_DIR" status --porcelain)" ]; }

repo_version() { # semantic version of the currently checked-out tree
  sed -n 's/^__version__ = "\(.*\)"/\1/p' "$REPO_DIR/acms/__init__.py" | head -1
}

# ---------- filesystem state ----------
init_release_dirs() {
  mkdir -p "$ACMS_RELEASES_DIR" "$ACMS_BACKUPS_DIR" "$ACMS_RELEASE_META_DIR"
  chmod 700 "$ACMS_RELEASES_DIR" "$ACMS_BACKUPS_DIR" "$ACMS_RELEASE_META_DIR"
}

current_release_id() {
  [ -f "$ACMS_RELEASES_DIR/current" ] && tr -d '[:space:]' < "$ACMS_RELEASES_DIR/current" || true
}

previous_release_id() {
  [ -f "$ACMS_RELEASES_DIR/previous" ] && tr -d '[:space:]' < "$ACMS_RELEASES_DIR/previous" || true
}

release_meta_path() { printf '%s/%s.json' "$ACMS_RELEASE_META_DIR" "$1"; }

release_field() { # <release_id> <json-key> -> value (grep-based; no jq dependency)
  local meta; meta="$(release_meta_path "$1")"
  [ -f "$meta" ] || return 1
  sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$meta" | head -1
}

# ---------- history ledger (append-only JSONL; controlled values only) ----------
history_append() { # <action> <result> <k=v pairs...>
  local action="$1" result="$2"; shift 2
  local fields="" kv key val
  for kv in "$@"; do
    key="${kv%%=*}"; val="${kv#*=}"
    fields="$fields, \"$key\": \"$val\""
  done
  printf '{"ts": "%s", "action": "%s", "result": "%s"%s}\n' \
    "$(utc_now_iso)" "$action" "$result" "$fields" >> "$ACMS_HISTORY_FILE"
}

# ---------- health helpers ----------
app_container_direct_health() {
  # /health inside the app container (no published port; no curl in image).
  docker compose -f "$COMMON_DIR/compose.yaml" --env-file "$ACMS_ENV_FILE" exec -T acms-app \
    python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)" >/dev/null 2>&1
}

proxy_health() { # HTTPS through nginx (source IP = this VM; allowlisted)
  curl -kfsS --max-time 8 "$ACMS_SMOKE_BASE_URL/health" >/dev/null 2>&1
}

db_ready() {
  "${COMPOSE[@]}" exec -T postgres pg_isready -U acms -d acms >/dev/null 2>&1
}

db_alembic_revision() {
  "${COMPOSE[@]}" exec -T postgres psql -U acms -d acms -tAc \
    "SELECT version_num FROM alembic_version" 2>/dev/null | tr -d '[:space:]'
}

app_restart_count() {
  local cid
  cid="$("${COMPOSE[@]}" ps -q acms-app 2>/dev/null)" || return 0
  [ -n "$cid" ] || { echo 0; return 0; }
  docker inspect "$cid" --format '{{.RestartCount}}' 2>/dev/null || echo 0
}

app_log_error_scan() { # non-zero if repeated exceptions in recent app logs
  # Live-drill lesson (2026-09-26): grep -c exits 1 when the count is 0, and
  # pipefail would mark that "failure" — capture the count explicitly instead.
  local count
  count="$("${COMPOSE[@]}" logs --tail 100 acms-app 2>&1 | grep -cE "Traceback|SQLException|OperationalError" || true)"
  [ "${count:-0}" -eq 0 ]
}

wait_app_ready() { # block until /health passes direct (default 60s), else die
  local waited=0 deadline="${ACMS_APP_READY_SECONDS:-60}"
  until app_container_direct_health; do
    waited=$((waited + 2))
    [ "$waited" -ge "$deadline" ] && die "acms-app not healthy after ${deadline}s (live-drill lesson: validate only after readiness)"
    sleep 2
  done
  info "acms-app ready (waited ${waited}s)"
}

app_container_direct_get() { # <path> -> body of GET http://127.0.0.1:8000<path>
  docker compose -f "$COMMON_DIR/compose.yaml" --env-file "$ACMS_ENV_FILE" exec -T acms-app \
    python -c "import urllib.request,sys; sys.stdout.write(urllib.request.urlopen('http://127.0.0.1:8000$1', timeout=4).read().decode())"
}

# ---------- image build with build identity ----------
build_release_image() { # <git-sha-full> -> tags acms-app:<short-sha>
  # Drill lessons (2026-09-26 x2): NOTHING on stdout but the tag. compose
  # build goes to stderr, and so does every log line of this function —
  # command substitution captures stdout verbatim and any stray line
  # (including our own info()) pollutes the image tag.
  local sha="$1" short build_time
  short="$(printf '%s' "$sha" | cut -c1-7)"
  build_time="$(utc_now_iso)"
  info "Building image acms-app:$short (git sha $sha)" >&2
  if ! ACMS_BUILD_GIT_SHA="$sha" ACMS_BUILD_TIME="$build_time" ACMS_APP_IMAGE_TAG="$short" \
    "${COMPOSE[@]}" build acms-app >&2; then
    error "image build failed for acms-app:$short" >&2
    return 1
  fi
  if ! image_exists "acms-app:$short"; then
    error "image acms-app:$short missing after build" >&2
    return 1
  fi
  printf '%s' "$short"
}

image_exists() { docker image inspect "$1" >/dev/null 2>&1; }

# ---------- pre-deploy PostgreSQL backup (plan §5) ----------
create_verified_backup() { # <release_id> -> sets BACKUP_PATH BACKUP_SHA256 BACKUP_BYTES
  local release_id="$1"
  local path size magic
  init_release_dirs
  path="$ACMS_BACKUPS_DIR/${release_id}.dump"
  info "Creating pre-deploy backup: $path"
  if ! "${COMPOSE[@]}" exec -T postgres pg_dump -U acms -Fc acms > "$path"; then
    rm -f "$path"; die "pg_dump failed — refusing to continue (plan §5)"
  fi
  # Validate: non-empty, sane size floor, custom-format magic "PGDMP".
  [ -s "$path" ] || { rm -f "$path"; die "backup file is empty"; }
  size="$(stat -c %s "$path")"
  [ "$size" -ge 10240 ] || { rm -f "$path"; die "backup suspiciously small ($size bytes)"; }
  magic="$(head -c 5 "$path")"
  [ "$magic" = "PGDMP" ] || { rm -f "$path"; die "backup magic mismatch (not pg_dump -Fc output)"; }
  # Deep validation: pg_restore --list must parse the whole archive.
  if ! cat "$path" | "${COMPOSE[@]}" exec -T postgres pg_restore -U acms --list >/dev/null 2>&1; then
    rm -f "$path"; die "pg_restore --list failed on backup — refusing to continue"
  fi
  BACKUP_PATH="$path"
  BACKUP_BYTES="$size"
  BACKUP_SHA256="$(sha256sum "$path" | awk '{print $1}')"
  chmod 600 "$path"
  info "Backup verified: $BACKUP_BYTES bytes, sha256 $BACKUP_SHA256"
}

backup_checksum_matches() { # <path> <expected-sha256>
  local actual
  [ -s "$1" ] || return 1
  [ "$(head -c 5 "$1")" = "PGDMP" ] || return 1
  actual="$(sha256sum "$1" | awk '{print $1}')"
  [ "$actual" = "$2" ]
}

prune_old_backups() { # keep newest N; never touch current/previous release backups
  local keep="$ACMS_KEEP_BACKUPS" current prev
  current="$(current_release_id)"; prev="$(previous_release_id)"
  ls -1t "$ACMS_BACKUPS_DIR"/*.dump 2>/dev/null | tail -n +"$((keep + 1))" | while read -r f; do
    local id; id="$(basename "$f" .dump)"
    if [ "$id" = "$current" ] || [ "$id" = "$prev" ]; then continue; fi
    rm -f "$f"; info "pruned old backup $(basename "$f")"
  done
}

# ---------- maintenance mode (plan §6) ----------
# The maintenance template is copied OVER the tracked nginx.conf (same inode —
# directory mount makes the change visible to the running container) and nginx
# is reloaded. The app container is stopped during the window. Checkout hygiene:
# the tracked file is restored before any git checkout, then re-applied.
# Live-drill lesson (2026-09-26): a single-file bind mount pins the inode —
# never swap the conf via rename under a single-file mount; HUP then serves
# stale config forever. Reload is always guarded by `nginx -t` first, and the
# fallback uses `compose restart` (one container) rather than `up -d` (which
# re-evaluates depends_on and can recreate the app with the wrong image tag).
nginx_reload() {
  if ! "${COMPOSE[@]}" exec -T reverse-proxy nginx -t >/dev/null 2>&1; then
    "${COMPOSE[@]}" exec -T reverse-proxy nginx -t 2>&1 | head -5 >&2 || true
    die "nginx config test failed — refusing to reload"
  fi
  "${COMPOSE[@]}" kill -s HUP reverse-proxy >/dev/null 2>&1 \
    || "${COMPOSE[@]}" restart reverse-proxy >/dev/null 2>&1 \
    || die "could not reload reverse-proxy"
}

maintenance_on() {
  init_release_dirs
  cp "$NGINX_CONF" "$NGINX_SAVED_CONF"
  cp "$NGINX_MAINT_CONF" "$NGINX_CONF"
  nginx_reload
  "${COMPOSE[@]}" stop acms-app >/dev/null 2>&1 || true
  info "Maintenance mode ON (nginx 503 page, app stopped, writes blocked)"
}

maintenance_reapply() { # after a git checkout moved the tracked file
  [ -f "$NGINX_MAINT_CONF" ] || die "maintenance conf missing"
  cp "$NGINX_MAINT_CONF" "$NGINX_CONF"
  nginx_reload
  info "Maintenance mode re-applied after checkout"
}

maintenance_off() {
  # Restore tracked config: from git (target checkout) or saved copy fallback.
  if ! git -C "$REPO_DIR" checkout -- deploy/reverse-proxy/nginx.conf 2>/dev/null; then
    if [ -f "$NGINX_SAVED_CONF" ]; then
      cp "$NGINX_SAVED_CONF" "$NGINX_CONF"
    else
      die "cannot restore nginx.conf (git restore failed, no saved copy)"
    fi
  fi
  rm -f "$NGINX_SAVED_CONF"
  nginx_reload
  info "Maintenance mode OFF (normal proxy path re-enabled)"
}

maintenance_active() {
  grep -q "ACMS maintenance" "$NGINX_CONF" 2>/dev/null
}

# ---------- transaction state (the in-flight release, plan §8 guards) ----------
transaction_write() { # <release_id> <target-sha> <backup-path> <backup-sha256> <old-sha> <old-rev>
  init_release_dirs
  cat > "$ACMS_TRANSACTION_FILE" <<EOF
{"release_id": "$1", "target_git_sha": "$2", "backup_path": "$3", "backup_sha256": "$4", "previous_git_sha": "$5", "old_alembic_rev": "$6", "started_at": "$(utc_now_iso)"}
EOF
  chmod 600 "$ACMS_TRANSACTION_FILE"
}

transaction_clear() { rm -f "$ACMS_TRANSACTION_FILE"; }

transaction_active() { [ -f "$ACMS_TRANSACTION_FILE" ]; }

# ---------- preflight (plan §7 Phase A) ----------
preflight() { # <target-commit-sha>
  local target="$1" avail key

  info "Preflight: target commit is approved main history"
  git -C "$REPO_DIR" fetch origin main >/dev/null 2>&1 || die "git fetch origin main failed"
  git -C "$REPO_DIR" rev-parse --verify "$target^{commit}" >/dev/null 2>&1 \
    || die "target $target is not a valid commit"
  git -C "$REPO_DIR" merge-base --is-ancestor "$target" origin/main \
    || die "REFUSING: $target is not an ancestor of origin/main (plan §7)"

  info "Preflight: production working tree clean"
  repo_clean || die "production repo tree is dirty — refusing (plan §7)"

  info "Preflight: PostgreSQL healthy"
  db_ready || die "postgres is not healthy — refusing to deploy"

  info "Preflight: current ACMS health"
  app_container_direct_health || die "current release is unhealthy — fix before deploying"

  info "Preflight: disk space"
  init_release_dirs
  avail="$(df -Pm "$ACMS_RELEASES_DIR" | awk 'NR==2 {print $4}')"
  [ "$avail" -ge "$ACMS_MIN_FREE_MB" ] || die "only ${avail}MB free (need ${ACMS_MIN_FREE_MB}MB)"

  info "Preflight: required secret keys present (names only)"
  for key in $ACMS_REQUIRE_SECRETS; do
    grep -q "^${key}=" "$ACMS_ENV_FILE" || die "missing required secret key: $key (in $ACMS_ENV_FILE)"
  done

  info "Preflight: OK"
}

# ---------- release bookkeeping (plan §4 metadata) ----------
record_release() { # <release_id> <result> <sha> <image-tag> <rev_before> <rev_after> <backup-path> <backup-sha256> <backup-bytes> [rolled_back_from]
  local id="$1" result="$2" sha="$3" tag="$4" rev_before="$5" rev_after="$6" bpath="$7" bsha="$8" bbytes="$9" rbf="${10:-}"
  local meta version; meta="$(release_meta_path "$id")"; version="$(repo_version)"
  cat > "$meta" <<EOF
{"release_id": "$id", "git_sha": "$sha", "git_sha_short": "$(printf '%s' "$sha" | cut -c1-7)", "image_tag": "$tag", "version": "$version", "alembic_before": "$rev_before", "alembic_after": "$rev_after", "backup_path": "$bpath", "backup_sha256": "$bsha", "backup_bytes": "$bbytes", "result": "$result", "rolled_back_from": "$rbf", "recorded_at": "$(utc_now_iso)"}
EOF
  chmod 600 "$meta"
}

promote_release() { # <new-current-id> — old current becomes previous
  local old; old="$(current_release_id)"
  if [ -n "$old" ]; then printf '%s\n' "$old" > "$ACMS_RELEASES_DIR/previous"; fi
  printf '%s\n' "$1" > "$ACMS_RELEASES_DIR/current"
}
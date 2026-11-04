#!/usr/bin/env bash
# Makes a SEPARATE database for the "new on-demand call flow" demo: a copy of the database you give it,
# plus migrations/030_call_direct_payment.sql and 031_advance_payment.sql. The classic (wallet-hold) backend keeps using the original
# database untouched, so switching branches can never leave one flow's requests in the other's way.
#
#   scripts/new_flow_db.sh postgresql://postgres:postgres@127.0.0.1:5433/convoy_dev          # -> convoy_newflow
#   scripts/new_flow_db.sh postgresql://postgres:postgres@127.0.0.1:5433/convoy_dev my_copy  # custom name
#
# Prints the DATABASE_URL to start the new-flow backend with. Re-running refuses to overwrite an existing
# copy (drop it yourself first). Users keep their ids, so a phone that is logged in stays logged in on both.
set -euo pipefail

SRC="${1:?usage: $0 <source database url> [new database name]}"
NAME="${2:-convoy_newflow}"
[[ "$NAME" =~ ^[a-zA-Z0-9_]+$ ]] || { echo "bad database name: $NAME" >&2; exit 1; }

BASE="${SRC%/*}"            # postgresql://user:pass@host:port
SRC_NAME="${SRC##*/}"
SRC_NAME="${SRC_NAME%%\?*}"
QUERY=""; [[ "$SRC" == *\?* ]] && QUERY="?${SRC#*\?}"
ADMIN="$BASE/postgres$QUERY"
NEW="$BASE/$NAME$QUERY"
[[ "$SRC_NAME" != "$NAME" ]] || { echo "the copy must have a different name than the source" >&2; exit 1; }

LOG="$(mktemp)"
here="$(cd "$(dirname "$0")/.." && pwd)"

if [[ "$(psql "$ADMIN" -tAc "SELECT 1 FROM pg_database WHERE datname = '$NAME'")" == "1" ]]; then
  echo "database $NAME already exists; drop it first if you want a fresh copy (DROP DATABASE $NAME;)" >&2
  exit 1
fi

echo "Creating $NAME and copying $SRC_NAME into it (this reads the source, it changes nothing there)..."
psql "$ADMIN" -v ON_ERROR_STOP=1 -qc "CREATE DATABASE $NAME"
psql "$NEW" -v ON_ERROR_STOP=1 -qc "CREATE EXTENSION IF NOT EXISTS postgis"
pg_dump "$SRC" --no-owner --no-privileges | psql "$NEW" -v ON_ERROR_STOP=0 -q -o /dev/null 2>"$LOG" || true
if grep -E "ERROR" "$LOG" | grep -v "already exists" | head -3 | grep -q .; then
  echo "The copy reported errors (see $LOG):" >&2
  grep -E "ERROR" "$LOG" | grep -v "already exists" | head -5 >&2
  exit 1
fi

echo "Applying migrations/030_call_direct_payment.sql to the copy..."
psql "$NEW" -v ON_ERROR_STOP=1 --single-transaction -q -f "$here/migrations/030_call_direct_payment.sql"
echo "Applying migrations/031_advance_payment.sql to the copy..."
psql "$NEW" -v ON_ERROR_STOP=1 --single-transaction -q -f "$here/migrations/031_advance_payment.sql"

echo
echo "Done. Start the new-flow backend (branch feat/new-demand-call-flow) with:"
echo "  DATABASE_URL=$NEW"

#!/usr/bin/env bash
#
# Ingest the DA-1795 MDS QA pack files into a pilot's launchpad bucket.
#
# Order matters and is per the pack's 00-README section 3: each file depends on
# the previous one. BC_SCHEDULE must land before RAW, or RAW chunks fail with
# LINES_FAILED on a missing bill cycle.
#
# Each upload sets --metadata utility_file_name=<name>, which is what the
# ingestion pipeline reads to route/parse the file.
#
# The pilot and its bucket come from MDS_AUTOMATION/pilots.json -- nothing here
# is hardcoded to one pilot. The pack's files are named for the source pilot; a
# different target pilot gets them uploaded under names carrying its own id.
#
# Usage:
#   ./ingest_files.sh --pilot-id 88011 --dry-run   print the commands only
#   ./ingest_files.sh --pilot-id 88011             upload, 60s between files
#   MDS_PILOT_ID=88011 ./ingest_files.sh           pilot from the environment
#   ./ingest_files.sh --pilot-id 88011 --bucket X  override the bucket for one run

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FILES_DIR="$HERE/../DA-1795-MDS-QA-Pack/02-ingestion-files"
AUTOMATION_DIR="$HERE/../../MDS_AUTOMATION"
WAIT_SECONDS="${WAIT_SECONDS:-60}"

# The pilot id the pack's own filenames carry; replaced with the target pilot's.
SOURCE_PILOT_ID=88002

DRY_RUN=0
PILOT_ID="${MDS_PILOT_ID:-}"
BUCKET="${MDS_BUCKET:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)   DRY_RUN=1; shift ;;
    --pilot-id)  PILOT_ID="$2"; shift 2 ;;
    --pilot-id=*) PILOT_ID="${1#*=}"; shift ;;
    --bucket)    BUCKET="$2"; shift 2 ;;
    --bucket=*)  BUCKET="${1#*=}"; shift ;;
    -h|--help)   sed -n '2,21p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$PILOT_ID" ]]; then
  echo "no pilot selected. Pass --pilot-id <id>, or set MDS_PILOT_ID." >&2
  echo "Known pilots: $(python3 -c "
import sys; sys.path.insert(0, '$AUTOMATION_DIR')
import mds_config; print(', '.join(mds_config.known_pilots()))" 2>/dev/null || echo '(pilots.json unreadable)')" >&2
  exit 2
fi

# Resolve the bucket from pilots.json unless one was given explicitly.
if [[ -z "$BUCKET" ]]; then
  BUCKET="$(MDS_PILOT_ID="$PILOT_ID" python3 -c "
import sys; sys.path.insert(0, '$AUTOMATION_DIR')
import mds_config
try:
    print(mds_config.get('bucket'))
except mds_config.ConfigError as exc:
    sys.exit(str(exc))
")" || { echo "could not resolve the bucket for pilot $PILOT_ID" >&2; exit 2; }
fi
BUCKET_URI="s3://${BUCKET#s3://}"

# Ingestion order: BC Schedule -> USERENROLL -> METERENROLL -> RAW(D) -> RAW(R) -> INVOICE
FILES=(
  "BC_SCHEDULE_${SOURCE_PILOT_ID}_4_114520.txt"
  "USERENROLL_D_20260821_${SOURCE_PILOT_ID}_jorge_1.csv"
  "METERENROLL_D_20260821_${SOURCE_PILOT_ID}_JORGE_RATE_131318.csv"
  "RAW_D_900_S_${SOURCE_PILOT_ID}_JORGE_D_full_124014.txt"
  "RAW_D_900_S_${SOURCE_PILOT_ID}_JORGE_R_full_124014.txt"
  "INVOICE_${SOURCE_PILOT_ID}_JORGE_FEEDIN_120126.txt"
)

echo "pilot  : $PILOT_ID"
echo "bucket : $BUCKET_URI"
echo "source : $FILES_DIR"
echo "files  : ${#FILES[@]}   wait between: ${WAIT_SECONDS}s"
[[ "$PILOT_ID" != "$SOURCE_PILOT_ID" ]] && \
  echo "rename : ${SOURCE_PILOT_ID} -> ${PILOT_ID} in each uploaded file name"
[[ $DRY_RUN -eq 1 ]] && echo "mode   : DRY RUN (nothing uploaded)"
echo

total=${#FILES[@]}
step=0
for name in "${FILES[@]}"; do
  src_path="$FILES_DIR/$name"
  step=$((step + 1))

  if [[ ! -f "$src_path" ]]; then
    echo "[$step/$total] MISSING: $src_path" >&2
    exit 1
  fi

  # Upload under a name carrying the TARGET pilot id: utility_file_name drives routing.
  dest_name="${name//${SOURCE_PILOT_ID}/${PILOT_ID}}"
  echo "[$step/$total] $dest_name"

  cmd=(aws s3 cp "$src_path" "$BUCKET_URI/$dest_name" --metadata "utility_file_name=$dest_name")
  echo "        ${cmd[*]}"

  if [[ $DRY_RUN -eq 0 ]]; then
    "${cmd[@]}"
    echo "        uploaded"
  fi

  # No wait after the final file - nothing follows it.
  if [[ $step -lt $total ]]; then
    if [[ $DRY_RUN -eq 0 ]]; then
      echo "        waiting ${WAIT_SECONDS}s before next file..."
      sleep "$WAIT_SECONDS"
    else
      echo "        (would wait ${WAIT_SECONDS}s)"
    fi
  fi
  echo
done

echo "done: $total file(s)"

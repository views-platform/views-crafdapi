"""C-71 operator rollback gate: the quarantine blocklist and approval allowlist
(read from the environment at selection time so a bad upload can be rolled back or a
new one held back without a redeploy)."""
import os
from typing import Set


# C-71: operator-controlled rollback path. "Latest" is otherwise selected purely by
# newest `$createdAt`, so a wrong/partial/regression upload becomes live immediately with
# no way back short of deleting it from Appwrite. This env var is a quarantine blocklist
# (comma-separated bucket file IDs) excluded from selection: quarantine the bad file and
# the previous known-good one is served automatically — reversible, no deletion.
QUARANTINED_FILE_IDS_ENV = "APPWRITE_UNFAO_QUARANTINED_FILE_IDS"

# C-71 (proactive gate): an optional allowlist. When set (non-empty), ONLY these bucket
# file IDs are eligible for selection — a "promote to production" gate where a new upload
# is not served until explicitly approved. When unset/empty, selection is unrestricted
# (the default, behaviour-preserving). Complements the quarantine blocklist above.
APPROVED_FILE_IDS_ENV = "APPWRITE_UNFAO_APPROVED_FILE_IDS"


def _get_quarantined_file_ids() -> Set[str]:
    """Parse the quarantine blocklist (comma-separated bucket file IDs) from the
    environment. Read at selection time so an operator can roll back a bad upload
    without a redeploy. Empty/unset → no quarantine."""
    raw = os.getenv(QUARANTINED_FILE_IDS_ENV, "")
    return {fid.strip() for fid in raw.split(",") if fid.strip()}


def _get_approved_file_ids() -> Set[str]:
    """Parse the approval allowlist (comma-separated bucket file IDs) from the environment.
    Empty/unset → allowlist disabled (no restriction). Non-empty → only listed files are
    eligible for selection (proactive promote-to-production gate)."""
    raw = os.getenv(APPROVED_FILE_IDS_ENV, "")
    return {fid.strip() for fid in raw.split(",") if fid.strip()}

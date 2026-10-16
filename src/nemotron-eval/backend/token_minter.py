"""Short-lived, read-only storage tokens for governed sandboxes.

The user's own credentials are exchanged for a token limited by a Credential
Access Boundary: object reads on GOVERNED_BUCKETS only, whatever else the user
can do. The token goes to OpenShell as a provider credential, so the sandbox
holds a placeholder and the supervisor adds the real token on requests to
storage.googleapis.com that the policy allows. A stopgap: a platform would
issue a separate agent identity instead.
"""
import os
import time
from datetime import timezone
from typing import List, Tuple

BUCKETS: List[str] = [b.strip().removeprefix("gs://").strip("/")
                      for b in os.environ.get("GOVERNED_BUCKETS", "").split(",") if b.strip()]
ROLE = "inRole:roles/storage.objectViewer"


def mint() -> Tuple[str, float]:
    """Returns (token, expiry epoch seconds)."""
    import google.auth
    import google.auth.downscoped as downscoped
    import google.auth.transport.requests as transport

    source, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    boundary = downscoped.CredentialAccessBoundary(rules=[
        downscoped.AccessBoundaryRule(available_resource=f"//storage.googleapis.com/projects/_/buckets/{b}",
                                      available_permissions=[ROLE])
        for b in BUCKETS
    ])
    credentials = downscoped.Credentials(source_credentials=source, credential_access_boundary=boundary)
    credentials.refresh(transport.Request())
    if not credentials.expiry:
        return credentials.token, time.time() + 3600
    # google-auth returns naive UTC datetimes.
    return credentials.token, credentials.expiry.replace(tzinfo=timezone.utc).timestamp()


def endpoint_rules() -> str:
    """YAML rules for the storage endpoint: object list and read on the allowlisted buckets only."""
    lines = []
    for b in BUCKETS:
        lines += [f'          - allow: {{method: GET, path: "/storage/v1/b/{b}/o"}}',
                  f'          - allow: {{method: GET, path: "/storage/v1/b/{b}/o/**"}}',
                  f'          - allow: {{method: GET, path: "/download/storage/v1/b/{b}/o/**"}}']
    return "\n".join(lines)

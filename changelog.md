# Changelog

## Unreleased

### Fixed
- **CORS misconfiguration**: `main.py` combined `allow_origins=["*"]` with
  `allow_credentials=True`, which lets any origin read authenticated responses made
  with the browser's ambient credentials. The API only authenticates via a Bearer
  token in the `Authorization` header (never cookies — confirmed no
  `withCredentials`/`credentials: include` anywhere in `urolens-web`), so credentialed
  CORS was never needed. Set `allow_credentials=False`.
- **Hardcoded JWT signing-key fallback**: both `app/config.py` and
  `src/urolens/core/config.py` defaulted `JWT_SIGNING_KEY` to the literal string
  `"change-me-in-production-use-a-long-random-string"` if the env var was unset, so a
  misconfigured deployment would silently sign tokens with a public, guessable key.
  Both modules now raise `RuntimeError` at import time if `JWT_SIGNING_KEY` is unset
  or still equal to that placeholder.

These two fixes were pulled out of the in-progress backend consolidation
(`docs/backend-consolidation-plan.md`) and shipped independently since they're
single-file, don't depend on any of the consolidation's open decisions (which
service layer survives, which RBAC import wins), and were live security gaps.

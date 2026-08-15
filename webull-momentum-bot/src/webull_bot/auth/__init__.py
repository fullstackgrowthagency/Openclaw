"""Multi-tenant auth: password hashing (security.py), broker-credential
encryption at rest (crypto.py), the current-user FastAPI dependency
(dependencies.py), and the /api/auth/* signup/login/logout routes
(routes.py). See docs/ARCHITECTURE.md's "Multi-tenant auth" section for
the full design (session mechanism, why there's no email/password-reset
flow in v1, and how this replaced the single-.env-account model)."""

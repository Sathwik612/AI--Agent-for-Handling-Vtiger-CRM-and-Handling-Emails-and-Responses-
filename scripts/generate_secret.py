"""Print a random secret suitable for APP_SECRET_KEY / database passwords."""

import secrets

print(secrets.token_urlsafe(48))

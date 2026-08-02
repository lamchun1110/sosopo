# Contributing to Sosopo

Thank you for improving Sosopo. For bugs, include the Compose command, the database backend, redacted logs, and the smallest reproducible sequence. Do not include access tokens, cookies, uploaded media, database backups, or `.env` files in an issue.

Before opening a pull request, run:

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
docker compose config --quiet
docker compose up -d --build
curl -fsS http://127.0.0.1:8088/api/health
```

Keep changes narrowly scoped, add regression tests for behavior changes, update README configuration where necessary, and use provider sandbox credentials only. Do not add real provider tokens to tests, fixtures, image layers, or commits.

For security-sensitive changes, follow [SECURITY.md](SECURITY.md) instead of opening a public issue.

# LawNeeds backend demo

FastAPI source for the LawNeeds demo. Includes API routes, services, models, runtime assets, and background-job modules. Tests, historical migrations, reports, and maintenance utilities have been removed.

## Local setup

Use Python 3.11 and Docker. From this directory:

```sh
cp .env.example .env
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
docker compose up -d
uvicorn app:app --reload --port 8000
```

Set a random `SECRET_KEY` in `.env` before starting. Use a fresh local database only. Tables are created from SQLAlchemy models at startup when `SKIP_AUTO_CREATE_TABLES` is unset; this does not upgrade an existing database. Optional subscription plan seeding is retained in `seed_subscription_plans.py`.

Open http://localhost:8000/docs for the API. Background scheduling is disabled in the example environment.

This is application source, not an offline mock. AI/chat, authentication providers, email, Firebase, storage, identity verification, calls, and payments require your own service configuration; some services may require credentials at startup. Use sandbox credentials and fictional data. A complete end-to-end demo has not been verified. Review remaining source and assets before public publication.

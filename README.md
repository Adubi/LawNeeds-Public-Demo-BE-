# LawNeeds Backend

LawNeeds is a legal services platform connecting clients with legal professionals. This repository contains the backend API for account management, legal enquiries, referrals, consultations, casework, messaging, and payments.

The application is built with FastAPI and SQLAlchemy, with PostgreSQL for persistent data and Redis for temporary state. It supports a companion React Native mobile application through HTTP endpoints and WebSocket connections.

## Features

- **Accounts and authentication:** client and professional registration, authentication, email verification, password resets, and OAuth integration.
- **Legal assistance:** AI-assisted conversations, knowledge-base retrieval, document processing, and speech features.
- **Referrals and casework:** legal service requests, professional matching, acceptance workflows, consultation categories, and casework budgets.
- **Consultations:** booking, session status, timers, extensions, and professional fee proposals.
- **Communication:** inbox messaging, attachments, WebSocket updates, notifications, and call integration.
- **Billing:** Stripe payments and Connect integration, subscriptions, and usage entitlements.
- **Platform operations:** identity verification, reviews, feedback, administrative endpoints, analytics, and scheduled jobs.

## Technology

| Area | Implementation |
| --- | --- |
| API | Python, FastAPI, Uvicorn |
| Database | PostgreSQL, SQLAlchemy |
| Temporary state | Redis |
| AI and retrieval | OpenAI, LangChain, Pinecone / Chroma integrations |
| File storage | Amazon S3 |
| Payments | Stripe and Stripe Connect |
| Notifications | Firebase |
| Identity verification | Persona |
| Calls | Agora |
| Background scheduling | APScheduler |

## Repository structure

```text
api/                         HTTP and WebSocket handlers
config/                      Environment and application configuration
jobs/                        Scheduled background tasks
services/                    Business logic, integrations, models, and assets
app.py                       FastAPI entry point and application lifecycle
seed_subscription_plans.py    Subscription plan initialisation
requirements.txt             Python dependencies
.env.example                 Local environment template
Dockerfile                   API container definition
docker-compose.yml          Local PostgreSQL and Redis services
```

## Requirements

- Python 3.11
- Docker with Docker Compose for the local database and Redis services
- Credentials for the external integrations used by the selected demo features
- A fresh PostgreSQL database dedicated to the demo

## Local installation

Run the following commands from the repository root:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Generate a local signing secret:

```sh
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Set `SECRET_KEY` in `.env` to the generated value. Configure the external services required by the demo before starting the API.

Start PostgreSQL and Redis:

```sh
docker compose up -d
```

Once PostgreSQL is accepting connections, initialise the schema and subscription plans:

```sh
python seed_subscription_plans.py
```

Start the API:

```sh
uvicorn app:app --reload --port 8000
```

| Resource | Local address |
| --- | --- |
| API | http://localhost:8000 |
| Swagger UI | http://localhost:8000/docs |
| ReDoc | http://localhost:8000/redoc |
| OpenAPI schema | http://localhost:8000/openapi.json |

The companion frontend should use this API address. Physical devices require the computer's reachable LAN address instead of `localhost`; serving the API to another device also requires binding Uvicorn to an appropriate interface, such as `--host 0.0.0.0`.

## Environment configuration

| Variable | Purpose |
| --- | --- |
| `ENVIRONMENT` | Application environment; the example uses `local`. |
| `DATABASE_URL` | PostgreSQL connection string for the demo database. |
| `REDIS_URL` | Redis connection URL. |
| `SECRET_KEY` | Secret used by authentication code. |
| `PAYMENTS_ENV` | Payment environment; the example uses `sandbox`. |
| `STRIPE_API_KEY` | Stripe sandbox secret key for payment features. |
| `SCHEDULER_ENABLED` | Enables background scheduling; disabled in the example. |
| `SKIP_AUTO_CREATE_TABLES` | When set, disables automatic table creation. Leave unset for a fresh demo database. |

The example file covers local infrastructure and core settings. AI, email, storage, OAuth, Firebase, Persona, and Agora require additional provider configuration in their respective service modules. Some integrations initialise during application startup, so an incomplete configuration can prevent the API from starting.

Stripe payment flows require matching sandbox credentials in the backend and frontend, plus the appropriate webhook configuration. Environment files and provider credentials are excluded from version control.

## Database behaviour

The database layer creates missing tables from the SQLAlchemy models when automatic creation is enabled. Historical migrations are not included in this demo repository. Automatic table creation does not update the structure of existing tables; use a fresh database for this source snapshot.

The subscription seed command inserts missing plan definitions and leaves existing plans unchanged. To inspect which plans would be inserted:

```sh
python seed_subscription_plans.py --check
```

The check still initialises the database layer, which may create missing tables when automatic creation is enabled.

## Demo scope

This repository retains the application routes, business logic, models, runtime assets, and integration code. Historical migrations, automated tests, assessment reports, and maintenance scripts are omitted.

The application uses real service integrations rather than mock responses. End-to-end features depend on configured providers and sandbox accounts. The reduced source snapshot has not been validated through a complete installation and end-to-end run.

# Dental Bill Detective v2: https://youtu.be/2iXo3ooZv54

An OpenClaw skill for ClawHub that audits dental bills for overcharges, duplicate charges, unbundled procedure codes, and upcoding fraud.

## What it does

1. User sends a dental bill photo or PDF via Telegram
2. OCRs the bill to extract CDT procedure codes and billed amounts
3. Scrapes live FairHealth + CMS pricing benchmarks via Apify
4. Uploads bill + pricing data to a Contextual AI RAG datastore
5. Queries the Contextual AI agent for multi-hop audit reasoning
6. Verifies user identity via Civic before storing any PHI
7. Caches insurance plan + bill history in Redis
8. Returns: overcharge report, itemized disputes, phone script, appeal letter PDF

## Quick start

### 1. Copy environment variables

```bash
cp example.env .env
# Fill in your API keys
```

### 2. Run setup notebook

```bash
# Activate the project venv
source ../../actor/bin/activate

# Launch Jupyter
jupyter notebook notebooks/setup.ipynb
```

Run all 4 parts:
- Part 1: Create Contextual AI datastore + agent (copy IDs to `.env`)
- Part 2: Build + launch Docker services
- Part 3: Test a sample bill query
- Part 4: Send a Telegram test message

### 3. Start services

```bash
docker compose up --build
```

## Architecture

See [AGENTS.md](AGENTS.md) for full architecture, API patterns, and gotchas.

## OpenClaw skill

See [SKILL.md](SKILL.md) for the ClawHub skill definition.

## Environment variables

| Variable | Description |
|----------|-------------|
| `CONTEXTUAL_API_KEY` | Contextual AI API key |
| `DATASTORE_ID` | Created in setup.ipynb Part 1 |
| `AGENT_ID` | Created in setup.ipynb Part 1 |
| `APIFY_API_TOKEN` | Apify scraping API token |
| `REDIS_URL` | Redis connection URL |
| `CIVIC_API_KEY` | Civic identity verification key |
| `ANTHROPIC_API_KEY` | Anthropic API key (Claude) |
| `TELEGRAM_BOT_TOKEN` | From @BotFather |
| `TELEGRAM_CHAT_ID` | Your Telegram numeric chat ID |

## Sponsor integrations

- **Contextual AI** — RAG datastore + agent for multi-hop billing analysis
- **Apify** — Web scraping FairHealth + CMS dental fee schedules
- **Civic** — PHI-safe identity verification gate
- **Redis** — Persistent user insurance plan + bill history
- **Anthropic** — Claude tool-use reasoning loop

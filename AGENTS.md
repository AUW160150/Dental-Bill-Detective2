# AGENTS.md

## Project Overview

**Dental Bill Detective v2** is an OpenClaw skill for ClawHub that audits dental bills for overcharges, duplicate codes, unbundling fraud, and upcoded procedures. A user sends a dental bill photo or PDF via Telegram; the system OCRs it, scrapes live CDT pricing benchmarks, uploads everything to a Contextual AI RAG datastore, and returns a structured dispute report with an itemized appeal letter.

Identity is verified via Civic before any PHI is stored. Insurance plans and bill history are cached per-user in Redis for persistent context across sessions.

---

## Architecture

```
User (Telegram) ──photo/PDF──▶ telegram_bot.py (Claude tool-use loop)
                                       │
                     ┌─────────────────┼──────────────────┐
                     │                 │                  │
                     ▼                 ▼                  ▼
              civic_auth.py     bill_analyzer.py     redis_cache.py
           (verify identity)  (OCR + CDT extract)  (get/set insurance
                                     │               plan + history)
                                     ▼
                              scrape.py output
                         (Apify: FairHealth + CMS
                          + ADA CDT pricing data)
                                     │
                                     ▼
                       Contextual AI Datastore
                     (bill PDF + pricing PDFs uploaded
                      as multipart form POST)
                                     │
                                     ▼
                       Contextual AI RAG Agent
                    (multi-hop: correct codes?
                     duplicates? fair price vs billed?)
                                     │
                                     ▼
                          Structured Report
                     (overcharge %, itemized disputes,
                      phone script, appeal letter PDF)
                                     │
                                     ▼
                          User ◀── Telegram reply
```

### Services (docker-compose)

| Service | Description |
|---------|-------------|
| `openclaw-scraper` | Runs `scrape.py` on a 24-hour loop. Uses Apify to scrape FairHealth consumer estimates, CMS fee schedule, and ADA CDT code descriptions. Converts results to PDF and uploads to Contextual AI datastore. |
| `telegram-bot` | Runs `telegram_bot.py`. Long-polls Telegram. Routes messages through Claude tool-use loop. Tools: `analyze_bill`, `get_user_history`, `store_bill_result`. |

Both services share the same Docker image (built from `openclaw/Dockerfile`) but run different commands.

---

## Key APIs & SDKs

### Contextual AI Python SDK

```python
from contextual import ContextualAI

client = ContextualAI(api_key=os.environ["CONTEXTUAL_API_KEY"])

# Create datastore (run once in notebook)
ds = client.datastores.create(name="Dental Pricing Benchmarks")

# Create agent (run once in notebook)
agent = client.agents.create(
    name="Dental Bill Auditor",
    datastore_ids=[ds.id],
    system_prompt=(
        "You are an expert dental billing auditor. "
        "Given CDT codes and billed amounts, identify overcharges, "
        "duplicate billing, unbundling, and upcoding. "
        "Compare billed amounts against FairHealth and CMS benchmarks."
    ),
)

# Query the agent — NOTE: must use .create(), NOT .query()
resp = client.agents.query.create(
    agent_id=agent.id,
    messages=[{"role": "user", "content": "Analyze this bill: ..."}]
)
# Response content is at:
answer = resp.message.content
```

**CRITICAL GOTCHA:** Use `client.agents.query.create(...)` — NOT `client.agents.query(...)`. The latter raises `TypeError: QueryResource object is not callable`.

### Contextual AI REST API

- Base URL: `https://api.contextual.ai/v1`
- Auth header: `Authorization: Bearer <CONTEXTUAL_API_KEY>`

#### Upload a document to datastore

```python
import requests, json

requests.post(
    f"https://api.contextual.ai/v1/datastores/{DATASTORE_ID}/documents",
    headers={"Authorization": f"Bearer {CONTEXTUAL_API_KEY}"},
    files={"file": (filename, file_bytes, "application/pdf")},
    data={"metadata": json.dumps({"custom_metadata": {
        "source": "fairhealth",
        "region": "national",
        "date": "2025-01"
    }})},
)
```

#### Query agent via REST

```python
resp = requests.post(
    f"https://api.contextual.ai/v1/agents/{AGENT_ID}/query",
    headers={
        "Authorization": f"Bearer {CONTEXTUAL_API_KEY}",
        "Content-Type": "application/json",
    },
    json={"messages": [{"role": "user", "content": "..."}]},
)
answer = resp.json()["message"]["content"]
```

### Apify Python Client

```python
from apify_client import ApifyClient

client = ApifyClient(os.environ["APIFY_API_TOKEN"])

# Run a web scraping actor
run = client.actor("apify/web-scraper").call(run_input={
    "startUrls": [{"url": "https://www.fairhealthconsumer.org/dental"}],
    "maxCrawlingDepth": 1,
})

# Get results from dataset
for item in client.dataset(run["defaultDatasetId"]).iterate_items():
    print(item)
```

### Anthropic SDK (Claude tool-use loop)

```python
import anthropic

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

tools = [
    {
        "name": "analyze_bill",
        "description": "OCR a dental bill image/PDF, extract CDT codes, and query the RAG agent for an audit report.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Local path to bill image or PDF"},
                "user_id": {"type": "string", "description": "Civic-verified user ID"},
            },
            "required": ["file_path", "user_id"],
        },
    },
    {
        "name": "get_user_history",
        "description": "Retrieve a user's cached insurance plan and past bill results from Redis.",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
            },
            "required": ["user_id"],
        },
    },
    {
        "name": "store_bill_result",
        "description": "Cache the audit result in Redis for a user.",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "result": {"type": "object", "description": "The audit result dict"},
            },
            "required": ["user_id", "result"],
        },
    },
]

response = client.messages.create(
    model="claude-sonnet-4-20250514",
    max_tokens=4096,
    tools=tools,
    messages=[{"role": "user", "content": "Analyze my dental bill"}],
)
```

### Redis (redis-py)

```python
import redis, json

r = redis.from_url(os.environ["REDIS_URL"])

# Store user insurance plan
r.set(f"user:{user_id}:plan", json.dumps({"insurer": "Delta Dental", "plan": "PPO"}))

# Cache bill result (TTL 90 days)
r.setex(f"user:{user_id}:bill:{bill_hash}", 7776000, json.dumps(result))

# Get history
raw = r.get(f"user:{user_id}:plan")
plan = json.loads(raw) if raw else {}
```

### Civic Auth

```python
# civic_auth.py wraps Civic's identity verification API
# Before storing any PHI, verify user identity:
from civic_auth import verify_user

verification = verify_user(civic_api_key=os.environ["CIVIC_API_KEY"], token=user_civic_token)
if not verification["verified"]:
    raise PermissionError("User identity not verified via Civic")
```

---

## File Map

```
dental-bill-detective-v2/
  AGENTS.md              ← This file: full architecture + API patterns
  SKILL.md               ← OpenClaw skill definition for ClawHub publishing
  docker-compose.yml     ← Two services: openclaw-scraper + telegram-bot
  requirements.txt       ← Notebook deps (contextual-client, anthropic, etc.)
  example.env            ← Template for all env vars (safe to commit)
  .env                   ← Actual secrets (gitignored)
  .gitignore
  README.md
  openclaw/
    Dockerfile           ← Python 3.12-slim + WeasyPrint + tesseract system deps
    requirements.txt     ← Container deps
    scrape.py            ← Apify actor: scrapes FairHealth + CMS + ADA CDT codes
                            Converts results to PDF, uploads to Contextual AI datastore
    bill_analyzer.py     ← OCR bill (pytesseract / pdfplumber) → extract CDT codes
                            → upload bill to Contextual AI → query RAG agent
                            → generate structured report + WeasyPrint appeal letter PDF
    telegram_bot.py      ← Claude tool-use loop:
                            tools: analyze_bill, get_user_history, store_bill_result
                            long-polls Telegram, sends report back to user
    civic_auth.py        ← Civic identity verification wrapper
    redis_cache.py       ← get/set user insurance plan + bill result cache
  notebooks/
    setup.ipynb          ← Part 1: create Contextual AI datastore + agent
                            Part 2: build + launch Docker services
                            Part 3: test a sample bill query
                            Part 4: send Telegram test message
```

---

## Gotchas

1. **SDK query method**: Use `client.agents.query.create(agent_id=..., messages=[...])` — NOT `client.agents.query()`. The latter raises `TypeError: QueryResource object is not callable`.

2. **Response format**: Contextual AI REST responses use `resp.json()["message"]["content"]`. SDK responses use `resp.message.content`. Do NOT use `resp.json()["content"]` or `resp.json()["response"]`.

3. **Document upload is multipart**: Use `files=` and `data=` kwargs in `requests.post()`. Do not send JSON body — it will 400.

4. **Docker cache**: If you edit any `.py` file in `openclaw/`, you must rebuild with `docker compose build --no-cache openclaw` or Docker will serve the cached layer with old code.

5. **PYTHONUNBUFFERED=1**: Required in `docker-compose.yml` environment for Docker container stdout to appear in `docker compose logs`. Without it, Python buffers output and logs look empty.

6. **WeasyPrint system deps**: The Dockerfile installs `libpango-1.0-0 libcairo2 libgdk-pixbuf-2.0-0 libffi-dev`. If you change the base image, these must be present or PDF generation silently fails.

7. **pytesseract system dep**: Requires `tesseract-ocr` installed in the container (`apt-get install -y tesseract-ocr`). pytesseract is only the Python wrapper.

8. **Civic PHI gate**: `civic_auth.py` must be called before any Redis write or Contextual AI upload. Do not cache or upload unverified user data.

9. **Apify rate limits**: Free tier has concurrency limits. The scraper uses `time.sleep(1)` between CDT code lookups. Do not remove these delays.

10. **CDT code format**: ADA CDT codes are `Dxxxx` (e.g., D0120 = periodic oral exam, D2740 = crown). Always normalize to uppercase `D` prefix before lookup.

11. **FairHealth URL structure**: FairHealth consumer estimates are behind a JS-rendered form. Use Apify's `apify/web-scraper` actor (Puppeteer-based) rather than a plain HTTP request.

12. **Redis TTL**: Bill results are cached with a 90-day TTL (`setex` with 7776000 seconds). Insurance plan has no TTL (persists until user updates it).

---

## Sponsor Integrations

| Sponsor | Integration | File |
|---------|-------------|------|
| Contextual AI | RAG datastore + agent for multi-hop dental billing audit | `bill_analyzer.py`, `scrape.py`, `setup.ipynb` |
| Apify | Web scraping FairHealth + CMS fee schedule + ADA CDT codes | `scrape.py` |
| Civic | PHI-safe identity verification before any data storage | `civic_auth.py`, `telegram_bot.py` |
| Redis | Persistent user insurance plan + bill history cache | `redis_cache.py` |
| Anthropic | Claude tool-use loop for Telegram bot reasoning | `telegram_bot.py` |
| WeasyPrint | PDF generation of appeal letters | `bill_analyzer.py` |

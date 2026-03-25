# SKILL.md — OpenClaw Skill Definition for ClawHub

name: dental-bill-detective
version: "2.0.0"
author: medbill-actor
license: MIT

---

## description

An AI-powered dental bill auditor that detects overcharges, duplicate billing, unbundled procedure codes, and upcoded treatments. It compares every CDT procedure code on your bill against live FairHealth consumer estimates and CMS Medicare fee schedule benchmarks, then generates an itemized dispute report with a ready-to-send appeal letter PDF and a phone script for calling your insurance company.

Identity is verified via Civic before any protected health information (PHI) is stored. Past bills and insurance plan details are remembered across sessions via Redis.

---

## triggers

Natural language phrases that activate this skill:

- "analyze my dental bill"
- "check my dental bill for overcharges"
- "is my dentist overbilling me"
- "review this dental invoice"
- "audit my dental charges"
- "I think my dental bill is wrong"
- "help me dispute my dental bill"
- "what is a fair price for [procedure]"
- "is D[code] billed correctly"
- "my insurance denied this dental claim"
- "my dentist billed me twice for the same thing"
- "explain the charges on my dental bill"
- "write an appeal letter for my dental bill"
- "how much should a crown cost"
- "is my dentist upcoding"

---

## integrations

- **civic** — Identity verification (HIPAA-safe PHI storage gate)
- **apify** — Web scraping FairHealth + CMS dental fee schedules
- **contextual-ai** — RAG datastore + agent for multi-hop billing analysis
- **redis** — Persistent user insurance plan + bill history memory
- **anthropic** — Claude tool-use reasoning loop
- **telegram** — User interface (photo/PDF bill submission)
- **weasyprint** — Appeal letter PDF generation

---

## instructions

Step-by-step execution when this skill is triggered:

1. **Receive bill**: User sends a photo or PDF of their dental bill via Telegram.

2. **Verify identity (Civic)**: Before processing any PHI, call `civic_auth.verify_user()` with the user's Civic token. If verification fails, return an error — do not proceed.

3. **Load user context (Redis)**: Call `redis_cache.get_user_plan(user_id)` to retrieve the user's stored insurance plan (insurer name, plan type, in-network vs out-of-network status). Load any past bill results for context.

4. **OCR the bill**:
   - If PDF: use `pdfplumber` to extract text.
   - If image: use `pytesseract` to OCR.
   - Parse extracted text to identify CDT procedure codes (`D0xxx`–`D9xxx`), billed amounts, provider name, and date of service.

5. **Fetch live benchmarks (Apify)**: Call `scrape.py` / `ApifyClient` to retrieve:
   - FairHealth consumer cost estimates for each CDT code in the user's zip code region
   - CMS Medicare fee schedule rates for each CDT code
   - ADA CDT code descriptions (to detect unbundling/upcoding by comparing code description vs billed description)

6. **Upload to Contextual AI**:
   - Upload the bill PDF to the Contextual AI datastore via multipart POST to `/v1/datastores/{DATASTORE_ID}/documents`
   - Upload the pricing benchmark PDF (generated from Apify results) to the same datastore

7. **Query RAG agent**: Send a structured prompt to the Contextual AI agent via `client.agents.query.create(...)`:
   ```
   For each CDT code on this bill:
   1. Is the code billed correctly (matches description)?
   2. Is the billed amount above FairHealth 80th percentile?
   3. Is there a duplicate charge?
   4. Are codes being unbundled (split into sub-codes to inflate total)?
   5. Is the code upcoded (higher complexity code billed for simpler procedure)?
   Provide: fair price, billed price, delta, and dispute recommendation per line.
   ```

8. **Generate report**: Structure the RAG agent response into:
   - **Summary**: Total billed, total fair price, total overcharge amount and percentage
   - **Line-item table**: CDT code | Description | Billed | Fair Price | Difference | Flag (overcharge/duplicate/unbundled/upcoded/ok)
   - **Phone script**: Word-for-word script for calling the insurer's member services line
   - **Appeal letter**: Formal dispute letter citing CDT codes, benchmark prices, and requesting adjustment

9. **Generate appeal letter PDF**: Use WeasyPrint to render the appeal letter as a PDF.

10. **Cache result (Redis)**: Store the audit result with a 90-day TTL via `redis_cache.store_bill_result(user_id, result)`.

11. **Reply via Telegram**: Send the structured report as a formatted message + attach the appeal letter PDF.

---

## output format

```json
{
  "summary": {
    "total_billed": 1240.00,
    "total_fair_price": 890.00,
    "overcharge_amount": 350.00,
    "overcharge_percent": 39.3,
    "flags_found": ["overcharge", "duplicate", "upcoding"]
  },
  "line_items": [
    {
      "cdt_code": "D2740",
      "description": "Crown - porcelain/ceramic substrate",
      "billed": 1800.00,
      "fair_price_80pct": 1320.00,
      "difference": 480.00,
      "flag": "overcharge",
      "recommendation": "Dispute: billed 36% above FairHealth 80th percentile"
    }
  ],
  "phone_script": "Hi, I'm calling about claim #XXXXX dated MM/DD/YYYY...",
  "appeal_letter_pdf_path": "/tmp/appeal_USERID_TIMESTAMP.pdf"
}
```

---

## permissions required

- Read: user Telegram messages + file attachments
- Write: Telegram messages (reply)
- External: Contextual AI API, Apify API, Redis, Civic API, Anthropic API
- Storage: Redis (user plan + bill cache, 90-day TTL)
- PHI gate: Civic identity verification required before any storage or upload

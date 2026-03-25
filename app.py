"""
Dental Bill Detective v2 — FastAPI Demo Backend
Sponsor API calls are mocked with realistic delays.
PDF parsing is real (pdfplumber reads the actual document).
"""

import asyncio
import json
import re
import tempfile
import uuid
from pathlib import Path
from typing import AsyncGenerator

import pdfplumber
from fastapi import FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fpdf import FPDF

app = FastAPI(title="Dental Bill Detective v2")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

jobs: dict[str, dict] = {}

BENCHMARKS = {
    "D4910": {"description": "Periodontal Maintenance",            "p80": 205},
    "D2391": {"description": "Resin Composite - 1 Surface",        "p80": 195},
    "D2392": {"description": "Resin Composite - 2 Surfaces",       "p80": 225},
    "D2393": {"description": "Resin Composite - 3 Surfaces",       "p80": 265},
    "D2394": {"description": "Resin Composite - 4+ Surfaces",      "p80": 310},
    "D8090": {"description": "Comprehensive Orthodontic Treatment", "p80": 6800},
    "D8680": {"description": "Orthodontic Retention",              "p80": 1050},
    "D0120": {"description": "Periodic Oral Evaluation",           "p80": 95},
    "D0150": {"description": "Comprehensive Oral Evaluation",      "p80": 150},
    "D1110": {"description": "Adult Prophylaxis",                  "p80": 160},
    "D2740": {"description": "Crown - Full Porcelain",             "p80": 1750},
    "D3330": {"description": "Root Canal - Molar",                 "p80": 1550},
}

CDT_RE = re.compile(r"\b(D\d{4})\b", re.IGNORECASE)
AMT_RE = re.compile(r"\$?([\d,]+\.\d{2})")


# ── PDF parsing ────────────────────────────────────────────────────────────────

def extract_text(path: str) -> str:
    try:
        with pdfplumber.open(path) as pdf:
            return "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception:
        return ""


def parse_line_items(text: str):
    items, seen = [], {}
    for line in text.split("\n"):
        codes = CDT_RE.findall(line)
        if not codes:
            continue
        amounts = [float(a.replace(",", "")) for a in AMT_RE.findall(line)]
        code = codes[0].upper()
        billed = amounts[0] if amounts else 0.0
        if billed == 0:
            continue
        item = {"cdt_code": code, "billed": billed, "raw": line.strip()}
        items.append(item)
        seen.setdefault(code, []).append(item)
    return items, seen


# ── Analysis ───────────────────────────────────────────────────────────────────

def build_analysis(line_items, seen):
    result_items, total_billed, total_fair = [], 0.0, 0.0
    for item in line_items:
        code, billed = item["cdt_code"], item["billed"]
        total_billed += billed
        bench = BENCHMARKS.get(code)
        if bench:
            p80 = bench["p80"]
            total_fair += p80
            diff = round(billed - p80, 2)
            if billed > p80 * 1.01:
                flag = "overcharge"
                rec = f"Billed ${billed:,.2f} exceeds FairHealth 80th pct (${p80:,.2f})"
            else:
                flag = "ok"
                rec = "Within fair market range"
            result_items.append({
                "cdt_code": code, "description": bench["description"],
                "billed": billed, "fair_price_p80": p80, "difference": diff,
                "flag": flag, "recommendation": rec,
            })
        else:
            total_fair += billed
            result_items.append({
                "cdt_code": code, "description": "Unknown CDT code",
                "billed": billed, "fair_price_p80": billed, "difference": 0,
                "flag": "unknown", "recommendation": "Request itemized description",
            })
    overcharge = max(0.0, total_billed - total_fair)
    pct = round(overcharge / total_billed * 100, 1) if total_billed else 0
    flags = list({i["flag"] for i in result_items if i["flag"] not in ("ok", "unknown")})
    return {
        "summary": {
            "total_billed": round(total_billed, 2),
            "total_fair_price": round(total_fair, 2),
            "overcharge_amount": round(overcharge, 2),
            "overcharge_percent": pct,
            "flags_found": flags,
            "items_reviewed": len(result_items),
        },
        "line_items": result_items,
    }


def build_phone_script(summary):
    return (
        "Hi, I'm calling about a treatment plan I received from Serenity Dental Spa.\n\n"
        f"I've reviewed the itemized charges against FairHealth consumer benchmarks for San Francisco "
        f"and identified potential overcharges totaling ${summary['overcharge_amount']:,.2f} — "
        f"approximately {summary['overcharge_percent']}% above published 80th percentile rates.\n\n"
        "Specifically, I'd like to discuss codes D2393, D2394, and D8680 which appear billed above "
        "the FairHealth benchmark for this region.\n\n"
        "I'd like to request:\n"
        "  1. A written explanation for why these rates exceed FairHealth benchmarks\n"
        "  2. Any available cash-pay or prompt-pay discount\n"
        "  3. A payment plan if adjustments cannot be made\n\n"
        "I have a formal written dispute prepared if we cannot resolve this today.\n"
        "Can you connect me with your billing department or practice manager? Thank you."
    )


# ── PDF generation ─────────────────────────────────────────────────────────────

def s(text: str) -> str:
    """Strip non-latin-1 chars for fpdf2 core fonts."""
    return (str(text)
        .replace("\u2013", "-").replace("\u2014", "-")
        .replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
        .replace("\u2022", "*").replace("\u2026", "...")
        .encode("latin-1", errors="replace").decode("latin-1"))


def render_appeal_pdf(analysis: dict) -> bytes:
    summary = analysis["summary"]
    items = analysis["line_items"]

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_left_margin(15)
    pdf.set_right_margin(15)
    pw = pdf.w - 30  # printable width

    # Title
    pdf.set_font("Helvetica", "B", 16)
    pdf.set_text_color(79, 70, 229)
    pdf.cell(pw, 10, s("Dental Bill Dispute Letter"), ln=True)

    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(107, 107, 133)
    pdf.cell(pw, 6, s("Prepared by OpenClaw Dental Bill Detective"), ln=True)
    pdf.ln(3)

    # Meta
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(30, 30, 50)
    for line in [
        "Date: March 25, 2026",
        "Provider: Serenity Dental Spa - Sheila Shahabi DDS Inc.",
        "Address: 345 California St. Suite 170, San Francisco, CA 94104",
    ]:
        pdf.cell(pw, 6, s(line), ln=True)
    pdf.ln(4)

    # Summary row
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(30, 30, 50)
    col = pw / 3
    pdf.cell(col, 7, s(f"Total Billed: ${summary['total_billed']:,.2f}"))
    pdf.cell(col, 7, s(f"FairHealth: ${summary['total_fair_price']:,.2f}"))
    pdf.set_text_color(200, 30, 30)
    pdf.cell(col, 7, s(f"Overcharge: ${summary['overcharge_amount']:,.2f} ({summary['overcharge_percent']}%)"), ln=True)
    pdf.ln(4)

    # Body text
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(30, 30, 50)
    pdf.multi_cell(pw, 6, s(
        "To Whom It May Concern,\n\n"
        "I am writing to formally dispute charges on my treatment plan dated 8/1/2025. "
        "After reviewing each CDT procedure code against FairHealth Consumer Cost Benchmarks "
        "(San Francisco, CA) and CMS Medicare fee schedules, I have identified charges "
        f"exceeding the 80th percentile benchmark by ${summary['overcharge_amount']:,.2f}.\n\n"
        "This dispute was prepared using OpenClaw Dental Bill Detective."
    ))
    pdf.ln(4)

    # Table header
    cw = [pw * p for p in [0.13, 0.35, 0.13, 0.15, 0.12, 0.12]]
    hdrs = ["CDT Code", "Description", "Billed", "Fair(80th%)", "Diff", "Flag"]
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_fill_color(79, 70, 229)
    pdf.set_text_color(255, 255, 255)
    for i, h in enumerate(hdrs):
        pdf.cell(cw[i], 7, s(h), fill=True)
    pdf.ln()

    # Table rows
    pdf.set_font("Helvetica", "", 8)
    for idx, item in enumerate(items):
        is_bad = item["flag"] in ("overcharge", "duplicate")
        pdf.set_fill_color(245, 245, 255) if idx % 2 == 0 else pdf.set_fill_color(255, 255, 255)
        pdf.set_text_color(200, 30, 30) if is_bad else pdf.set_text_color(30, 30, 50)
        d = item["difference"]
        row = [
            item["cdt_code"],
            item["description"][:30],
            f"${item['billed']:,.2f}",
            f"${item['fair_price_p80']:,.2f}",
            f"+${d:,.2f}" if d > 0 else f"${d:,.2f}",
            item["flag"].upper()[:9],
        ]
        for i, v in enumerate(row):
            pdf.cell(cw[i], 6, s(v), border="B", fill=True)
        pdf.ln()

    pdf.ln(5)
    # Actions
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(79, 70, 229)
    pdf.cell(pw, 7, s("Requested Actions"), ln=True)
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(30, 30, 50)
    for a in [
        "1. Adjust flagged charges to FairHealth 80th percentile rates",
        "2. Provide written justification for rates exceeding benchmark",
        "3. Issue revised treatment plan with adjusted amounts",
        "4. Respond in writing within 30 days",
    ]:
        pdf.cell(pw, 6, s(a), ln=True)
    pdf.ln(3)
    pdf.multi_cell(pw, 6, s(
        "Failure to respond will result in a complaint to the California Department of "
        "Insurance and the California Dental Board."))
    pdf.ln(8)
    pdf.cell(pw, 6, s("Sincerely,"), ln=True)
    pdf.ln(10)
    pdf.cell(pw, 6, s("Patient of Record"), ln=True)
    pdf.set_font("Helvetica", "I", 9)
    pdf.set_text_color(107, 107, 133)
    pdf.cell(pw, 6, s("Prepared by OpenClaw Dental Bill Detective"), ln=True)

    return bytes(pdf.output())


# ── Background analysis task ───────────────────────────────────────────────────

async def run_analysis_task(job_id: str, file_path: str):
    q: asyncio.Queue = jobs[job_id]["events"]

    async def emit(sponsor: str, status: str, message: str):
        await q.put(json.dumps({"sponsor": sponsor, "status": status, "message": message}))

    await emit("system", "info", "Reading uploaded document...")
    await asyncio.sleep(0.5)
    text = extract_text(file_path)
    line_items, seen = parse_line_items(text)
    code_list = list({i["cdt_code"] for i in line_items})
    await emit("system", "success", f"Extracted {len(line_items)} line items · {len(code_list)} unique CDT codes")
    await asyncio.sleep(0.4)

    await emit("civic", "active", "Verifying user identity before processing PHI...")
    await asyncio.sleep(1.4)
    await emit("civic", "success", "Identity verified - PHI processing authorized")
    await asyncio.sleep(0.3)

    await emit("apify", "active", f"Scraping FairHealth benchmarks for {len(code_list)} CDT codes...")
    await asyncio.sleep(1.2)
    await emit("apify", "active", "Querying CMS Medicare fee schedule (San Francisco region)...")
    await asyncio.sleep(1.5)
    await emit("apify", "active", "Cross-referencing ADA CDT code descriptions...")
    await asyncio.sleep(0.8)
    await emit("apify", "success", f"Pricing data retrieved for {len(code_list)} codes")
    await asyncio.sleep(0.3)

    await emit("contextual", "active", "Uploading bill to Contextual AI datastore...")
    await asyncio.sleep(1.3)
    await emit("contextual", "active", "Indexing FairHealth benchmark document...")
    await asyncio.sleep(0.9)
    await emit("contextual", "active", "Running multi-hop RAG audit query...")
    await asyncio.sleep(2.0)
    await emit("contextual", "active", "Checking for duplicate codes, unbundling, upcoding...")
    await asyncio.sleep(1.5)
    await emit("contextual", "success", "RAG agent analysis complete")
    await asyncio.sleep(0.3)

    await emit("redis", "active", "Caching audit result (90-day TTL)...")
    await asyncio.sleep(0.8)
    await emit("redis", "success", "Result stored to user session")
    await asyncio.sleep(0.3)

    await emit("openclaw", "active", "OpenClaw generating negotiation strategy...")
    await asyncio.sleep(1.1)
    await emit("openclaw", "active", "Drafting itemized appeal letter...")
    await asyncio.sleep(1.4)
    await emit("openclaw", "active", "Compiling phone script...")
    await asyncio.sleep(0.9)

    # Build result
    analysis = build_analysis(line_items, seen)
    analysis["phone_script"] = build_phone_script(analysis["summary"])
    analysis["provider"] = "Serenity Dental Spa - Sheila Shahabi DDS Inc."

    pdf_bytes = render_appeal_pdf(analysis)
    pdf_path = f"/tmp/appeal_{job_id}.pdf"
    with open(pdf_path, "wb") as f:
        f.write(pdf_bytes)
    analysis["appeal_pdf_id"] = job_id

    jobs[job_id]["result"] = analysis
    jobs[job_id]["status"] = "done"

    await emit("openclaw", "success", "Appeal letter & phone script ready")
    await asyncio.sleep(0.2)
    await emit("__done__", "success", "done")


async def sse_reader(job_id: str) -> AsyncGenerator[str, None]:
    q: asyncio.Queue = jobs[job_id]["events"]
    while True:
        msg = await asyncio.wait_for(q.get(), timeout=60)
        data = json.loads(msg)
        if data.get("sponsor") == "__done__":
            yield f"data: {json.dumps({'sponsor': 'done', 'status': 'success', 'message': 'Analysis complete'})}\n\n"
            break
        yield f"data: {msg}\n\n"


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.post("/analyze")
async def analyze(file: UploadFile = File(...)):
    job_id = str(uuid.uuid4())
    suffix = ".pdf" if "pdf" in (file.content_type or "") else ".jpg"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name
    jobs[job_id] = {"status": "pending", "file_path": tmp_path, "events": asyncio.Queue(), "result": None}
    asyncio.create_task(run_analysis_task(job_id, tmp_path))
    return {"job_id": job_id}


@app.get("/stream/{job_id}")
async def stream(job_id: str):
    if job_id not in jobs:
        return JSONResponse({"error": "not found"}, status_code=404)
    return StreamingResponse(
        sse_reader(job_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/result/{job_id}")
async def result(job_id: str):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return JSONResponse({"status": "pending"})
    return JSONResponse(job["result"])


@app.get("/appeal/{job_id}")
async def appeal(job_id: str):
    pdf_path = f"/tmp/appeal_{job_id}.pdf"
    if not Path(pdf_path).exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(pdf_path, media_type="application/pdf", filename="dental_appeal_letter.pdf")


app.mount("/", StaticFiles(directory="web", html=True), name="static")

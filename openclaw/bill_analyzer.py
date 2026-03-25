"""
bill_analyzer.py — Core dental bill audit engine.

Steps:
  1. OCR bill image or PDF (pytesseract / pdfplumber)
  2. Extract CDT codes and billed amounts
  3. Upload bill PDF to Contextual AI datastore
  4. Query Contextual AI RAG agent for multi-hop audit
  5. Generate structured report
  6. Render appeal letter as PDF (WeasyPrint)
"""

import hashlib
import io
import json
import os
import re
import time
from pathlib import Path
from typing import Optional

import pdfplumber
import requests
from contextual import ContextualAI
from dotenv import load_dotenv
from weasyprint import HTML

try:
    import pytesseract
    from PIL import Image
    TESSERACT_AVAILABLE = True
except ImportError:
    TESSERACT_AVAILABLE = False

load_dotenv()

CONTEXTUAL_API_KEY = os.environ["CONTEXTUAL_API_KEY"]
DATASTORE_ID = os.environ["DATASTORE_ID"]
AGENT_ID = os.environ["AGENT_ID"]

contextual = ContextualAI(api_key=CONTEXTUAL_API_KEY)

# CDT code pattern: D followed by 4 digits
CDT_PATTERN = re.compile(r"\b(D\d{4})\b", re.IGNORECASE)
# Dollar amount pattern: $1,234.56 or 1234.56
AMOUNT_PATTERN = re.compile(r"\$?([\d,]+\.?\d{0,2})")


def extract_text_from_pdf(file_path: str) -> str:
    """Extract text from a PDF using pdfplumber."""
    text = ""
    with pdfplumber.open(file_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            text += page_text + "\n"
    return text


def extract_text_from_image(file_path: str) -> str:
    """Extract text from an image using pytesseract OCR."""
    if not TESSERACT_AVAILABLE:
        raise RuntimeError("pytesseract not available — install tesseract-ocr")
    img = Image.open(file_path)
    return pytesseract.image_to_string(img)


def extract_text(file_path: str) -> str:
    """Detect file type and extract text accordingly."""
    path = Path(file_path)
    if path.suffix.lower() == ".pdf":
        return extract_text_from_pdf(file_path)
    elif path.suffix.lower() in (".png", ".jpg", ".jpeg", ".tiff", ".bmp"):
        return extract_text_from_image(file_path)
    else:
        # Try PDF first, fall back to OCR
        try:
            return extract_text_from_pdf(file_path)
        except Exception:
            return extract_text_from_image(file_path)


def parse_line_items(text: str) -> list[dict]:
    """
    Extract CDT codes and associated billed amounts from OCR'd text.
    Looks for lines containing a CDT code (Dxxxx) and a dollar amount.
    Returns list of {"cdt_code": "D2740", "billed": 1800.00, "raw_line": "..."}.
    """
    line_items = []
    lines = text.split("\n")
    for line in lines:
        codes = CDT_PATTERN.findall(line)
        amounts = AMOUNT_PATTERN.findall(line)
        if codes:
            # Normalize CDT code to uppercase
            code = codes[0].upper()
            amount = 0.0
            if amounts:
                # Take the largest dollar amount on the line as the billed amount
                parsed = [float(a.replace(",", "")) for a in amounts if a]
                if parsed:
                    amount = max(parsed)
            line_items.append({
                "cdt_code": code,
                "billed": amount,
                "raw_line": line.strip(),
            })
    return line_items


def read_file_bytes(file_path: str) -> bytes:
    """Read file as bytes. If image, convert to PDF for upload."""
    path = Path(file_path)
    if path.suffix.lower() == ".pdf":
        with open(file_path, "rb") as f:
            return f.read()
    else:
        # Wrap image in a minimal PDF for Contextual AI upload
        img_bytes = open(file_path, "rb").read()
        html = f"""
        <html><body>
        <img src="data:image/jpeg;base64,{__import__('base64').b64encode(img_bytes).decode()}"
             style="max-width:100%;"/>
        </body></html>
        """
        return HTML(string=html).write_pdf()


def upload_bill_to_contextual(file_bytes: bytes, user_id: str, bill_hash: str) -> bool:
    """Upload the user's bill PDF to the Contextual AI datastore."""
    filename = f"bill_{user_id}_{bill_hash[:8]}.pdf"
    resp = requests.post(
        f"https://api.contextual.ai/v1/datastores/{DATASTORE_ID}/documents",
        headers={"Authorization": f"Bearer {CONTEXTUAL_API_KEY}"},
        files={"file": (filename, file_bytes, "application/pdf")},
        data={"metadata": json.dumps({"custom_metadata": {
            "type": "user_bill",
            "user_id": user_id,
            "bill_hash": bill_hash,
            "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }})},
        timeout=60,
    )
    return resp.status_code in (200, 201)


def build_audit_prompt(line_items: list[dict], user_plan: Optional[dict] = None) -> str:
    """Build the RAG agent prompt for multi-hop dental bill audit."""
    items_text = "\n".join(
        f"- {item['cdt_code']}: ${item['billed']:.2f} (raw: {item['raw_line']})"
        for item in line_items
    )
    plan_text = ""
    if user_plan:
        plan_text = f"\nUser's insurance plan: {json.dumps(user_plan)}"

    return f"""You are a dental billing auditor. Analyze this dental bill:{plan_text}

BILLED LINE ITEMS:
{items_text}

Using the FairHealth and CMS pricing benchmarks in your knowledge base, for each CDT code:

1. VERIFY: Is the CDT code description correct for what was likely performed?
2. PRICE CHECK: Is the billed amount above the FairHealth 80th percentile benchmark?
3. DUPLICATE: Does this code appear more than once without clinical justification?
4. UNBUNDLING: Are multiple codes being billed that should be bundled into one code?
5. UPCODING: Is a higher-complexity code billed when a simpler code applies?

Respond in this exact JSON format:
{{
  "summary": {{
    "total_billed": <float>,
    "total_fair_price": <float>,
    "overcharge_amount": <float>,
    "overcharge_percent": <float>,
    "flags_found": [<list of: "overcharge"|"duplicate"|"unbundled"|"upcoded"|"ok">]
  }},
  "line_items": [
    {{
      "cdt_code": "<string>",
      "description": "<string>",
      "billed": <float>,
      "fair_price_80pct": <float>,
      "difference": <float>,
      "flag": "<overcharge|duplicate|unbundled|upcoded|ok>",
      "recommendation": "<string>"
    }}
  ],
  "phone_script": "<string — word-for-word script for calling insurer>",
  "dispute_summary": "<string — 2-3 sentence summary of the dispute>"
}}"""


def query_rag_agent(prompt: str) -> dict:
    """
    Query the Contextual AI RAG agent.
    IMPORTANT: use client.agents.query.create(...) NOT client.agents.query(...)
    """
    resp = contextual.agents.query.create(
        agent_id=AGENT_ID,
        messages=[{"role": "user", "content": prompt}],
    )
    content = resp.message.content

    # Try to parse JSON from the response
    try:
        # Strip markdown code fences if present
        cleaned = re.sub(r"```(?:json)?", "", content).strip().rstrip("`").strip()
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Return raw content as fallback
        return {"raw_response": content, "parse_error": True}


def render_appeal_letter_pdf(audit_result: dict, user_id: str) -> str:
    """Generate a WeasyPrint PDF appeal letter from the audit result."""
    summary = audit_result.get("summary", {})
    line_items = audit_result.get("line_items", [])
    dispute_summary = audit_result.get("dispute_summary", "")

    rows = ""
    for item in line_items:
        flag_color = "#e53e3e" if item.get("flag") != "ok" else "#38a169"
        rows += f"""
        <tr>
            <td>{item.get('cdt_code','')}</td>
            <td>{item.get('description','')}</td>
            <td>${item.get('billed',0):.2f}</td>
            <td>${item.get('fair_price_80pct',0):.2f}</td>
            <td style="color:{flag_color}"><strong>${item.get('difference',0):.2f}</strong></td>
            <td style="color:{flag_color}">{item.get('flag','').upper()}</td>
        </tr>"""

    html = f"""
    <html>
    <head>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 60px; color: #1a202c; }}
        h1 {{ color: #2c5282; border-bottom: 2px solid #2c5282; padding-bottom: 10px; }}
        h2 {{ color: #2c5282; margin-top: 30px; }}
        table {{ border-collapse: collapse; width: 100%; margin-top: 15px; }}
        th {{ background: #2c5282; color: white; padding: 10px; text-align: left; font-size: 13px; }}
        td {{ padding: 8px 10px; border-bottom: 1px solid #e2e8f0; font-size: 13px; }}
        .summary-box {{ background: #ebf8ff; border: 1px solid #bee3f8; padding: 20px;
                        border-radius: 6px; margin: 20px 0; }}
        .overcharge {{ color: #e53e3e; font-weight: bold; font-size: 20px; }}
        p {{ line-height: 1.6; }}
    </style>
    </head>
    <body>
        <h1>Dental Bill Dispute Letter</h1>
        <p>Date: {time.strftime('%B %d, %Y')}</p>
        <p>Re: Formal dispute of dental charges — Patient ID: {user_id}</p>

        <div class="summary-box">
            <strong>TOTAL BILLED:</strong> ${summary.get('total_billed', 0):.2f} &nbsp;&nbsp;
            <strong>FAIR PRICE:</strong> ${summary.get('total_fair_price', 0):.2f} &nbsp;&nbsp;
            <span class="overcharge">OVERCHARGE: ${summary.get('overcharge_amount', 0):.2f}
            ({summary.get('overcharge_percent', 0):.1f}%)</span>
        </div>

        <p>To Whom It May Concern,</p>
        <p>
            I am writing to formally dispute the dental charges on the bill referenced above.
            After reviewing each CDT procedure code against FairHealth consumer cost estimates
            and CMS Medicare fee schedule benchmarks, I have identified significant discrepancies
            totaling <strong>${summary.get('overcharge_amount', 0):.2f}</strong>.
        </p>
        <p>{dispute_summary}</p>

        <h2>Itemized Dispute</h2>
        <table>
            <thead>
                <tr>
                    <th>CDT Code</th><th>Description</th><th>Billed</th>
                    <th>Fair Price (80th %ile)</th><th>Difference</th><th>Issue</th>
                </tr>
            </thead>
            <tbody>{rows}</tbody>
        </table>

        <h2>Requested Action</h2>
        <p>
            I respectfully request a review and adjustment of all flagged charges to align with
            FairHealth consumer benchmark rates. Please provide a revised Explanation of Benefits
            (EOB) within 30 days. If you require additional documentation, I am happy to provide it.
        </p>
        <p>
            Failure to respond within 30 days will result in a formal complaint to the
            state insurance commissioner and the relevant state dental board.
        </p>
        <p>Sincerely,<br><br>Patient (ID: {user_id})</p>
    </body>
    </html>
    """

    pdf_bytes = HTML(string=html).write_pdf()
    out_path = f"/tmp/appeal_{user_id}_{int(time.time())}.pdf"
    with open(out_path, "wb") as f:
        f.write(pdf_bytes)
    return out_path


def analyze_bill(file_path: str, user_id: str, user_plan: Optional[dict] = None) -> dict:
    """
    Full bill analysis pipeline.
    Returns audit result dict with summary, line_items, phone_script,
    dispute_summary, and appeal_letter_pdf_path.
    """
    # 1. Read file and compute hash
    file_bytes = read_file_bytes(file_path)
    bill_hash = hashlib.sha256(file_bytes).hexdigest()

    # 2. Extract text and parse CDT codes
    print(f"Extracting text from {file_path}...")
    text = extract_text(file_path)
    line_items = parse_line_items(text)
    if not line_items:
        return {
            "error": "No CDT codes found in bill. Please ensure the image is clear and contains a dental bill.",
            "raw_text_preview": text[:500],
        }
    print(f"Found {len(line_items)} CDT line items: {[i['cdt_code'] for i in line_items]}")

    # 3. Upload bill to Contextual AI
    print("Uploading bill to Contextual AI datastore...")
    upload_bill_to_contextual(file_bytes, user_id, bill_hash)

    # 4. Query RAG agent
    print("Querying Contextual AI RAG agent for audit...")
    prompt = build_audit_prompt(line_items, user_plan)
    audit_result = query_rag_agent(prompt)

    if audit_result.get("parse_error"):
        # Return raw response if JSON parsing failed
        return audit_result

    # 5. Generate appeal letter PDF
    print("Generating appeal letter PDF...")
    pdf_path = render_appeal_letter_pdf(audit_result, user_id)
    audit_result["appeal_letter_pdf_path"] = pdf_path
    audit_result["bill_hash"] = bill_hash

    print(f"Analysis complete. Appeal letter: {pdf_path}")
    return audit_result


if __name__ == "__main__":
    # Quick test with a sample file
    import sys
    if len(sys.argv) < 3:
        print("Usage: python bill_analyzer.py <file_path> <user_id>")
        sys.exit(1)
    result = analyze_bill(sys.argv[1], sys.argv[2])
    print(json.dumps(result, indent=2))

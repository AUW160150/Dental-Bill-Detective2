"""
telegram_bot.py — Claude tool-use Telegram bot for dental bill analysis.

Tools Claude can call:
  - analyze_bill: OCR + CDT extract + RAG audit
  - get_user_history: Redis — user's past bills + insurance plan
  - store_bill_result: Redis — cache audit result

Flow:
  1. User sends a photo or PDF file via Telegram
  2. Bot verifies identity via Civic (or prompts for verification)
  3. Bot downloads the file
  4. Claude tool-use loop: Claude decides which tools to call
  5. Bot sends back the structured report + appeal letter PDF
"""

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Optional

import anthropic
from dotenv import load_dotenv
from telegram import Document, Message, PhotoSize, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import bill_analyzer
import redis_cache
from civic_auth import verify_user

load_dotenv()

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Tool definitions for Claude ────────────────────────────────────────────────

TOOLS = [
    {
        "name": "analyze_bill",
        "description": (
            "OCR a dental bill image or PDF, extract CDT procedure codes and billed amounts, "
            "upload to Contextual AI, query the RAG agent for a multi-hop audit, and generate "
            "a structured report with itemized disputes and an appeal letter PDF."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Local path to the downloaded bill image or PDF",
                },
                "user_id": {
                    "type": "string",
                    "description": "Civic-verified user ID",
                },
                "insurance_plan": {
                    "type": "object",
                    "description": "User's insurance plan context (optional, from Redis)",
                },
            },
            "required": ["file_path", "user_id"],
        },
    },
    {
        "name": "get_user_history",
        "description": "Retrieve the user's cached insurance plan and past bill audit results from Redis.",
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
        "description": "Cache a completed bill audit result in Redis (90-day TTL).",
        "input_schema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "result": {"type": "object", "description": "The full audit result dict"},
            },
            "required": ["user_id", "result"],
        },
    },
]

SYSTEM_PROMPT = """You are Dental Bill Detective, an AI agent that helps users find overcharges and
billing errors on their dental bills. You have access to tools to:
1. Analyze a dental bill (OCR + CDT code audit against FairHealth/CMS benchmarks)
2. Retrieve a user's insurance plan and bill history
3. Store audit results for future reference

When a user sends a bill:
1. First get their history to check for insurance plan context
2. Analyze the bill using their plan context
3. Store the result
4. Present the findings clearly: total overcharge, each flagged item, and next steps
5. Tell them their appeal letter PDF has been generated

Be empathetic — dental bills are stressful. Lead with the most important finding."""


def execute_tool(tool_name: str, tool_input: dict) -> str:
    """Execute a Claude tool call and return the result as a JSON string."""
    if tool_name == "analyze_bill":
        result = bill_analyzer.analyze_bill(
            file_path=tool_input["file_path"],
            user_id=tool_input["user_id"],
            user_plan=tool_input.get("insurance_plan"),
        )
        return json.dumps(result)

    elif tool_name == "get_user_history":
        result = redis_cache.get_user_history(tool_input["user_id"])
        return json.dumps(result)

    elif tool_name == "store_bill_result":
        redis_cache.store_bill_result(tool_input["user_id"], tool_input["result"])
        return json.dumps({"stored": True})

    return json.dumps({"error": f"Unknown tool: {tool_name}"})


def run_claude_tool_loop(user_message: str, user_id: str, file_path: Optional[str] = None) -> str:
    """
    Run Claude's tool-use loop until end_turn.
    Injects file_path into the initial message if a bill was uploaded.
    Returns Claude's final text response.
    """
    messages = []

    if file_path:
        user_message = (
            f"{user_message}\n\n"
            f"Bill file path: {file_path}\n"
            f"User ID: {user_id}"
        )

    messages.append({"role": "user", "content": user_message})

    max_rounds = 5
    for round_num in range(max_rounds):
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        # Add assistant response to message history
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            # Extract text from response
            for block in response.content:
                if hasattr(block, "text"):
                    return block.text
            return "Analysis complete. Check the attached appeal letter PDF."

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    print(f"Claude calling tool: {block.name}({json.dumps(block.input)[:100]}...)")
                    result_str = execute_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_str,
                    })

            messages.append({"role": "user", "content": tool_results})

    return "Analysis complete, but I hit the maximum reasoning rounds. Please try again."


# ── Telegram handlers ──────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Hi! I'm Dental Bill Detective. Send me a photo or PDF of your dental bill "
        "and I'll check it for overcharges, duplicate billing, upcoding, and more.\n\n"
        "I'll compare every CDT procedure code against FairHealth and CMS benchmarks "
        "and generate an appeal letter if needed.\n\n"
        "Just send your bill now!"
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle PDF or image file uploads."""
    message = update.message
    user = message.from_user

    await message.reply_text("Received your bill! Verifying identity and analyzing...")

    # Civic verification — use Telegram user ID as token in dev mode
    # In production: prompt user for their Civic QR code scan result
    civic_token = str(user.id)
    verification = verify_user(civic_token)
    if not verification.get("verified"):
        await message.reply_text(
            f"Identity verification failed: {verification.get('error')}\n"
            "Please complete Civic verification to use this service."
        )
        return

    user_id = verification["user_id"]

    # Download the file
    file_obj = message.document or (message.photo[-1] if message.photo else None)
    if file_obj is None:
        await message.reply_text("Could not read the uploaded file. Please try again.")
        return

    telegram_file = await context.bot.get_file(
        file_obj.file_id if hasattr(file_obj, "file_id") else file_obj.file_id
    )

    suffix = ".pdf"
    if message.document:
        mime = message.document.mime_type or ""
        if "image" in mime:
            suffix = ".jpg"
        elif "pdf" in mime:
            suffix = ".pdf"

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = tmp.name

    await telegram_file.download_to_drive(tmp_path)
    print(f"Downloaded file to {tmp_path} for user {user_id}")

    # Run Claude tool-use loop
    final_response = run_claude_tool_loop(
        user_message="Please analyze this dental bill for overcharges and billing errors.",
        user_id=user_id,
        file_path=tmp_path,
    )

    await message.reply_text(final_response, parse_mode="Markdown")

    # Send appeal letter PDF if it was generated
    # Check Redis for the latest bill result
    history = redis_cache.get_user_history(user_id)
    if history.get("bill_history"):
        latest_hash = history["bill_history"][0]["bill_hash"]
        result = redis_cache.get_bill_result(user_id, latest_hash)
        if result and result.get("appeal_letter_pdf_path"):
            pdf_path = result["appeal_letter_pdf_path"]
            if Path(pdf_path).exists():
                with open(pdf_path, "rb") as f:
                    await message.reply_document(
                        document=f,
                        filename="dental_appeal_letter.pdf",
                        caption="Your appeal letter is ready to send.",
                    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle plain text messages — provide guidance or answer questions."""
    message = update.message
    user = message.from_user
    text = message.text or ""

    civic_token = str(user.id)
    verification = verify_user(civic_token)
    user_id = verification.get("user_id", f"telegram_{user.id}")

    response = run_claude_tool_loop(
        user_message=text,
        user_id=user_id,
        file_path=None,
    )
    await message.reply_text(response, parse_mode="Markdown")


def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("Dental Bill Detective Telegram bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

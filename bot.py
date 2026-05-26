import logging
import os
import io
import asyncio
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
from PIL import Image
from fpdf import FPDF

# HEIC support (optional)
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIC_OK = True
except Exception:
    HEIC_OK = False

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get('BOT_TOKEN')

# Limits
MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB

# Modes
MODE_WAIT_FILE = "wait_file"
MODE_WAIT_TARGET = "wait_target"

# Supported formats
IMAGE_IN = {"jpg", "jpeg", "png", "webp", "bmp", "tiff", "tif", "gif", "heic", "heif"}
IMAGE_OUT = ["JPG", "PNG", "WEBP", "BMP", "TIFF", "GIF", "PDF"]
TEXT_IN = {"txt"}
TEXT_OUT = ["PDF"]


# ---------- Helpers ----------

def main_menu_markup() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("🔄 Convert a File", callback_data="menu_convert")],
        [InlineKeyboardButton("📋 Supported Formats", callback_data="menu_formats")],
        [InlineKeyboardButton("ℹ️ Help", callback_data="menu_help")],
    ]
    return InlineKeyboardMarkup(keyboard)


def targets_markup(options):
    rows = []
    for i in range(0, len(options), 3):
        rows.append(
            [InlineKeyboardButton(opt, callback_data=f"to_{opt.lower()}") for opt in options[i:i+3]]
        )
    rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="menu_home")])
    return InlineKeyboardMarkup(rows)


def reset_user_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop('mode', None)
    context.user_data.pop('source_file', None)


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def get_ext(filename: str) -> str:
    return os.path.splitext(filename or "")[1].lower().lstrip(".")


# ---------- Commands ----------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"User {user.id} started the bot")
    reset_user_state(context)

    welcome = (
        "👋 *Welcome to File Format Converter Bot!*\n\n"
        "I convert between popular file formats:\n\n"
        "🖼 *Images:* JPG, PNG, WEBP, BMP, TIFF, GIF"
        + (", HEIC" if HEIC_OK else "") +
        "\n📄 *Docs:* TXT → PDF, Image → PDF\n\n"
        f"_Max file size: 20 MB._\n\n"
        "Tap below to begin:"
    )
    await update.message.reply_text(welcome, reply_markup=main_menu_markup(), parse_mode='Markdown')


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "ℹ️ *How to use*\n\n"
        "1. Tap 🔄 *Convert a File*\n"
        "2. Send me the file you want to convert\n"
        "3. Pick the target format\n"
        "4. Get your converted file!\n\n"
        "Use /cancel anytime to reset."
    )
    if update.message:
        await update.message.reply_text(text, parse_mode='Markdown', reply_markup=main_menu_markup())
    else:
        await update.callback_query.edit_message_text(
            text, parse_mode='Markdown', reply_markup=main_menu_markup()
        )


async def formats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📋 *Supported Conversions*\n\n"
        "🖼 *Image inputs:* JPG, JPEG, PNG, WEBP, BMP, TIFF, TIF, GIF"
        + (", HEIC, HEIF" if HEIC_OK else "") + "\n"
        "🖼 *Image outputs:* JPG, PNG, WEBP, BMP, TIFF, GIF, PDF\n\n"
        "📄 *Text inputs:* TXT\n"
        "📄 *Text outputs:* PDF\n\n"
        "_Send any supported file to start._"
    )
    if update.message:
        await update.message.reply_text(text, parse_mode='Markdown', reply_markup=main_menu_markup())
    else:
        await update.callback_query.edit_message_text(
            text, parse_mode='Markdown', reply_markup=main_menu_markup()
        )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_user_state(context)
    await update.message.reply_text(
        "❌ Cancelled. Use /start to begin again.",
        reply_markup=main_menu_markup(),
    )


# ---------- Menu callbacks ----------

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "menu_home":
        reset_user_state(context)
        await query.edit_message_text(
            "🏠 *Main Menu*\nChoose an option below:",
            reply_markup=main_menu_markup(),
            parse_mode='Markdown',
        )

    elif data == "menu_help":
        await help_command(update, context)

    elif data == "menu_formats":
        await formats_command(update, context)

    elif data == "menu_convert":
        context.user_data['mode'] = MODE_WAIT_FILE
        await query.edit_message_text(
            "🔄 *Convert Mode*\n\nSend me the file you want to convert.\n\n"
            "_Supports images & TXT files (max 20 MB)._",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Main Menu", callback_data="menu_home")]]
            ),
        )

    elif data.startswith("to_"):
        target = data.split("_", 1)[1].upper()
        await do_convert(update, context, target)


# ---------- File handlers ----------

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await handle_incoming(update, context, update.message.document, is_photo=False)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        return
    largest = update.message.photo[-1]
    # photo has no filename; build a fake one
    fake_doc = type("Obj", (), {
        "file_id": largest.file_id,
        "file_name": f"photo_{largest.file_unique_id}.jpg",
        "file_size": largest.file_size,
        "mime_type": "image/jpeg",
    })()
    await handle_incoming(update, context, fake_doc, is_photo=True)


async def handle_incoming(update, context, doc, is_photo=False):
    mode = context.user_data.get('mode')
    if not doc:
        return

    if mode != MODE_WAIT_FILE:
        await update.message.reply_text(
            "Please tap 🔄 *Convert a File* first.",
            reply_markup=main_menu_markup(),
            parse_mode='Markdown',
        )
        return

    if doc.file_size and doc.file_size > MAX_FILE_SIZE:
        await update.message.reply_text(
            f"⚠️ File too large ({human_size(doc.file_size)}). Max is 20 MB."
        )
        return

    fname = doc.file_name or "file"
    ext = get_ext(fname)

    if ext in IMAGE_IN:
        options = list(IMAGE_OUT)
        # Remove the current format from output options to avoid same-format conversion
        norm = "JPG" if ext in ("jpg", "jpeg") else ext.upper()
        if norm == "TIF":
            norm = "TIFF"
        options = [o for o in options if o != norm]
        kind = "image"
    elif ext in TEXT_IN:
        options = list(TEXT_OUT)
        kind = "text"
    else:
        await update.message.reply_text(
            f"⚠️ Unsupported format: `.{ext}`\n\nUse 📋 *Supported Formats* to see what works.",
            parse_mode='Markdown',
            reply_markup=main_menu_markup(),
        )
        return

    context.user_data['source_file'] = {
        "file_id": doc.file_id,
        "name": fname,
        "ext": ext,
        "kind": kind,
        "size": doc.file_size or 0,
    }
    context.user_data['mode'] = MODE_WAIT_TARGET

    await update.message.reply_text(
        f"📄 Got *{fname}* ({human_size(doc.file_size or 0)}).\n\n"
        f"Convert to which format?",
        reply_markup=targets_markup(options),
        parse_mode='Markdown',
    )


# ---------- Conversion logic ----------

def convert_image(in_bytes: bytes, src_ext: str, target: str) -> bytes:
    """Convert image bytes to target format. target is e.g. 'JPG','PNG','WEBP','PDF'."""
    img = Image.open(io.BytesIO(in_bytes))

    out = io.BytesIO()
    t = target.upper()

    if t == "JPG":
        # JPEG can't handle alpha → flatten on white
        if img.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            img_rgb = img.convert("RGBA")
            bg.paste(img_rgb, mask=img_rgb.split()[-1] if img_rgb.mode == "RGBA" else None)
            img = bg
        else:
            img = img.convert("RGB")
        img.save(out, format="JPEG", quality=92, optimize=True)

    elif t == "PNG":
        img.save(out, format="PNG", optimize=True)

    elif t == "WEBP":
        img.save(out, format="WEBP", quality=92)

    elif t == "BMP":
        img.convert("RGB").save(out, format="BMP")

    elif t == "TIFF":
        img.save(out, format="TIFF")

    elif t == "GIF":
        img.save(out, format="GIF")

    elif t == "PDF":
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")
        img.save(out, format="PDF", resolution=100.0)

    else:
        raise ValueError(f"Unsupported target format: {target}")

    return out.getvalue()


def convert_text_to_pdf(in_bytes: bytes, filename: str) -> bytes:
    """Convert a UTF-8 text file to a PDF."""
    try:
        text = in_bytes.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = in_bytes.decode("latin-1")
        except Exception:
            text = in_bytes.decode("utf-8", errors="replace")

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)

    # Title
    pdf.set_font("Helvetica", style="B", size=13)
    pdf.cell(0, 10, txt=os.path.basename(filename), ln=True)
    pdf.ln(2)

    pdf.set_font("Helvetica", size=11)
    for line in text.splitlines() or [""]:
        # fpdf2 handles long lines via multi_cell wrapping
        # Replace tabs with spaces
        safe = line.replace("\t", "    ")
        try:
            pdf.multi_cell(0, 6, txt=safe)
        except Exception:
            # Fallback: ascii-only
            pdf.multi_cell(0, 6, txt=safe.encode("ascii", "replace").decode("ascii"))

    out = pdf.output(dest="S")
    if isinstance(out, str):
        out = out.encode("latin-1")
    return bytes(out)


async def do_convert(update: Update, context: ContextTypes.DEFAULT_TYPE, target: str):
    query = update.callback_query
    src = context.user_data.get('source_file')

    if not src:
        await query.edit_message_text(
            "⚠️ No file found. Start again.", reply_markup=main_menu_markup()
        )
        return

    chat_id = query.message.chat_id
    await query.edit_message_text(f"⏳ Converting to *{target}*…", parse_mode='Markdown')

    try:
        tg_file = await context.bot.get_file(src["file_id"])
        in_buf = io.BytesIO()
        await tg_file.download_to_memory(out=in_buf)
        in_bytes = in_buf.getvalue()

        loop = asyncio.get_event_loop()

        if src["kind"] == "image":
            out_bytes = await loop.run_in_executor(
                None, convert_image, in_bytes, src["ext"], target
            )
        elif src["kind"] == "text":
            if target.upper() != "PDF":
                raise ValueError("Text files can only be converted to PDF.")
            out_bytes = await loop.run_in_executor(
                None, convert_text_to_pdf, in_bytes, src["name"]
            )
        else:
            raise ValueError("Unsupported source type.")

        base = os.path.splitext(src["name"])[0]
        ext_map = {"JPG": "jpg", "PNG": "png", "WEBP": "webp", "BMP": "bmp",
                   "TIFF": "tiff", "GIF": "gif", "PDF": "pdf"}
        out_name = f"{base}.{ext_map[target.upper()]}"

        await context.bot.send_document(
            chat_id=chat_id,
            document=InputFile(io.BytesIO(out_bytes), filename=out_name),
            caption=f"✅ Converted to *{target}* — {human_size(len(out_bytes))}",
            parse_mode='Markdown',
            reply_markup=main_menu_markup(),
        )

    except Exception as e:
        logger.error(f"Conversion failed: {e}")
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ Conversion failed: {e}",
            reply_markup=main_menu_markup(),
        )
    finally:
        reset_user_state(context)


# ---------- Dummy web server (keeps Render Web Service alive) ----------

async def health(request):
    return web.Response(text="Bot is running")


async def run_web():
    port = int(os.environ.get("PORT", 10000))
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Health server listening on port {port}")


# ---------- Runner ----------

async def run_bot():
    if not BOT_TOKEN:
        logger.critical("FATAL: BOT_TOKEN is missing!")
        return

    try:
        application = Application.builder().token(BOT_TOKEN).build()

        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("formats", formats_command))
        application.add_handler(CommandHandler("cancel", cancel_command))
        application.add_handler(CallbackQueryHandler(menu_callback))
        application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
        application.add_handler(MessageHandler(filters.PHOTO, handle_photo))

        await run_web()

        logger.info("Bot is now polling...")
        await application.initialize()
        await application.start()
        await application.updater.start_polling(drop_pending_updates=True)

        stop_event = asyncio.Event()
        await stop_event.wait()

    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
    finally:
        if 'application' in locals():
            await application.stop()
            await application.shutdown()


def main():
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    except Exception as e:
        logger.error(f"Main loop error: {e}")


if __name__ == '__main__':
    main()

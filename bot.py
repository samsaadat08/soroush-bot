import os
import sys
import asyncio
import logging
from aiohttp import web
import libsql_client
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------
# ۱. تنظیمات لاگ‌گیری آنی (بدون بافر و بدون نمایش توکن)
# ---------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("aiohttp").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------
# ۲. خواندن متغیرهای محیطی از Render
# ---------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL", "").strip()
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or "0")
RENDER_EXTERNAL_URL = os.environ.get(
    "RENDER_EXTERNAL_URL", "https://soroush-bot-w2q5.onrender.com"
).strip()
PORT = int(os.environ.get("PORT", "8080") or "8080")

# ---------------------------------------------------------
# ۳. اتصال ناهمزمان (Async) به دیتابیس Turso و همگام‌سازی جداول
# ---------------------------------------------------------
def get_db():
    db_url = TURSO_DATABASE_URL.replace("libsql://", "https://")
    return libsql_client.create_client(
        url=db_url,
        auth_token=TURSO_AUTH_TOKEN,
    )

async def init_db():
    if not TURSO_DATABASE_URL:
        logger.error("TURSO_DATABASE_URL is missing!")
        return
    try:
        async with get_db() as client:
            try:
                pragma_rs = await client.execute("PRAGMA table_info(experiences)")
                if pragma_rs.rows:
                    expected_cols = {
                        "id", "therapist_name", "clinic_name", "clinic_address",
                        "clinic_phone", "neighborhood", "therapy_domain",
                        "approach", "experience_text", "status", "created_at"
                    }
                    has_legacy_notnull = any(
                        (row[1] not in expected_cols and row[3] == 1)
                        for row in pragma_rs.rows
                    )
                    if has_legacy_notnull:
                        await client.execute("ALTER TABLE experiences RENAME TO experiences_legacy_backup")
            except Exception as e:
                logger.warning(f"Schema check notice: {e}")

            await client.execute("""
                CREATE TABLE IF NOT EXISTS experiences (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    therapist_name TEXT NOT NULL,
                    clinic_name TEXT,
                    clinic_address TEXT,
                    clinic_phone TEXT,
                    neighborhood TEXT,
                    therapy_domain TEXT,
                    approach TEXT,
                    experience_text TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            for col in [
                "clinic_name",
                "clinic_address",
                "clinic_phone",
                "neighborhood",
                "therapy_domain",
                "approach",
                "status",
            ]:
                try:
                    await client.execute(f"ALTER TABLE experiences ADD COLUMN {col} TEXT")
                except Exception:
                    pass

            await client.execute("""
                CREATE TABLE IF NOT EXISTS clinics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    address TEXT,
                    phone TEXT,
                    status TEXT DEFAULT 'approved'
                )
            """)
            for col in ["address", "phone", "status"]:
                try:
                    await client.execute(f"ALTER TABLE clinics ADD COLUMN {col} TEXT")
                except Exception:
                    pass

            try:
                await client.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_clinics_name ON clinics(name)")
            except Exception:
                pass

            default_clinics = [
                ("کلینیک هیمان", "مشهد - بلوار هاشمیه", "ثبت نشده"),
                ("کلینیک پناه", "مشهد - محدوده بلوار سجاد / احمدآباد", "ثبت نشده"),
                ("پلی‌کلینیک دانشگاه فردوسی", "مشهد - پردیس دانشگاه فردوسی", "ثبت نشده"),
            ]
            for c_name, c_addr, c_phone in default_clinics:
                try:
                    check_rs = await client.execute("SELECT id FROM clinics WHERE name = ?", [c_name])
                    if not check_rs.rows:
                        await client.execute(
                            "INSERT INTO clinics (name, address, phone, status) VALUES (?, ?, ?, 'approved')",
                            [c_name, c_addr, c_phone],
                        )
                except Exception:
                    pass
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.exception(f"Database init error (continuing startup): {e}")

# ---------------------------------------------------------
# ۴. تعریف مراحل گفتگو و کیبوردهای گزینه‌ای
# ---------------------------------------------------------
(
    THERAPIST_NAME,
    CLINIC_SELECT,
    NEW_CLINIC_NAME,
    NEW_CLINIC_ADDRESS,
    NEW_CLINIC_PHONE,
    CONFIRM_EXISTING_CLINIC,
    NEIGHBORHOOD,
    THERAPY_DOMAIN,
    APPROACH,
    EXPERIENCE_TEXT,
    SEARCH_TYPE,
    SEARCH_QUERY,
    GUIDE_MENU,
) = range(13)

BACK_BTN = "🔙 بازگشت به منوی اصلی"
GUIDE_BTN = "🧭 راهنمای انتخاب آگاهانه درمانگر"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["✍️ ثبت تجربه", "🔍 جستجوی درمانگر"],
        [GUIDE_BTN],
    ],
    resize_keyboard=True,
)

ADMIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["✍️ ثبت تجربه", "🔍 جستجوی درمانگر"],
        [GUIDE_BTN],
        ["📊 پنل مدیریت و گزارش‌ها"],
    ],
    resize_keyboard=True,
)

GUIDE_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["⚖️ اخلاق حرفه‌ای و خطوط قرمز درمانگر"],
        ["🎓 چک‌لیست انتخاب درمانگر حرفه‌ای"],
        ["🧠 رویکردهای درمانی به زبان ساده"],
        ["📖 نحوه کار با ربات"],
        [BACK_BTN],
    ],
    resize_keyboard=True,
)

NEIGHBORHOOD_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["بلوار سجاد", "وکیل‌آباد", "احمدآباد"],
        ["هاشمیه", "کوهسنگی", "معلم و دانشجو"],
        ["قاسم‌آباد و الهیه", "سایر مناطق مشهد"],
        [BACK_BTN],
    ],
    resize_keyboard=True,
)

DOMAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["فردی بزرگسال", "زوج‌درمانی"],
        ["خانواده درمانی", "کودک و نوجوان"],
        ["گروه درمانی", "مشاوره پیش از ازدواج"],
        ["تحصیلی و شغلی", "سایر حوزه‌ها"],
        [BACK_BTN],
    ],
    resize_keyboard=True,
)

APPROACH_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["انسان‌گرا", "وجودی"],
        ["شناختی-رفتاری (CBT)", "طرحواره‌درمانی"],
        ["متمرکز بر هیجان (EFT)", "روان‌پویشی کوتاه‌مدت"],
        ["پذیرش و تعهد (ACT)", "روانکاوی و تحلیلی"],
        ["سایر رویکردها / نامشخص"],
        [BACK_BTN],
    ],
    resize_keyboard=True,
)

SEARCH_TYPE_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["👤 جستجو با نام درمانگر", "🏥 جستجو با کلینیک"],
        ["🎯 جستجو با حوزه درمان", "🧠 جستجو با رویکرد"],
        ["📍 جستجو با محله", "📋 آخرین تجربه‌های ثبت‌شده"],
        [BACK_BTN],
    ],
    resize_keyboard=True,
)

def get_menu_keyboard(user_id: int):
    return ADMIN_KEYBOARD if user_id == ADMIN_ID else MAIN_KEYBOARD

async def build_clinics_keyboard(include_add_new: bool = True) -> ReplyKeyboardMarkup:
    buttons = []
    try:
        async with get_db() as client:
            rs = await client.execute("SELECT name FROM clinics WHERE status = 'approved' ORDER BY id ASC")
            clinic_names = [row[0] for row in rs.rows]
            for i in range(0, len(clinic_names), 2):
                buttons.append(clinic_names[i : i + 2])
    except Exception as e:
        logger.error(f"Error loading clinics: {e}")

    if include_add_new:
        buttons.append(["➕ ثبت کلینیک جدید", "مطب شخصی / نامشخص"])
    buttons.append([BACK_BTN])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

# ---------------------------------------------------------
# ۵. دستورات پایه (/start) و بخش آموزشی راهنمای انتخاب آگاهانه درمانگر
# ---------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    user = update.effective_user
    welcome_text = """سلام! به سروش خوش آمدید 🌱✨

«سروش» فضایی امن و کاملاً ناشناس برای به اشتراک گذاشتن تجربه‌های واقعی و مثبت مراجعان در مشهد است، تا انتخاب درمانگر برای شما شفاف‌تر و آگاهانه‌تر باشد 💚

👇 از دکمه‌های زیر استفاده کنید:"""
    await update.message.reply_text(
        welcome_text,
        reply_markup=get_menu_keyboard(user.id),
    )
    return ConversationHandler.END

async def open_guide_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    guide_intro = """🧭 **راهنمای انتخاب آگاهانه درمانگر**

انتخاب یک درمانگر خوب، مهم‌ترین قدم در مسیر رشد و حال خوب است.
برای اینکه با آگاهی کامل تصمیم بگیرید، یکی از موضوعات زیر را انتخاب کنید 👇"""
    await update.message.reply_text(
        guide_intro,
        parse_mode="Markdown",
        reply_markup=GUIDE_KEYBOARD,
    )
    return GUIDE_MENU

async def handle_guide_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = update.message.text.strip()
    if "بازگشت به منوی اصلی" in choice:
        return await start(update, context)

    if "اخلاق" in choice:
        text = """⚖️ **اخلاق حرفه‌ای و خطوط قرمز در اتاق درمان**

🔒 **رازدارای مطلق:**
تمام صحبت‌های شما در اتاق درمان کاملاً محرمانه است و بدون رضایت کتبی شما (مگر در موارد خطر جانی فوری) در اختیار هیچ‌کس حتی خانواده قرار نمی‌گیرد.

🚧 **مرزبندی شفاف حرفه‌ای:**
درمانگر متعهد، هیچ‌گونه رابطه دوستی، عاطفی، مالی یا خانوادگی خارج از چارچوب جلسات با مراجع برقرار نمی‌کند.

🤝 **احترام و عدم قضاوت:**
اتاق درمان جای سرزنش یا تحمیل عقاید شخصی درمانگر نیست؛ درمانگر به ارزش‌ها و استقلال شما در تصمیم‌گیری احترام می‌گذارد.

📋 **شفافیت در روند و هزینه:**
مدت جلسه، هزینه و قواعد کنسلی باید از همان جلسه اول به صورت روشن به شما اعلام شود."""
    elif "چک‌لیست" in choice:
        text = """🎓 **چک‌لیست انتخاب یک درمانگر حرفه‌ای**

🪪 **۱. پروانه اشتغال تخصصی:**
مطمئن شوید درمانگر دارای مدرک تحصیلی مرتبط، شماره نظام روانشناسی و پروانه کار معتبر از سازمان نظام روانشناسی است.

🎯 **۲. تناسب تخصص با مسئله شما:**
هر درمانگری در حوزه‌های خاصی (فردی، زوج، کودک، خانواده و...) آموزش تخصصی دیده است؛ درمانگری را انتخاب کنید که متناسب با دغدغه فعلی شما باشد.

💚 **۳. احساس امنیت و درک شدن (اتحاد درمانی):**
پژوهش‌ها نشان می‌دهند مهم‌ترین عامل موفقیت درمان، احساس امنیت و همدلی در ۲ تا ۳ جلسه اول است. اگر احساس راحتی ندارید، تغییر درمانگر حق طبیعی شماست.

⏳ **۴. پرهیز از وعده‌های معجزه‌آسا:**
تغییر روانی یک فرآیند تدریجی و مشترک است؛ از افرادی که وعده درمان قطعی در یک جلسه را می‌دهند فاصله بگیرید."""
    elif "رویکرد" in choice:
        text = """🧠 **آشنایی خیلی خلاصه با رویکردهای درمانی**

🌱 **انسان‌گرا و وجودی:**
تمرکز بر رشد فردی، شناخت خود واقعی، یافتن معنا، مسئولیت‌پذیری و تجربه «اینجا و اکنون» در فضایی صمیمی و بدون قضاوت.

🧩 **شناختی-رفتاری (CBT):**
شناسایی و اصلاح خطاهای فکری و رفتارهایی که باعث اضطراب، وسواس یا افسردگی می‌شوند (ساختارمند و تمرین‌محور).

🪤 **طرحواره‌درمانی:**
ریشه‌یابی «تله‌های زندگی» و الگوهای تکراری که از دوران کودکی شکل گرفته‌اند و در روابط و انتخاب‌های امروز تکرار می‌شوند.

❤️ **متمرکز بر هیجان (EFT):**
کمک به شناخت، تجربه و تنظیم هیجان‌های عمیق و ترمیم دلبستگی (بسیار موثر در زوج‌درمانی و روابط عاطفی).

🌊 **روان‌پویشی کوتاه‌مدت و تحلیلی:**
کاوش در تعارض‌های ناخودآگاه، دفاع‌های روانی و ریشه‌های عمیق هیجانی برای رسیدن به تغییرات پایدار.

🧭 **پذیرش و تعهد (ACT):**
آموزش پذیرش افکار و هیجان‌های دشوار به جای جنگیدن با آن‌ها و حرکت به سمت ارزش‌های مهم زندگی."""
    elif "نحوه کار" in choice:
        text = """📖 **سروش چطور به شما کمک می‌کند؟**

✍️ **ثبت تجربه ناشناس:**
اگر تجربه مثبت و سازنده‌ای از جلسات درمان در مشهد داشته‌اید، می‌توانید بدون ثبت نام و هویت خودتان آن را ثبت کنید تا چراغ راه دیگران باشد.

🔍 **جستجوی هوشمند:**
بر اساس حوزه درمان، رویکرد، محله، کلینیک یا نام درمانگر جستجو کنید و علاوه بر خواندن تجربه واقعی مراجعان، به آدرس و شماره تماس کلینیک دسترسی داشته باشید.

📞 **ارتباط با پشتیبانی:** @Reza_saadatpour"""
    else:
        text = "👇 لطفاً یکی از موضوعات زیر را انتخاب کنید:"

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=GUIDE_KEYBOARD,
    )
    return GUIDE_MENU

# ---------------------------------------------------------
# ۶. فرآیند گزینه‌ای ثبت تجربه جدید + سیستم هوشمند کلینیک، آدرس و تلفن
# ---------------------------------------------------------
async def start_submit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg = """👤 نام و نام خانوادگی درمانگر را بنویسید (بدون پیشوند دکتر):
✍️ مثال: علی رضایی"""
    await update.message.reply_text(
        msg,
        reply_markup=ReplyKeyboardMarkup([[BACK_BTN]], resize_keyboard=True),
    )
    return THERAPIST_NAME

async def get_therapist_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == BACK_BTN:
        return await start(update, context)

    context.user_data["therapist_name"] = text
    kb = await build_clinics_keyboard(include_add_new=True)
    await update.message.reply_text(
        "🏥 کلینیک را از لیست زیر انتخاب کنید (اگر در لیست نیست، گزینه «➕ ثبت کلینیک جدید» را بزنید):",
        reply_markup=kb,
    )
    return CLINIC_SELECT

async def select_clinic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == BACK_BTN:
        return await start(update, context)

    if text == "➕ ثبت کلینیک جدید":
        context.user_data["is_new_clinic"] = True
        await update.message.reply_text(
            "🏥 نام کلینیک یا مرکز مشاوره جدید را بنویسید:",
            reply_markup=ReplyKeyboardMarkup([[BACK_BTN]], resize_keyboard=True),
        )
        return NEW_CLINIC_NAME

    if text == "مطب شخصی / نامشخص":
        context.user_data["clinic_name"] = "مطب شخصی / نامشخص"
        context.user_data["clinic_address"] = "ثبت نشده"
        context.user_data["clinic_phone"] = "ثبت نشده"
        await update.message.reply_text(
            "📍 محله یا محدوده کلینیک/مطب در مشهد را انتخاب کنید:",
            reply_markup=NEIGHBORHOOD_KEYBOARD,
        )
        return NEIGHBORHOOD

    context.user_data["clinic_name"] = text
    existing_address = None
    existing_phone = None
    try:
        async with get_db() as client:
            rs = await client.execute("SELECT address, phone FROM clinics WHERE name = ?", [text])
            if rs.rows:
                existing_address = rs.rows[0][0]
                existing_phone = rs.rows[0][1]
    except Exception as e:
        logger.error(f"Error fetching clinic info: {e}")

    has_addr = existing_address and existing_address != "ثبت نشده"
    has_phone = existing_phone and existing_phone != "ثبت نشده"

    if has_addr or has_phone:
        context.user_data["clinic_address"] = existing_address or "ثبت نشده"
        context.user_data["clinic_phone"] = existing_phone or "ثبت نشده"
        addr_display = existing_address if has_addr else "هنوز ثبت نشده"
        phone_display = existing_phone if has_phone else "هنوز ثبت نشده"

        info_msg = f"""🏥 اطلاعات ثبت‌شده برای **{text}**:
🗺 **آدرس:** {addr_display}
☎️ **شماره تماس:** {phone_display}

آیا این اطلاعات را تایید می‌کنید یا می‌خواهید آدرس و شماره تماس را تکمیل/اصلاح کنید؟"""
        await update.message.reply_text(
            info_msg,
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup(
                [
                    ["✅ بله، تایید اطلاعات کلینیک"],
                    ["✏️ تکمیل / اصلاح آدرس و تلفن"],
                    [BACK_BTN],
                ],
                resize_keyboard=True,
            ),
        )
        return CONFIRM_EXISTING_CLINIC
    else:
        context.user_data["is_new_clinic"] = True
        await update.message.reply_text(
            f"🗺 آدرس یا محدوده دقیق **{text}** را بنویسید (تا برای مراجعان بعدی هم ذخیره شود):",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup(
                [["⏭ رد کردن آدرس"], [BACK_BTN]],
                resize_keyboard=True,
            ),
        )
        return NEW_CLINIC_ADDRESS

async def get_new_clinic_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == BACK_BTN:
        return await start(update, context)

    context.user_data["clinic_name"] = text
    await update.message.reply_text(
        f"🗺 آدرس کلینیک **{text}** را بنویسید (مثال: بلوار سجاد، بزرگمهر جنوبی ۲):",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(
            [["⏭ رد کردن آدرس"], [BACK_BTN]],
            resize_keyboard=True,
        ),
    )
    return NEW_CLINIC_ADDRESS

async def get_new_clinic_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == BACK_BTN:
        return await start(update, context)

    context.user_data["clinic_address"] = "ثبت نشده" if text == "⏭ رد کردن آدرس" else text
    clinic_name = context.user_data.get("clinic_name", "کلینیک")
    await update.message.reply_text(
        f"☎️ شماره تلفن پذیرش یا نوبت‌دهی **{clinic_name}** را بنویسید (مثال: 05137654321):",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(
            [["⏭ رد کردن شماره تلفن"], [BACK_BTN]],
            resize_keyboard=True,
        ),
    )
    return NEW_CLINIC_PHONE

async def get_new_clinic_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == BACK_BTN:
        return await start(update, context)

    context.user_data["clinic_phone"] = "ثبت نشده" if text == "⏭ رد کردن شماره تلفن" else text
    await update.message.reply_text(
        "📍 محله یا محدوده کلینیک در مشهد را از گزینه‌های زیر انتخاب کنید:",
        reply_markup=NEIGHBORHOOD_KEYBOARD,
    )
    return NEIGHBORHOOD

async def confirm_existing_clinic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == BACK_BTN:
        return await start(update, context)

    if text == "✏️ تکمیل / اصلاح آدرس و تلفن":
        context.user_data["is_new_clinic"] = True
        await update.message.reply_text(
            "🗺 آدرس جدید یا تکمیلی کلینیک را بنویسید:",
            reply_markup=ReplyKeyboardMarkup(
                [["⏭ رد کردن آدرس"], [BACK_BTN]],
                resize_keyboard=True,
            ),
        )
        return NEW_CLINIC_ADDRESS

    await update.message.reply_text(
        "📍 محله یا محدوده کلینیک در مشهد را انتخاب کنید:",
        reply_markup=NEIGHBORHOOD_KEYBOARD,
    )
    return NEIGHBORHOOD

async def get_neighborhood(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == BACK_BTN:
        return await start(update, context)

    context.user_data["neighborhood"] = text
    await update.message.reply_text(
        "🎯 **حوزه درمان** شما در جلسات چه بوده است؟ از گزینه‌های زیر انتخاب کنید:",
        parse_mode="Markdown",
        reply_markup=DOMAIN_KEYBOARD,
    )
    return THERAPY_DOMAIN

async def get_therapy_domain(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == BACK_BTN:
        return await start(update, context)

    context.user_data["therapy_domain"] = text
    await update.message.reply_text(
        "🧠 **رویکرد درمانی** درمانگر چه بوده است؟ از گزینه‌های زیر انتخاب کنید:",
        parse_mode="Markdown",
        reply_markup=APPROACH_KEYBOARD,
    )
    return APPROACH

async def get_approach(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if text == BACK_BTN:
        return await start(update, context)

    context.user_data["approach"] = text
    await update.message.reply_text(
        "📝 تجربه مثبت و سازنده خود را از روند درمان با این درمانگر بنویسید (هویت شما کاملاً ناشناس می‌ماند):",
        reply_markup=ReplyKeyboardMarkup([[BACK_BTN]], resize_keyboard=True),
    )
    return EXPERIENCE_TEXT

async def save_experience(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    exp_text = update.message.text.strip()
    if exp_text == BACK_BTN:
        return await start(update, context)

    data = context.user_data
    clinic_name = data.get("clinic_name", "نامشخص")
    clinic_address = data.get("clinic_address", "ثبت نشده")
    clinic_phone = data.get("clinic_phone", "ثبت نشده")
    is_new_clinic = data.get("is_new_clinic", False)

    try:
        async with get_db() as client:
            if is_new_clinic and clinic_name not in ["مطب شخصی / نامشخص", "نامشخص"]:
                check_rs = await client.execute(
                    "SELECT id, address, phone FROM clinics WHERE name = ?", [clinic_name]
                )
                if check_rs.rows:
                    c_id, old_addr, old_phone = check_rs.rows[0]
                    final_addr = clinic_address if clinic_address != "ثبت نشده" else (old_addr or "ثبت نشده")
                    final_phone = clinic_phone if clinic_phone != "ثبت نشده" else (old_phone or "ثبت نشده")
                    await client.execute(
                        "UPDATE clinics SET address = ?, phone = ?, status = 'pending' WHERE id = ?",
                        [final_addr, final_phone, c_id],
                    )
                else:
                    await client.execute(
                        "INSERT INTO clinics (name, address, phone, status) VALUES (?, ?, ?, 'pending')",
                        [clinic_name, clinic_address, clinic_phone],
                    )

            result = await client.execute(
                """
                INSERT INTO experiences (
                    therapist_name, clinic_name, clinic_address, clinic_phone,
                    neighborhood, therapy_domain, approach, experience_text, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                """,
                [
                    data.get("therapist_name", "نامشخص"),
                    clinic_name,
                    clinic_address,
                    clinic_phone,
                    data.get("neighborhood", "نامشخص"),
                    data.get("therapy_domain", "نامشخص"),
                    data.get("approach", "نامشخص"),
                    exp_text,
                ],
            )
            exp_id = result.last_insert_rowid or 1

        await update.message.reply_text(
            "✅ سپاس از همراهی شما! تجربه ارزشمندتان به صورت ناشناس ثبت شد و پس از تایید، در سامانه نمایش داده خواهد شد.",
            reply_markup=get_menu_keyboard(update.effective_user.id),
        )

        if ADMIN_ID:
            new_clinic_tag = "🆕 (کلینیک/اطلاعات جدید)" if is_new_clinic else ""
            admin_msg = f"""🆕 **تجربه جدید ثبت شد (کد #{exp_id})**

👤 **درمانگر:** {data.get('therapist_name')}
🏥 **کلینیک:** {clinic_name} {new_clinic_tag}
🗺 **آدرس کلینیک:** {clinic_address}
☎️ **شماره تماس:** {clinic_phone}
📍 **محله:** {data.get('neighborhood')}
🎯 **حوزه درمان:** {data.get('therapy_domain')}
🧠 **رویکرد:** {data.get('approach')}
📝 **متن تجربه:**
{exp_text}"""
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✅ تایید و انتشار", callback_data=f"approve_{exp_id}"),
                        InlineKeyboardButton("❌ رد کردن", callback_data=f"reject_{exp_id}"),
                    ]
                ]
            )
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=admin_msg,
                    parse_mode="Markdown",
                    reply_markup=keyboard,
                )
            except Exception as e:
                logger.error(f"Error notifying admin: {e}")

    except Exception as e:
        logger.error(f"Database error in save_experience: {e}")
        await update.message.reply_text(
            f"⚠️ خطایی در ذخیره‌سازی رخ داد ({str(e)[:80]}). لطفاً دوباره /start را بزنید.",
            reply_markup=get_menu_keyboard(update.effective_user.id),
        )

    context.user_data.clear()
    return ConversationHandler.END

# ---------------------------------------------------------
# ۷. فرآیند گزینه‌ای جستجوی درمانگر
# ---------------------------------------------------------
async def start_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "🔍 روش جستجوی خود را از گزینه‌های زیر انتخاب کنید:",
        reply_markup=SEARCH_TYPE_KEYBOARD,
    )
    return SEARCH_TYPE

async def select_search_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    choice = update.message.text.strip()
    if choice == BACK_BTN:
        return await start(update, context)

    if choice == "📋 آخرین تجربه‌های ثبت‌شده":
        return await show_search_results(update, context, column=None, query_str=None)

    context.user_data["search_mode"] = choice

    if choice == "👤 جستجو با نام درمانگر":
        await update.message.reply_text(
            "👤 نام یا نام خانوادگی درمانگر مورد نظر را بنویسید:",
            reply_markup=ReplyKeyboardMarkup([[BACK_BTN]], resize_keyboard=True),
        )
    elif choice == "🏥 جستجو با کلینیک":
        kb = await build_clinics_keyboard(include_add_new=False)
        await update.message.reply_text(
            "🏥 کلینیک مورد نظر را از دکمه‌های زیر انتخاب کنید:",
            reply_markup=kb,
        )
    elif choice == "🎯 جستجو با حوزه درمان":
        await update.message.reply_text(
            "🎯 حوزه درمان مورد نظر را انتخاب کنید:",
            reply_markup=DOMAIN_KEYBOARD,
        )
    elif choice == "🧠 جستجو با رویکرد":
        await update.message.reply_text(
            "🧠 رویکرد درمانی مورد نظر را انتخاب کنید:",
            reply_markup=APPROACH_KEYBOARD,
        )
    elif choice == "📍 جستجو با محله":
        await update.message.reply_text(
            "📍 محله مورد نظر را انتخاب کنید:",
            reply_markup=NEIGHBORHOOD_KEYBOARD,
        

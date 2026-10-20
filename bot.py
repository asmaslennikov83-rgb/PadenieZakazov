from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from io import BytesIO
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    Message,
)
from aiogram.client.default import DefaultBotProperties
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from analysis_service import build_cabinet_report, CabinetReport
from config import (
    ALLOWED_TELEGRAM_IDS,
    BOT_TOKEN,
    CABINETS,
    REPORT_TIMEZONE,
    validate_config,
)
from db import (
    UserSettings,
    get_user_settings,
    init_db,
    list_user_settings,
    mark_sent,
    update_setting,
)
from keyboards import (
    cabinet_menu,
    main_menu,
    sort_menu,
    threshold_menu,
)
from reporting import build_excel, format_report_message
from wb_client import WBApiError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("wb-drop-bot")

router = Router()
TZ = ZoneInfo(REPORT_TIMEZONE)


class TimeState(StatesGroup):
    waiting_for_time = State()


def is_allowed(user_id: int | None) -> bool:
    return bool(user_id and user_id in ALLOWED_TELEGRAM_IDS)


async def reject_if_needed(event) -> bool:
    user_id = event.from_user.id if event.from_user else None
    if is_allowed(user_id):
        return False

    if isinstance(event, CallbackQuery):
        await event.answer("Нет доступа", show_alert=True)
    else:
        await event.answer("⛔ У вас нет доступа к этому боту.")
    return True


def cabinet_codes(mode: str) -> list[str]:
    if mode == "spb_tt":
        return ["spb_tt"]
    if mode == "spb_tt_group":
        return ["spb_tt_group"]
    return ["spb_tt", "spb_tt_group"]


def sort_label(mode: str) -> str:
    return {
        "percent": "по проценту падения",
        "average": "по среднему числу заказов",
        "absolute": "по абсолютному снижению",
    }.get(mode, "по проценту падения")


def cabinet_label(mode: str) -> str:
    if mode == "spb_tt":
        return CABINETS["spb_tt"].name
    if mode == "spb_tt_group":
        return CABINETS["spb_tt_group"].name
    return "Оба кабинета"


def settings_text(settings: UserSettings) -> str:
    return (
        "⚙️ <b>Текущие настройки</b>\n\n"
        f"Порог падения: <b>более {settings.threshold}%</b>\n"
        f"Сортировка: <b>{sort_label(settings.sort_mode)}</b>\n"
        f"Время отчёта: <b>{settings.report_time} МСК</b>\n"
        f"Кабинеты: <b>{cabinet_label(settings.cabinet_mode)}</b>\n\n"
        "Среднее рассчитывается за 14 полных дней до вчерашнего дня.\n"
        "FBO + FBS. Отменённые заказы не исключаются."
    )


@router.message(CommandStart())
async def start(message: Message):
    if await reject_if_needed(message):
        return
    settings = await get_user_settings(message.from_user.id)
    await message.answer(
        "👋 <b>Мониторинг падения заказов Wildberries</b>\n\n"
        "Бот сравнивает заказы за вчера со средним за предыдущие 14 полных дней "
        "по каждому артикулу продавца.",
        reply_markup=main_menu(),
    )
    await message.answer(settings_text(settings))


@router.message(Command("menu"))
async def menu_command(message: Message):
    if await reject_if_needed(message):
        return
    await message.answer("Меню:", reply_markup=main_menu())


@router.callback_query(F.data == "menu")
async def menu_callback(call: CallbackQuery):
    if await reject_if_needed(call):
        return
    await call.message.edit_text("Меню:", reply_markup=main_menu())
    await call.answer()


@router.callback_query(F.data == "settings")
async def show_settings(call: CallbackQuery):
    if await reject_if_needed(call):
        return
    settings = await get_user_settings(call.from_user.id)
    await call.message.answer(settings_text(settings), reply_markup=main_menu())
    await call.answer()


@router.callback_query(F.data == "threshold")
async def threshold_callback(call: CallbackQuery):
    if await reject_if_needed(call):
        return
    await call.message.edit_text(
        "Выберите порог падения заказов:",
        reply_markup=threshold_menu(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("set_threshold:"))
async def set_threshold(call: CallbackQuery):
    if await reject_if_needed(call):
        return
    value = int(call.data.split(":", 1)[1])
    if value not in {10, 15, 20, 25, 30, 50}:
        await call.answer("Недопустимое значение", show_alert=True)
        return
    await update_setting(call.from_user.id, "threshold", value)
    await call.message.edit_text(
        f"✅ Порог установлен: <b>более {value}%</b>",
        reply_markup=main_menu(),
    )
    await call.answer()


@router.callback_query(F.data == "sort")
async def sort_callback(call: CallbackQuery):
    if await reject_if_needed(call):
        return
    await call.message.edit_text(
        "Выберите сортировку отчёта:",
        reply_markup=sort_menu(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("set_sort:"))
async def set_sort(call: CallbackQuery):
    if await reject_if_needed(call):
        return
    value = call.data.split(":", 1)[1]
    if value not in {"percent", "average", "absolute"}:
        await call.answer("Недопустимое значение", show_alert=True)
        return
    await update_setting(call.from_user.id, "sort_mode", value)
    await call.message.edit_text(
        f"✅ Сортировка: <b>{sort_label(value)}</b>",
        reply_markup=main_menu(),
    )
    await call.answer()


@router.callback_query(F.data == "cabinet")
async def cabinet_callback(call: CallbackQuery):
    if await reject_if_needed(call):
        return
    await call.message.edit_text(
        "Какие кабинеты анализировать?",
        reply_markup=cabinet_menu(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("set_cabinet:"))
async def set_cabinet(call: CallbackQuery):
    if await reject_if_needed(call):
        return
    value = call.data.split(":", 1)[1]
    if value not in {"spb_tt", "spb_tt_group", "both"}:
        await call.answer("Недопустимое значение", show_alert=True)
        return
    await update_setting(call.from_user.id, "cabinet_mode", value)
    await call.message.edit_text(
        f"✅ Кабинеты: <b>{cabinet_label(value)}</b>",
        reply_markup=main_menu(),
    )
    await call.answer()


@router.callback_query(F.data == "time")
async def time_callback(call: CallbackQuery, state: FSMContext):
    if await reject_if_needed(call):
        return
    await state.set_state(TimeState.waiting_for_time)
    await call.message.answer(
        "Введите время ежедневного отчёта по Москве в формате <b>ЧЧ:ММ</b>.\n"
        "Например: <code>10:00</code>"
    )
    await call.answer()


@router.message(TimeState.waiting_for_time)
async def set_time(message: Message, state: FSMContext):
    if await reject_if_needed(message):
        await state.clear()
        return

    value = (message.text or "").strip()
    try:
        parsed = datetime.strptime(value, "%H:%M")
    except ValueError:
        await message.answer("Неверный формат. Пример: <code>10:00</code>")
        return

    normalized = parsed.strftime("%H:%M")
    await update_setting(message.from_user.id, "report_time", normalized)
    await state.clear()
    await message.answer(
        f"✅ Время ежедневного отчёта: <b>{normalized} МСК</b>",
        reply_markup=main_menu(),
    )


@router.callback_query(F.data == "check_now")
async def check_now(call: CallbackQuery, bot: Bot):
    if await reject_if_needed(call):
        return

    await call.answer("Запускаю анализ…")
    await call.message.answer("⏳ Получаю заказы Wildberries и считаю падение…")

    settings = await get_user_settings(call.from_user.id)
    try:
        await send_report(
            bot=bot,
            chat_id=call.message.chat.id,
            settings=settings,
            force_refresh=True,
        )
    except WBApiError as exc:
        logger.exception("WB API error")
        await call.message.answer(f"❌ Ошибка WB API:\n<code>{str(exc)}</code>")
    except Exception as exc:
        logger.exception("Unexpected report error")
        await call.message.answer(f"❌ Не удалось сформировать отчёт: <code>{exc}</code>")


async def build_reports(
    settings: UserSettings,
    force_refresh: bool = False,
) -> list[CabinetReport]:
    today = datetime.now(TZ).date()
    reports: list[CabinetReport] = []

    # Кабинеты можно запрашивать параллельно: лимит Statistics API считается
    # отдельно на каждый seller account.
    tasks = []
    for code in cabinet_codes(settings.cabinet_mode):
        cabinet = CABINETS[code]
        tasks.append(
            build_cabinet_report(
                cabinet_code=code,
                cabinet_name=cabinet.name,
                threshold=settings.threshold,
                sort_mode=settings.sort_mode,
                today_msk=today,
                force_refresh=force_refresh,
            )
        )

    if tasks:
        reports = list(await asyncio.gather(*tasks))
    return reports


async def send_legacy_report(
    bot: Bot,
    chat_id: int,
    settings: UserSettings,
    force_refresh: bool = False,
) -> None:
    reports = await build_reports(settings, force_refresh=force_refresh)

    for report in reports:
        await bot.send_message(
            chat_id,
            format_report_message(report, limit=20),
        )

    reports_with_rows = [r for r in reports if r.rows]
    if reports_with_rows:
        payload = build_excel(reports_with_rows)
        yesterday = reports_with_rows[0].yesterday.strftime("%d-%m-%Y")
        filename = f"WB_падение_заказов_{yesterday}.xlsx"
        await bot.send_document(
            chat_id,
            BufferedInputFile(payload, filename=filename),
            caption="📎 Полный список артикулов с падением заказов.",
        )


async def scheduler_tick(bot: Bot) -> None:
    now = datetime.now(TZ)
    current_time = now.strftime("%H:%M")
    today = now.date()

    users = await list_user_settings()
    due = [
        user for user in users
        if user.user_id in ALLOWED_TELEGRAM_IDS
        and user.report_time == current_time
        and user.last_sent_date != today.isoformat()
    ]

    if not due:
        return

    # Вызовы идут последовательно по пользователям, но WB-кэш не даст повторно
    # обращаться к API для одинакового периода в пределах WB_CACHE_MINUTES.
    for settings in due:
        try:
            await send_report(
                bot=bot,
                chat_id=settings.user_id,
                settings=settings,
                force_refresh=False,
                scheduled=True,
            )
            await mark_sent(settings.user_id, today)
        except Exception:
            logger.exception("Scheduled report failed for user_id=%s", settings.user_id)


async def main() -> None:
    validate_config()
    await init_db()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    from modern_ui import router as modern_router, refresh_background, urgent_tick, correction_tick, store
    store.recover_interrupted_sources()
    dp.include_router(modern_router)
    dp.include_router(router)

    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.add_job(
        scheduler_tick,
        trigger="interval",
        minutes=1,
        args=[bot],
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    scheduler.add_job(refresh_background, trigger="interval", minutes=20, max_instances=1, coalesce=True, next_run_time=datetime.now(TZ))
    scheduler.add_job(urgent_tick, trigger="interval", minutes=5, args=[bot], max_instances=1, coalesce=True)
    scheduler.add_job(correction_tick, trigger="interval", minutes=5, args=[bot], max_instances=1, coalesce=True)
    scheduler.add_job(store.backup, trigger="cron", hour=0, minute=30, max_instances=1, coalesce=True)

    logger.info("Bot started")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


from modern_ui import send_report

if __name__ == "__main__":
    from runtime_lock import instance_lock
    from config import DB_PATH
    with instance_lock(DB_PATH.parent / 'bot.lock'):
        asyncio.run(main())

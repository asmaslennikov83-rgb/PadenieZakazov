from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Проверить сейчас", callback_data="check_now")],
            [
                InlineKeyboardButton(text="📉 Изменить порог", callback_data="threshold"),
                InlineKeyboardButton(text="📋 Сортировка", callback_data="sort"),
            ],
            [
                InlineKeyboardButton(text="🕙 Время уведомлений", callback_data="time"),
                InlineKeyboardButton(text="🏢 Выбор кабинета", callback_data="cabinet"),
            ],
            [InlineKeyboardButton(text="⚙️ Мои настройки", callback_data="settings")],
        ]
    )


def threshold_menu() -> InlineKeyboardMarkup:
    values = [10, 15, 20, 25, 30, 50]
    rows = []
    for i in range(0, len(values), 3):
        rows.append(
            [
                InlineKeyboardButton(text=f"{v}%", callback_data=f"set_threshold:{v}")
                for v in values[i:i+3]
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def sort_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="🔻 По проценту падения",
                callback_data="set_sort:percent",
            )],
            [InlineKeyboardButton(
                text="📈 По среднему числу заказов",
                callback_data="set_sort:average",
            )],
            [InlineKeyboardButton(
                text="📉 По абсолютному снижению",
                callback_data="set_sort:absolute",
            )],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")],
        ]
    )


def cabinet_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="ООО «СПб-ТТ»", callback_data="set_cabinet:spb_tt")],
            [InlineKeyboardButton(
                text="ООО «СПБ-ТТ ГРУПП»",
                callback_data="set_cabinet:spb_tt_group",
            )],
            [InlineKeyboardButton(text="Оба кабинета", callback_data="set_cabinet:both")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu")],
        ]
    )

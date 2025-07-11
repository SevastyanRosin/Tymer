import os
import logging
from datetime import datetime, timedelta
import matplotlib.pyplot as plt
import asyncio
from io import BytesIO

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# Настройка логгирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация
TOKEN = '-'
GOOGLE_SHEETS_CREDENTIALS = '-'
SHEET_NAME = '-'
WORK_DURATION = 25
BREAK_DURATION = 5

class TimeTrackerBot:
    def __init__(self, application):
        self.application = application
        self.user_timers = {}
        self.user_data = {}
        self.sheet = None
        self._init_google_sheets()

    def _init_google_sheets(self):
        """Инициализация подключения к Google Sheets"""
        try:
            scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
            creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_SHEETS_CREDENTIALS, scope)
            client = gspread.authorize(creds)
            self.sheet = client.open(SHEET_NAME).sheet1
            if not self.sheet.row_values(1):
                self.sheet.append_row(['user_id', 'start_time', 'end_time', 'type', 'duration'])
            logger.info("Успешное подключение к Google Sheets")
        except Exception as e:
            logger.error(f"Ошибка подключения к Google Sheets: {e}")
            self.sheet = None

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        await update.message.reply_text(
            f"Привет, {user.first_name}! Я бот для трекинга времени по методу Pomodoro.\n\n"
            "Доступные команды:\n"
            "/work - начать рабочий интервал (25 мин)\n"
            "/break - начать перерыв (5 мин)\n"
            "/stop - остановить текущий таймер\n"
            "/report - получить отчет за неделю"
        )

    async def start_work(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        await self._cancel_existing_timer(chat_id)
        
        message = await update.message.reply_text(
            f"⏳ Начался рабочий интервал ({WORK_DURATION} минут). Сосредоточься!",
            reply_markup=self._get_stop_button()
        )
        
        self.user_data[chat_id] = {
            'start_time': datetime.now(),
            'type': 'work',
            'message_id': message.message_id
        }
        
        self.user_timers[chat_id] = asyncio.create_task(
            self._run_timer(chat_id, WORK_DURATION, self._work_complete)
        )

    async def start_break(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        await self._cancel_existing_timer(chat_id)
        
        message = await update.message.reply_text(
            f"☕ Начался перерыв ({BREAK_DURATION} минут). Отдохни!",
            reply_markup=self._get_stop_button()
        )
        
        self.user_data[chat_id] = {
            'start_time': datetime.now(),
            'type': 'break',
            'message_id': message.message_id
        }
        
        self.user_timers[chat_id] = asyncio.create_task(
            self._run_timer(chat_id, BREAK_DURATION, self._break_complete)
        )

    async def stop_timer(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        
        if chat_id in self.user_data:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=self.user_data[chat_id]['message_id'],
                    text="⏹ Таймер остановлен"
                )
            except Exception as e:
                logger.error(f"Ошибка редактирования сообщения: {e}")
        
        await self._cancel_existing_timer(chat_id)
        await update.message.reply_text("Таймер остановлен.")

    async def _run_timer(self, chat_id: int, duration: int, callback):
        try:
            await asyncio.sleep(duration * 60)
            await callback(chat_id)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Ошибка в таймере: {e}")

    async def _work_complete(self, chat_id: int):
        await self._save_session(chat_id)
        await self.application.bot.send_message(
            chat_id=chat_id,
            text=f"⌛ Рабочий интервал завершен! Пора сделать перерыв {BREAK_DURATION} минут.",
            reply_markup=self._get_break_button()
        )

    async def _break_complete(self, chat_id: int):
        await self._save_session(chat_id)
        await self.application.bot.send_message(
            chat_id=chat_id,
            text=f"✅ Перерыв завершен! Готов к новому рабочему интервалу {WORK_DURATION} минут?",
            reply_markup=self._get_work_button()
        )

    async def _cancel_existing_timer(self, chat_id: int):
        if chat_id in self.user_timers:
            self.user_timers[chat_id].cancel()
            try:
                await self.user_timers[chat_id]
            except asyncio.CancelledError:
                pass
            del self.user_timers[chat_id]
            
            if chat_id in self.user_data:
                await self._save_session(chat_id)

    async def _save_session(self, chat_id: int):
        if chat_id not in self.user_data or not self.sheet:
            return
            
        data = self.user_data[chat_id]
        end_time = datetime.now()
        duration = (end_time - data['start_time']).total_seconds() / 60
        
        try:
            self.sheet.append_row([
                str(chat_id),
                data['start_time'].strftime('%Y-%m-%d %H:%M:%S'),
                end_time.strftime('%Y-%m-%d %H:%M:%S'),
                data['type'],
                str(round(duration, 2))
            ])
            logger.info(f"Сохранено в таблицу: {data['type']} {duration} мин")
            del self.user_data[chat_id]
        except Exception as e:
            logger.error(f"Ошибка сохранения: {str(e)}")

    async def get_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self.sheet:
            await update.message.reply_text("Ошибка подключения к Google Sheets")
            return
            
        chat_id = update.effective_chat.id
        end_date = datetime.now()
        start_date = end_date - timedelta(days=7)
        
        try:
            records = self.sheet.get_all_records()
            
            user_records = [
                r for r in records 
                if str(r.get('user_id')) == str(chat_id) and 
                datetime.strptime(r.get('start_time'), '%Y-%m-%d %H:%M:%S') >= start_date
            ]
            
            if not user_records:
                await update.message.reply_text("Нет данных за последнюю неделю.")
                return
                
            work_time = sum(float(r.get('duration', 0)) for r in user_records if r.get('type') == 'work')
            break_time = sum(float(r.get('duration', 0)) for r in user_records if r.get('type') == 'break')
            sessions = len(user_records)
            
            await self._create_productivity_chart(user_records, chat_id)
            
            with open(f'{chat_id}_chart.png', 'rb') as chart:
                await update.message.reply_photo(
                    photo=chart,
                    caption=f"📊 Отчет за неделю:\n\n"
                           f"🕒 Рабочих минут: {work_time:.1f}\n"
                           f"☕ Перерывов: {break_time:.1f}\n"
                           f"🔢 Сессий: {sessions}"
                )
            
            os.remove(f'{chat_id}_chart.png')
            
        except Exception as e:
            logger.error(f"Ошибка отчета: {str(e)}")
            await update.message.reply_text(f"Ошибка формирования отчета: {str(e)}")

    async def _create_productivity_chart(self, records: list, chat_id: int):
        daily_data = {}
        for record in records:
            date = datetime.strptime(record['start_time'], '%Y-%m-%d %H:%M:%S').date()
            if date not in daily_data:
                daily_data[date] = {'work': 0, 'break': 0}
            daily_data[date][record['type']] += float(record['duration'])
        
        dates = sorted(daily_data.keys())
        work_values = [daily_data[d]['work'] for d in dates]
        break_values = [daily_data[d]['break'] for d in dates]
        
        plt.figure(figsize=(10, 5))
        plt.bar(dates, work_values, label='Работа', color='#4CAF50')
        plt.bar(dates, break_values, bottom=work_values, label='Перерывы', color='#FF9800')
        
        plt.title('Продуктивность за неделю')
        plt.xlabel('Дата')
        plt.ylabel('Минуты')
        plt.legend()
        plt.grid(True, axis='y', linestyle='--', alpha=0.7)
        plt.xticks(rotation=45)
        plt.tight_layout()
        
        plt.savefig(f'{chat_id}_chart.png')
        plt.close()

    def _get_work_button(self):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("Начать работу", callback_data='start_work')]
        ])

    def _get_break_button(self):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("Начать перерыв", callback_data='start_break')]
        ])

    def _get_stop_button(self):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("Остановить", callback_data='stop')]
        ])

    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        if query.data == 'start_work':
            await self.start_work(update, context)
        elif query.data == 'start_break':
            await self.start_break(update, context)
        elif query.data == 'stop':
            await self.stop_timer(update, context)

def main():
    application = Application.builder().token(TOKEN).build()
    bot = TimeTrackerBot(application)
    
    application.add_handler(CommandHandler("start", bot.start))
    application.add_handler(CommandHandler("work", bot.start_work))
    application.add_handler(CommandHandler("break", bot.start_break))
    application.add_handler(CommandHandler("stop", bot.stop_timer))
    application.add_handler(CommandHandler("report", bot.get_report))
    application.add_handler(CallbackQueryHandler(bot.button_handler))

    application.run_polling()

if __name__ == '__main__':
    main()
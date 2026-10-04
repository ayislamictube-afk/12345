# storage_id_helper.py
# Optional helper: run this locally/temporarily with the SAME bot token.
# Add the bot to your storage group, then send /storageid in the group.
# The bot replies with the numeric chat ID.
#
# Usage:
#   BOT_TOKEN="..." python storage_id_helper.py

import os
import telebot

TOKEN = os.getenv("8591198249:AAEaydQVNM_NaCpZYIiFPIxeBB8IWtrdpwc", "8591198249:AAEaydQVNM_NaCpZYIiFPIxeBB8IWtrdpwc").strip()
if not TOKEN:
    raise SystemExit("Set BOT_TOKEN first.")

bot = telebot.TeleBot(TOKEN)

@bot.message_handler(commands=["storageid"])
def storageid(message):
    bot.reply_to(
        message,
        f"Storage chat ID:\n<code>{message.chat.id}</code>",
        parse_mode="HTML"
    )

print("Send /storageid in your storage group.")
bot.infinity_polling(skip_pending=True)

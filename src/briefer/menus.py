"""Guide / help menu content and inline keyboards."""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

WELCOME = (
    "👋 *Briefer* — your intake analyst.\n\n"
    "Forward or send me *anything* — an article, a post, a link, a PDF, an "
    "image, a GitHub repo, or an event / Luma page — and I will:\n"
    "• summarise it and pull out the *catch points*\n"
    "• tell you *where Vivax could use it*\n"
    "• for events: extract *deadlines, criteria & how to apply*\n"
    "• *double-check my own work* for hallucinations\n"
    "• append it to the right *Google Sheet* (Articles or Events)\n"
    "• *poke you* here with the catch — and before any deadline.\n\n"
    "Type /help any time for the full guide."
)

HELP = (
    "📖 *Briefer — Guide*\n\n"
    "*How to use me*\n"
    "Just send content. I auto-detect whether it's an _article_ or an "
    "_event_ and route it to the right sheet. You can override with the "
    "buttons below or the commands.\n\n"
    "*What I accept*\n"
    "• Text & links (I fetch and read the page)\n"
    "• PDFs and text files (I extract the text)\n"
    "• Images / screenshots (I read them with vision)\n"
    "• Videos & voice/audio (I transcribe them)\n"
    "• Tweets/X posts (the post + the tweet it replies to + any "
    "quoted/retweeted original + its media)\n"
    "• YouTube / Vimeo / TikTok links (I transcribe the video)\n"
    "• GitHub repos (I read the README + metadata)\n"
    "• Luma / event pages (I pull dates, criteria, how to apply)\n\n"
    "*Commands*\n"
    "/start – welcome & menu\n"
    "/help – this guide\n"
    "/menu – quick action buttons\n"
    "/article <text> – force-classify as article\n"
    "/event <text> – force-classify as event\n"
    "/sheets – links to the two Google Sheets\n"
    "/deadlines – upcoming event deadlines I'm tracking\n"
    "/status – bot & queue health\n"
    "/login <password> – authenticate this chat\n"
    "/logout – end this chat's session\n"
    "/whoami – show your chat id (for allow-listing)\n"
    "/cancel – cancel the current action\n\n"
    "*Admin commands*\n"
    "/allow <chat_id> – let another chat use the bot\n"
    "/deny <chat_id> – revoke a runtime-added chat\n"
    "/allowlist – show who can access the bot\n\n"
    "*Calendar*\n"
    "For events I also send a `.ics` file — open it on iPhone/Android and tap "
    "*Add to Calendar*. It carries alarms for the day-of and 2h/1h before, plus "
    "a Google Calendar button.\n\n"
    "*Privacy & security*\n"
    "Only allow-listed chat ids that have /login-ed can talk to me. I never "
    "run anything from your messages; links are fetched through an SSRF "
    "filter; secrets stay on the server."
)


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📄 Send as Article", callback_data="mode:article"),
            InlineKeyboardButton("📅 Send as Event", callback_data="mode:event"),
        ],
        [
            InlineKeyboardButton("🗂 Sheets", callback_data="act:sheets"),
            InlineKeyboardButton("⏰ Deadlines", callback_data="act:deadlines"),
        ],
        [
            InlineKeyboardButton("📖 Help", callback_data="act:help"),
            InlineKeyboardButton("💚 Status", callback_data="act:status"),
        ],
    ])

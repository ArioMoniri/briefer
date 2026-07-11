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
    "*Send as many as you like at once* — I add them to a queue and work "
    "through them one by one, replying under each. Nothing is dropped, and "
    "if the server restarts I resume the queue where I left off.\n\n"
    "Type `/` to see all commands, or /help for this guide.\n\n"
    "*What I accept*\n"
    "• Text & links (I fetch and read the page)\n"
    "• PDFs, Word (.docx), PowerPoint (.pptx), Excel (.xlsx), text files\n"
    "• Images / screenshots (I read them with vision)\n"
    "• Videos & voice/audio (I transcribe them)\n"
    "• Tweets/X posts (the post + the tweet it replies to + any "
    "quoted/retweeted original + its media)\n"
    "• YouTube / Vimeo / TikTok / IG / FB links (I transcribe the video)\n"
    "• For Instagram/LinkedIn text: send a *screenshot* or paste the text\n"
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
    "/calendar – month calendar of all deadlines & event dates "
    "(+ an interactive HTML view)\n"
    "/people – map names→chat ids for row assignments\n"
    "/name <id> <name> – add/edit a person; /unname <id> – remove\n"
    "/status – bot health + your sheet links\n"
    "/stats – totals, done %, overdue, time-to-check\n"
    "/cookies – login freshness (warns before expiry)\n"
    "/logs – recent logs / errors (admin)\n"
    "/login <password> – authenticate this chat\n"
    "/logout – end this chat's session\n"
    "/id – show your chat id (no login needed)\n"
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
    "*The sheet's Status tag* (both sheets) is colored & live: 🔴 Passed, "
    "🟠 Due soon, 🟡 Coming up, 🟢 Upcoming/New, ✅ Done, ⚪ No date.\n\n"
    "*The sheet's ✅ checkbox*\n"
    "Each row has a *Done* checkbox. Tick it and I stop reminding about that "
    "item and record *when* you checked it (and how long it took — there's an "
    "average on the _Stats_ tab). Un-tick it and the clock resets. Delete a "
    "row and I'll never remind about it again.\n\n"
    "*Re-sending the same thing* updates that row _cumulatively_ with any new "
    "info, instead of ignoring it.\n\n"
    "*Assigning a row to someone*\n"
    "First map people once: `/name <their_chat_id> John` (they must have "
    "/start-ed the bot). Then in a row's *Assignee* column pick the name from "
    "the dropdown (or just type it — even 'pass it to John' works). That person "
    "gets a Telegram ping with *👀 Seen* / *✅ Mark checked* buttons; the *Seen* "
    "column shows when they acknowledged, and *Assignee Done* is their own "
    "checkbox (they can tick it from the sheet or the button). Note: Telegram "
    "can't do true read-receipts, so *Seen* means they tapped the button.\n\n"
    "*Reminders* — set one three ways: reply to any message with "
    "`remind me in 3 days` (or a date); add `remind me <when>` to the message "
    "you send; or type a date in the sheet's *Remind At* column. Events also "
    "auto-remind before both the *deadline* and the *event date*.\n\n"
    "*Privacy & security*\n"
    "Only allow-listed chat ids that have /login-ed can talk to me. I never "
    "run anything from your messages; links are fetched through an SSRF "
    "filter; secrets stay on the server. Links *inside* a post are safety-"
    "checked (heuristics + a guard model, and Google Safe Browsing if "
    "configured) before I ever open them."
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

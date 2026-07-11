"""A real, navigable month-calendar view for Telegram.

Unlike the old flat list (which only showed rows from the reminders table),
this pulls every *dated* item straight from the stored entries — article
application deadlines, event dates AND event deadlines, across both sheets —
plus any custom reminders. That means the calendar is always populated from
the data itself, even for items that never had a reminder scheduled.

Rendered as a monospace month grid (days with items marked) followed by that
month's agenda, with ◀/▶ buttons to move between months.
"""
from __future__ import annotations

import calendar
import html
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# icon per item kind
_ICON = {"deadline": "⏰", "event": "📅", "reminder": "📌"}


@dataclass
class CalItem:
    when: datetime
    kind: str            # "deadline" | "event" | "reminder"
    title: str
    sheet: str           # "article" | "event" | ""


def _parse(value: Any):
    """Parse a stored date/datetime string; reuse the pipeline's parsers so we
    accept the same formats (ranges, all-day, ISO, …)."""
    from .pipeline import _parse_deadline, _parse_event_date
    dt = _parse_deadline(value)
    if dt:
        return dt
    dt, _ = _parse_event_date(value)
    return dt


def collect_items(store, chat_id: int) -> list[CalItem]:
    """Every dated item for this chat:

    • each article/event's real *deadline* (⏰) and *event date* (📅), once each;
    • every reminder YOU set (📌) — a `remind me …` or a date typed in the
      sheet's **Remind At** column.

    Only the automatic 72h/24h/3h lead-up pokes are excluded (they're nudges
    toward a deadline that's already shown, not separate dates)."""
    items: list[CalItem] = []
    for sheet in ("article", "event"):
        for e in store.active_entries(sheet):
            if e.get("chat_id") not in (chat_id, 0, None):
                continue
            a = e.get("analysis") or {}
            title = str(a.get("title") or e.get("title") or "Untitled")
            dl = _parse(a.get("application_deadline"))
            if dl:
                items.append(CalItem(dl, "deadline", title, sheet))
            ev = _parse(a.get("event_date"))
            if ev:
                items.append(CalItem(ev, "event", title, sheet))
    # User-set reminders (remind-me + sheet Remind At). Exclude the automatic
    # deadline/event lead-up pokes, which duplicate the dates above.
    try:
        for r in store.all_reminders(chat_id):
            payload = r.get("payload") or {}
            if payload.get("kind") in ("deadline", "event_date"):
                continue
            items.append(CalItem(
                datetime.fromtimestamp(r["fire_at"]), "reminder",
                str(payload.get("title") or r.get("title") or "Reminder"), ""))
    except Exception:  # noqa: BLE001 — reminders are a bonus, never fatal
        pass
    # De-dupe (same day + kind + title).
    seen: set[tuple] = set()
    out: list[CalItem] = []
    for it in sorted(items, key=lambda x: x.when):
        key = (it.when.date(), it.kind, it.title.lower())
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out


def _month_grid(year: int, month: int, marked: set[int], today: datetime) -> str:
    """A Mon–Sun month grid. Each cell is 3 chars wide: right-aligned day +
    one marker — `•` = has item(s), `*` = today, space = nothing."""
    cal = calendar.Calendar(firstweekday=0)  # Monday
    is_today = (today.year, today.month) == (year, month)
    lines = [" Mo  Tu  We  Th  Fr  Sa  Su"]
    for week in cal.monthdayscalendar(year, month):
        cells = []
        for d in week:
            if d == 0:
                cells.append("   ")
            elif is_today and d == today.day:
                cells.append(f"{d:>2}*")
            elif d in marked:
                cells.append(f"{d:>2}•")
            else:
                cells.append(f"{d:>2} ")
        lines.append(" ".join(cells))
    return "\n".join(lines)


def render(items: list[CalItem], year: int, month: int, today: datetime
           ) -> tuple[str, InlineKeyboardMarkup]:
    """Return (HTML text, keyboard) for the given month."""
    in_month = [it for it in items
                if it.when.year == year and it.when.month == month]
    marked = {it.when.day for it in in_month}

    title = datetime(year, month, 1).strftime("%B %Y")
    grid = _month_grid(year, month, marked, today)
    parts = [f"🗓 <b>{title}</b>", f"<pre>{grid}</pre>"]

    if in_month:
        parts.append("<b>This month</b>")
        by_day: dict[int, list[CalItem]] = {}
        for it in in_month:
            by_day.setdefault(it.when.day, []).append(it)
        for day in sorted(by_day):
            when = datetime(year, month, day).strftime("%a %d")
            parts.append(f"<b>{when}</b>")
            for it in by_day[day]:
                t = it.when.strftime("%H:%M")
                t = "" if t == "00:00" else f"{t} "
                tag = f" <i>({it.sheet})</i>" if it.sheet else ""
                parts.append(f"  {_ICON.get(it.kind, '•')} {t}"
                             f"{html.escape(it.title[:60])}{tag}")
    else:
        parts.append("<i>Nothing scheduled this month.</i>")

    # total across all months, so an empty month still hints there's data.
    upcoming = [it for it in items if it.when >= today]
    if upcoming:
        nxt = upcoming[0]
        parts.append(f"\n<i>Next up: {_ICON.get(nxt.kind, '•')} "
                     f"{html.escape(nxt.title[:50])} on "
                     f"{nxt.when.strftime('%Y-%m-%d')}.</i>")

    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("◀", callback_data=f"cal:{prev_y}-{prev_m:02d}"),
        InlineKeyboardButton("Today", callback_data="cal:today"),
        InlineKeyboardButton("▶", callback_data=f"cal:{next_y}-{next_m:02d}"),
    ], [
        InlineKeyboardButton("🌐 Open full calendar (HTML)",
                             callback_data="cal:html"),
    ]])
    return "\n".join(parts), kb


def build_html(items: list[CalItem]) -> str:
    """A self-contained, dependency-free HTML calendar with Month / Week / Day /
    Year / List views and prev·today·next navigation. Events are embedded as
    JSON; opens in any browser offline."""
    events = [{
        "date": it.when.strftime("%Y-%m-%d"),
        "time": "" if it.when.strftime("%H:%M") == "00:00" else it.when.strftime("%H:%M"),
        "title": it.title,
        "kind": it.kind,      # "deadline" | "event"
        "sheet": it.sheet,
    } for it in items]
    data = json.dumps(events, ensure_ascii=False)
    return _HTML_TEMPLATE.replace("/*__EVENTS__*/", data)


# Pure-JS calendar. No external requests (CSP-safe / works offline).
_HTML_TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Briefer — Calendar</title>
<style>
:root{--bg:#0f1115;--panel:#171a21;--line:#262b36;--txt:#e6e9ef;--muted:#8b93a7;
--deadline:#ff6b5e;--event:#4c9aff;--reminder:#a970ff;--today:#2d6cdf22;}
@media (prefers-color-scheme:light){:root{--bg:#f6f7f9;--panel:#fff;--line:#e3e6ec;
--txt:#1a1d24;--muted:#6b7280;--today:#2d6cdf14;}}
*{box-sizing:border-box}body{margin:0;font:14px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;
background:var(--bg);color:var(--txt)}
header{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:12px 16px;
border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:5}
h1{font-size:16px;margin:0 12px 0 0}
button{background:var(--panel);color:var(--txt);border:1px solid var(--line);
border-radius:8px;padding:6px 12px;cursor:pointer;font-size:13px}
button:hover{border-color:var(--muted)}button.active{background:#2d6cdf;color:#fff;border-color:#2d6cdf}
#label{font-weight:600;min-width:160px;text-align:center}
.spacer{flex:1}
.grid{display:grid;grid-template-columns:repeat(7,1fr);gap:1px;background:var(--line);
border:1px solid var(--line)}
.dow{background:var(--panel);color:var(--muted);text-align:center;padding:6px;font-size:12px}
.cell{background:var(--bg);min-height:96px;padding:4px;overflow:hidden}
.cell.other{opacity:.4}.cell.today{background:var(--today)}
.dnum{font-size:12px;color:var(--muted);margin-bottom:2px}
.ev{font-size:11px;border-radius:5px;padding:2px 5px;margin:2px 0;color:#fff;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:default}
.ev.deadline{background:var(--deadline)}.ev.event{background:var(--event)}
.ev.reminder{background:var(--reminder)}.dot.reminder{background:var(--reminder)}
.list .row{display:flex;gap:10px;padding:9px 16px;border-bottom:1px solid var(--line);align-items:baseline}
.list .d{color:var(--muted);min-width:130px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.dot.deadline{background:var(--deadline)}.dot.event{background:var(--event)}
.yeargrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px;padding:16px}
.mini{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px}
.mini h3{margin:0 0 6px;font-size:13px}.mini .mg{display:grid;grid-template-columns:repeat(7,1fr);gap:2px}
.mini .mc{text-align:center;font-size:10px;padding:2px 0;border-radius:4px;color:var(--muted)}
.mini .mc.has{background:#2d6cdf;color:#fff;font-weight:600;cursor:pointer}
.wrap{padding:16px}.hint{color:var(--muted);font-size:12px;padding:8px 16px}
.legend{display:flex;gap:14px;color:var(--muted);font-size:12px;padding:6px 16px}
</style></head><body>
<header>
  <h1>🗓 Briefer Calendar</h1>
  <button onclick="nav(-1)">◀</button>
  <button onclick="today()">Today</button>
  <button onclick="nav(1)">▶</button>
  <span id="label"></span>
  <span class="spacer"></span>
  <button data-v="year"  onclick="setView('year')">Year</button>
  <button data-v="month" onclick="setView('month')">Month</button>
  <button data-v="week"  onclick="setView('week')">Week</button>
  <button data-v="day"   onclick="setView('day')">Day</button>
  <button data-v="list"  onclick="setView('list')">List</button>
</header>
<div class="legend"><span><span class="dot deadline"></span>Deadline</span>
<span><span class="dot event"></span>Event date</span>
<span><span class="dot reminder"></span>Reminder</span></div>
<div id="root" class="wrap"></div>
<script>
const EVENTS = /*__EVENTS__*/;
const byDate = {};
for(const e of EVENTS){(byDate[e.date]=byDate[e.date]||[]).push(e);}
const DOW=["Mon","Tue","Wed","Thu","Fri","Sat","Sun"];
const MON=["January","February","March","April","May","June","July","August",
"September","October","November","December"];
let view="month", cur=new Date(); cur.setHours(0,0,0,0);
const iso=d=>d.toISOString().slice(0,10);
const evOf=d=>byDate[iso(d)]||[];
function addDays(d,n){const x=new Date(d);x.setDate(x.getDate()+n);return x;}
function startOfWeek(d){const x=new Date(d);const wd=(x.getDay()+6)%7;return addDays(x,-wd);}
function isToday(d){const t=new Date();return d.toDateString()===t.toDateString();}

function evHtml(e){const t=e.time?e.time+" ":"";const s=e.sheet?" · "+e.sheet:"";
return `<div class="ev ${e.kind}" title="${t}${esc(e.title)}${s}">${t}${esc(e.title)}</div>`;}
function esc(s){return (s||"").replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}

function renderMonth(){
  const y=cur.getFullYear(),m=cur.getMonth();
  document.getElementById("label").textContent=MON[m]+" "+y;
  let start=startOfWeek(new Date(y,m,1));
  let h='<div class="grid">'+DOW.map(d=>`<div class="dow">${d}</div>`).join("");
  for(let i=0;i<42;i++){const d=addDays(start,i);
    const other=d.getMonth()!==m?" other":"";const td=isToday(d)?" today":"";
    h+=`<div class="cell${other}${td}"><div class="dnum">${d.getDate()}</div>`+
       evOf(d).map(evHtml).join("")+`</div>`;
    if(i%7===6&&d>new Date(y,m+1,0)&&i>=34)break;
  }
  h+='</div>';return h;
}
function renderWeek(){
  const start=startOfWeek(cur);const end=addDays(start,6);
  document.getElementById("label").textContent=
    start.toLocaleDateString()+" – "+end.toLocaleDateString();
  let h='<div class="grid">'+DOW.map(d=>`<div class="dow">${d}</div>`).join("");
  for(let i=0;i<7;i++){const d=addDays(start,i);const td=isToday(d)?" today":"";
    h+=`<div class="cell${td}" style="min-height:220px"><div class="dnum">${d.getDate()} ${MON[d.getMonth()].slice(0,3)}</div>`+
       evOf(d).map(evHtml).join("")+`</div>`;}
  return h+'</div>';
}
function renderDay(){
  document.getElementById("label").textContent=cur.toDateString();
  const evs=evOf(cur);
  if(!evs.length)return '<div class="hint">Nothing on this day.</div>';
  return '<div class="list">'+evs.map(e=>`<div class="row"><span class="d">${e.time||"all day"}</span>
    <span><span class="dot ${e.kind}"></span>${esc(e.title)}${e.sheet?` · ${e.sheet}`:""}</span></div>`).join("")+'</div>';
}
function renderList(){
  document.getElementById("label").textContent="All items";
  const keys=Object.keys(byDate).sort();
  if(!keys.length)return '<div class="hint">No dated items yet.</div>';
  let h='<div class="list">';
  for(const k of keys){for(const e of byDate[k]){
    h+=`<div class="row"><span class="d">${k}${e.time?" "+e.time:""}</span>
      <span><span class="dot ${e.kind}"></span>${esc(e.title)}${e.sheet?` · ${e.sheet}`:""}</span></div>`;}}
  return h+'</div>';
}
function renderYear(){
  const y=cur.getFullYear();document.getElementById("label").textContent=y;
  let h='<div class="yeargrid">';
  for(let m=0;m<12;m++){
    let start=startOfWeek(new Date(y,m,1));
    let g='<div class="mg">';
    for(let i=0;i<42;i++){const d=addDays(start,i);
      if(d.getMonth()!==m){g+='<div class="mc"></div>';continue;}
      const has=evOf(d).length?" has":"";
      g+=`<div class="mc${has}" ${has?`onclick="goto('${iso(d)}')"`:""}>${d.getDate()}</div>`;
    }
    h+=`<div class="mini"><h3>${MON[m]}</h3>${g}</div></div>`;
  }
  return h+'</div>';
}
function draw(){
  document.querySelectorAll("header button[data-v]").forEach(b=>
    b.classList.toggle("active",b.dataset.v===view));
  const r=document.getElementById("root");
  r.innerHTML=({month:renderMonth,week:renderWeek,day:renderDay,
                year:renderYear,list:renderList})[view]();
}
function setView(v){view=v;draw();}
function nav(n){if(view==="year")cur.setFullYear(cur.getFullYear()+n);
  else if(view==="week")cur=addDays(cur,7*n);
  else if(view==="day")cur=addDays(cur,n);
  else cur.setMonth(cur.getMonth()+n);draw();}
function today(){cur=new Date();cur.setHours(0,0,0,0);draw();}
function goto(s){cur=new Date(s+"T00:00");view="day";draw();}
// jump to the first upcoming item so the view opens somewhere useful.
(function(){const up=Object.keys(byDate).sort().find(k=>k>=iso(new Date()));
  if(up)cur=new Date(up+"T00:00");draw();})();
</script></body></html>"""

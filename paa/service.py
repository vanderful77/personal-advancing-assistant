import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .browser import Browser, AuthRequired
from .calendar import Calendar
from .model import Model
from .store import Store, digest


def briefing(cfg, store):
    current = datetime.now(ZoneInfo(cfg["timezone"]))
    lines = [f"PAA 简报 · {current:%Y-%m-%d %H:%M}", "\n来源状态（不是全部网站的实时保证）："]
    for s in store.db.execute("SELECT * FROM sources ORDER BY name"):
        lines.append(f"• {s['name']}: {s['status']}；成功时间 {s['last_ok'] or '从未'}\n  {s['detail'] or ''}")
    rows = store.db.execute('''SELECT e.*,d.url FROM events e JOIN docs d ON d.id=e.doc_id
                            WHERE e.status='approved' ORDER BY start''').fetchall()
    lines.append("\n未来七天安排与 deadline：")
    count = 0
    for r in sorted(rows, key=lambda r:datetime.fromisoformat(r["start"].replace("Z","+00:00"))):
        dt = datetime.fromisoformat(r["start"].replace("Z","+00:00")).astimezone(current.tzinfo)
        if current <= dt <= current + timedelta(days=7):
            label = "两日内" if dt <= current + timedelta(days=2) else "近期准备"
            lines.append(f"• [{label}/{r['kind']}] {dt:%m-%d %H:%M} {r['title']} {r['location']}\n{r['url']}")
            count += 1
    if not count:
        lines.append("暂无已确认安排；不代表来源中没有尚未提取的任务。")
    pending = store.db.execute("SELECT count(*) FROM events WHERE status='pending'").fetchone()[0]
    stale = store.db.execute("SELECT count(*) FROM events WHERE status='stale'").fetchone()[0]
    jobs = store.db.execute("SELECT count(*) FROM jobs WHERE status!='done'").fetchone()[0]
    lines.append(f"\n待确认日期 {pending}；来源变化待复核 {stale}；待分析材料 {jobs}")
    lines.append("命令：/status /find 关键词 /review /brief")
    return "\n".join(lines)


def local_index(cfg, store):
    root = Path(cfg["personal_dir"])
    seen = set()
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink() and path.suffix.lower() in (".md", ".txt"):
            if not path.resolve().is_relative_to(root.resolve()):
                continue
            key = "personal:" + str(path.relative_to(root))
            seen.add(key)
            store.document(key,"personal",path.stem,str(path.resolve()),path.read_text(encoding="utf-8"))
            with store.db:
                store.db.execute("UPDATE jobs SET status='done' WHERE doc_id=?", (key,))
    existing = store.db.execute("SELECT id FROM docs WHERE source='personal'").fetchall()
    with store.db:
        for row in existing:
            if row[0] not in seen:
                store.db.execute("DELETE FROM docs WHERE id=?",(row[0],))
                store.db.execute("DELETE FROM jobs WHERE doc_id=?",(row[0],))
    store.status("personal", "ok", f"Indexed {len(seen)} Markdown/text files")


def analyze(cfg, store, limit=3):
    if not cfg["model"].get("enabled"):
        store.status("analysis", "disabled", "Structured Canvas deadlines still work; prose/PDF analysis is queued")
        return
    model = Model(cfg,store)
    docs = store.db.execute('''SELECT d.* FROM docs d JOIN jobs j ON j.doc_id=d.id
                              WHERE j.status='pending' AND d.active=1 LIMIT ?''',(limit,)).fetchall()
    for doc in docs:
        try:
            result = model.analyze(doc)
            events = result.get("events",[])
            if not isinstance(events,list) or len(events)>30:
                raise ValueError("Invalid extraction")
            for i,e in enumerate(events):
                meta = json.loads(doc["meta"])
                if e.get("kind") == "deadline" and e.get("start") == meta.get("due"):
                    continue
                key = "proposal:" + digest(doc["id"] + ":" + str(i))[:20]
                store.event(key, doc["id"],e["title"],e["start"],e.get("end"),e["kind"],e.get("location",""),e["quote"])
            store.set("notes:"+doc["id"],result.get("notes",""))
            with store.db:
                store.db.execute("UPDATE jobs SET status='done' WHERE doc_id=? AND hash=?",(doc["id"],doc["hash"]))
        except Exception as exc:
            store.status("analysis", "error", type(exc).__name__ + ": " + str(exc)[:200] if isinstance(exc,ValueError) else type(exc).__name__)
            store.enqueue("analysis-error:"+datetime.now().strftime("%Y-%m-%d"),"材料分析暂停或失败，请用 /status 查看。已有提醒继续运行。")
            return
    store.status("analysis","ok", f"Analyzed {len(docs)} changed documents; dates require review")


async def sync(cfg, store, browser):
    local_index(cfg,store)
    for source in ("canvas","outlook"):
        if not cfg[source].get("enabled"):
            store.status(source,"disabled")
            continue
        old = store.db.execute("SELECT status FROM sources WHERE name=?",(source,)).fetchone()
        try:
            detail = await getattr(browser,source)()
            status = "partial" if source == "outlook" else "ok"
            store.status(source,status,detail)
        except Exception as exc:
            status = "auth_required" if isinstance(exc,AuthRequired) else "error"
            detail = str(exc)[:1500] if type(exc).__module__ == "paa.browser" else type(exc).__name__
            store.status(source,status,detail)
        if status in ("auth_required","error") and (not old or old[0] != status):
            episode = str(datetime.now().timestamp())
            store.enqueue(f"source:{source}:{episode}",f"{source} 同步暂停：{status}。{detail}\n已有数据保留；/status 查看最近成功时间。")
    await asyncio.to_thread(analyze_isolated,cfg)
    if cfg["calendar"].get("enabled"):
        try:
            count = await asyncio.to_thread(calendar_isolated,cfg)
            store.status("calendar","ok",f"Checked {count} approved events")
        except Exception as exc:
            store.status("calendar","error",type(exc).__name__)
    # Notify newly discovered or changed imminent confirmed events once per version.
    current = datetime.now(ZoneInfo(cfg["timezone"]))
    for row in store.db.execute("SELECT * FROM events WHERE status='approved'"):
        dt = datetime.fromisoformat(row["start"].replace("Z","+00:00"))
        if current <= dt <= current + timedelta(days=2):
            store.enqueue("urgent:"+row["id"]+":"+row["hash"],f"近期新增/变更：{row['title']}\n{row['start']} {row['location']}\n/brief 查看详情。")


def analyze_isolated(cfg):
    store = Store(cfg["data_dir"])
    try:
        analyze(cfg,store)
    finally:
        store.db.close()


def calendar_isolated(cfg):
    store = Store(cfg["data_dir"])
    try:
        return Calendar(cfg,store).sync()
    finally:
        store.db.close()


def telegram_tick(cfg):
    from .telegram import Telegram
    store = Store(cfg["data_dir"])
    try:
        bot = Telegram(cfg,store)
        bot.poll()
        bot.flush()
        store.status("telegram","ok")
    except Exception as exc:
        store.status("telegram","error",type(exc).__name__)
    finally:
        store.db.close()


async def run(cfg):
    from .sender import send_confirmed
    store = Store(cfg["data_dir"])
    with store.db:
        store.db.execute("UPDATE drafts SET status='unknown' WHERE status='sending'")
        store.db.execute("UPDATE outbox SET status='unknown' WHERE status='sending'")
    browser = Browser(cfg,store)
    await browser.start()

    async def messaging():
        while True:
            current = datetime.now(ZoneInfo(cfg["timezone"]))
            hour, minute = map(int,cfg["briefing_time"].split(":"))
            if (current.hour,current.minute) >= (hour,minute):
                store.enqueue("brief:"+current.date().isoformat(),briefing(cfg,store))
            if cfg["telegram"].get("enabled"):
                await asyncio.to_thread(telegram_tick,cfg)
            await asyncio.sleep(2)

    messaging_task = asyncio.create_task(messaging())
    next_sync = 0
    try:
        while True:
            loop = asyncio.get_running_loop()
            if loop.time() >= next_sync:
                await sync(cfg,store,browser)
                next_sync = loop.time() + cfg["poll_seconds"]
            await send_confirmed(cfg,store,browser)
            if store.get("sync_requested",False):
                store.set("sync_requested",False)
                next_sync = 0
            if messaging_task.done():
                messaging_task.result()
            await asyncio.sleep(2)
    finally:
        messaging_task.cancel()
        await asyncio.gather(messaging_task,return_exceptions=True)
        await browser.close()
        store.db.close()

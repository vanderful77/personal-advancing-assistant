import argparse
import asyncio
import fcntl
import json
from pathlib import Path
from .config import load
from .store import Store


def main():
    parser = argparse.ArgumentParser(description="PAA persistent course assistant")
    parser.add_argument("--config",default="config.toml")
    sub = parser.add_subparsers(dest="command",required=True)
    for name in ("run","sync","status","brief","google-login","index"):
        sub.add_parser(name)
    login = sub.add_parser("login")
    login.add_argument("source",choices=("canvas","outlook","all"))
    search = sub.add_parser("search")
    search.add_argument("query")
    draft = sub.add_parser("import-draft")
    draft.add_argument("id")
    draft.add_argument("json_file")
    args = parser.parse_args()
    cfg = load(args.config)
    store = Store(cfg["data_dir"])
    if args.command in ("status","brief"):
        from .service import briefing
        print(briefing(cfg,store))
        return
    if args.command == "search":
        for r in store.search(args.query):
            print(r["id"],r["title"],r["url"],r["body"][:600],sep="\n")
        return
    # All mutating CLI commands and daemon share a lock. Never share one profile
    # between separate Chromium processes, including the manual login helper.
    with (Path(cfg["data_dir"])/"process.lock").open("w") as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("PAA is running. Stop its service before login/sync/index commands.")
        if args.command == "google-login":
            from .calendar import Calendar
            Calendar(cfg,store).login()
        elif args.command == "index":
            from .service import local_index
            local_index(cfg,store)
        elif args.command == "import-draft":
            print(store.draft(args.id,json.loads(Path(args.json_file).read_text())))
        elif args.command == "run":
            from .service import run
            asyncio.run(run(cfg))
        else:
            asyncio.run(browser_command(cfg,store,args))


async def browser_command(cfg,store,args):
    from .browser import Browser
    from .service import sync
    if args.command == "login":
        cfg["browser"]["headless"] = False
    browser = Browser(cfg,store)
    await browser.start()
    try:
        if args.command == "sync":
            await sync(cfg,store,browser)
        else:
            names = ("canvas","outlook") if args.source == "all" else (args.source,)
            for name in names:
                page = await browser.page(name)
                await page.goto(cfg[name]["url"],wait_until="domcontentloaded")
            await asyncio.to_thread(input,"在浏览器中完成登录和 MFA，然后回到终端按 Enter 保存并关闭。")
    finally:
        await browser.close()


if __name__ == "__main__":
    main()

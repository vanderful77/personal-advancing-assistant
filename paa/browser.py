import asyncio
import io
import json
import re
from html import unescape
from pathlib import Path
from urllib.parse import urlparse, urljoin

from .store import digest


class AuthRequired(RuntimeError):
    pass


class IncompleteSync(RuntimeError):
    pass


def plain(html):
    return unescape(re.sub(r"<[^>]+>", " ", html or ""))


class Browser:
    def __init__(self, cfg, store):
        self.cfg, self.store = cfg, store
        self.pages = {}
        self.context = None

    async def start(self):
        from playwright.async_api import async_playwright
        self.driver = await async_playwright().start()
        options = dict(headless=self.cfg["browser"].get("headless", False),
                       accept_downloads=False, chromium_sandbox=True)
        channel = self.cfg["browser"].get("channel")
        if channel:
            options["channel"] = channel
        self.context = await self.driver.chromium.launch_persistent_context(
            str(Path(self.cfg["data_dir"]) / "browser"), **options)
        self.context.set_default_timeout(15000)
        self.context.set_default_navigation_timeout(45000)

    async def close(self):
        if self.context:
            await self.context.close()
        await self.driver.stop()

    async def page(self, name):
        if name not in self.pages or self.pages[name].is_closed():
            self.pages[name] = await self.context.new_page()
        return self.pages[name]

    async def visit(self, name):
        page = await self.page(name)
        await page.goto(self.cfg[name]["url"], wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
        host = urlparse(page.url).hostname or ""
        expected = urlparse(self.cfg[name]["url"]).hostname
        if host != expected or re.search(r"/(login|signin|auth)(/|$)", urlparse(page.url).path):
            raise AuthRequired(f"{name}: complete login in the dedicated browser")
        return page

    async def canvas_json(self, page, path):
        base = self.cfg["canvas"]["url"]
        url = urljoin(base, path)
        result = []
        visited = set()
        while url:
            if url in visited or len(visited) >= 500:
                raise IncompleteSync("Canvas pagination did not finish")
            if urlparse(url).netloc != urlparse(base).netloc:
                raise ValueError("Cross-origin Canvas pagination rejected")
            visited.add(url)
            # Ordinary signed-in browser request; no access token is extracted or created.
            data = await page.evaluate('''async url => {
              const c = new AbortController(); const t = setTimeout(() => c.abort(), 30000);
              try { const r = await fetch(url, {credentials:'same-origin', signal:c.signal});
                return {status:r.status, type:r.headers.get('content-type'),
                  link:r.headers.get('link'), body:await r.text(), url:r.url};
              } finally {clearTimeout(t);}
            }''', url)
            if data["status"] == 401 or "json" not in (data["type"] or ""):
                raise AuthRequired("Canvas session is unavailable or redirected to login")
            if data["status"] != 200:
                raise IncompleteSync(f"Canvas returned HTTP {data['status']}")
            payload = json.loads(data["body"])
            result.extend(payload if isinstance(payload, list) else [payload])
            match = re.search(r'<([^>]+)>;\s*rel="next"', data["link"] or "")
            url = match.group(1) if match else None
        return result

    async def canvas(self):
        page = await self.visit("canvas")
        courses = await self.canvas_json(page, "/api/v1/courses?enrollment_state=active&per_page=100")
        failures = []
        base = self.cfg["canvas"]["url"]
        for course in courses:
            cid = course["id"]
            if course.get("access_restricted_by_date"):
                continue
            name = course.get("name", str(cid))
            try:
                syllabus = await self.canvas_json(page, f"/api/v1/courses/{cid}?include[]=syllabus_body")
                self.store.document(f"canvas:course:{cid}", "canvas", name,
                                    f"{base}/courses/{cid}", plain(syllabus[0].get("syllabus_body")), {"course":cid})
                assignments = await self.canvas_json(page, f"/api/v1/courses/{cid}/assignments?per_page=100&include[]=submission")
                for a in assignments:
                    key = f"canvas:assignment:{cid}:{a['id']}"
                    due = a.get("due_at")
                    body = plain(a.get("description")) + f"\nDeadline (Canvas due_at): {due or 'not specified'}"
                    self.store.document(key, "canvas", f"{name} — {a['name']}", a["html_url"], body,
                                        {"course":cid, "due":due, "submission":a.get("submission", {}).get("workflow_state")})
                    if due:
                        self.store.event(key, key, f"{name} — {a['name']}", due, None, "deadline", "", due, approved=True)
                self.store.retire_missing(f"canvas:assignment:{cid}:", {f"canvas:assignment:{cid}:{a['id']}" for a in assignments})
                announcements = await self.canvas_json(page, f"/api/v1/courses/{cid}/discussion_topics?only_announcements=true&per_page=100")
                for a in announcements:
                    self.store.document(f"canvas:announcement:{cid}:{a['id']}", "canvas", f"{name} — {a['title']}",
                                        a.get("html_url", f"{base}/courses/{cid}"), plain(a.get("message")), {"course":cid})
                pages = await self.canvas_json(page, f"/api/v1/courses/{cid}/pages?per_page=100")
                for p in pages:
                    key = f"canvas:page:{cid}:{p['page_id']}"
                    revision = str(p.get("updated_at", ""))
                    if revision and self.store.get(key + ":revision") == revision:
                        continue
                    full = (await self.canvas_json(page, f"/api/v1/courses/{cid}/pages/{p['page_id']}"))[0]
                    self.store.document(key, "canvas", f"{name} — {p['title']}", p["html_url"], plain(full.get("body")), {"course":cid})
                    self.store.set(key + ":revision", revision)
                files = await self.canvas_json(page, f"/api/v1/courses/{cid}/files?per_page=100")
                for f in files:
                    if f.get("locked_for_user"):
                        failures.append(f"{name}: locked file {f['id']}")
                        continue
                    key = f"canvas:file:{f['id']}"
                    revision = str([f.get("updated_at"), f.get("size"), f.get("modified_at")])
                    if self.store.get(key + ":revision") == revision:
                        continue
                    content_type = f.get("content-type", "")
                    if not (content_type == "application/pdf" or content_type.startswith("text/")):
                        continue
                    max_bytes = self.cfg["canvas"].get("max_file_mb",20) * 1024 * 1024
                    if f.get("size", 0) > max_bytes:
                        failures.append(f"{name}: file {f['id']} exceeds size limit")
                        continue
                    # Canvas download path, authenticated by the browser context cookies.
                    response = await self.context.request.get(f"{base}/files/{f['id']}/download", timeout=45000)
                    if response.status != 200:
                        raise IncompleteSync(f"File {f['id']} HTTP {response.status}")
                    raw = await response.body()
                    if len(raw) > max_bytes:
                        failures.append(f"{name}: file {f['id']} exceeds size limit")
                        continue
                    if content_type == "application/pdf":
                        if not raw.startswith(b"%PDF"):
                            raise AuthRequired("Expected PDF but received another page")
                        from pypdf import PdfReader
                        reader = PdfReader(io.BytesIO(raw))
                        text = "\n\n".join(f"[Page {i+1}]\n{p.extract_text() or ''}" for i,p in enumerate(reader.pages))
                        if not any(p.extract_text().strip() for p in reader.pages if p.extract_text()):
                            failures.append(f"{name}: file {f['id']} requires OCR")
                    else:
                        text = raw.decode("utf-8", errors="replace")
                    self.store.document(key, "canvas", f"{name} — {f['display_name']}",
                                        f"{base}/courses/{cid}/files/{f['id']}", text, {"course":cid})
                    self.store.set(key + ":revision", revision)
                self.store.status(f"course:{cid}", "ok", name)
            except Exception as exc:
                self.store.status(f"course:{cid}", "error", f"{name}: {type(exc).__name__}")
                failures.append(f"{name}: {type(exc).__name__}")
        if failures:
            raise IncompleteSync("; ".join(failures)[:1500])
        return f"Checked {len(courses)} active courses (external tools and unsupported file formats excluded)"

    async def outlook(self):
        cfg = self.cfg["outlook"]
        page = await self.visit("outlook")
        if not all(cfg.get(k) for k in ("ready_selector", "rows_selector", "reading_pane_selector", "reading_id_attribute")):
            raise IncompleteSync("Outlook UI selectors need local calibration; mailbox has NOT been synchronized")
        await page.locator(cfg["ready_selector"]).wait_for(state="visible")
        rows = page.locator(cfg["rows_selector"])
        count = await rows.count()
        cap = cfg.get("max_messages",50)
        processed = 0
        for i in range(min(count, cap)):
            row = rows.nth(i)
            ident = await row.get_attribute(cfg.get("row_id_attribute", "data-itemid"))
            if not ident:
                raise IncompleteSync("Outlook row has no stable ID; refusing unreliable deduplication")
            title = await row.inner_text()
            await row.click()
            pane = page.locator(cfg["reading_pane_selector"])
            await pane.wait_for(state="visible")
            # A tenant-specific reading pane must identify the CURRENT message.
            # Re-read until its text settles to avoid storing a half-rendered message.
            previous = ""
            settled = False
            for _ in range(15):
                if await pane.get_attribute(cfg["reading_id_attribute"]) != ident:
                    await asyncio.sleep(0.4)
                    continue
                text = await pane.inner_text()
                if text.strip() and text == previous:
                    settled = True
                    break
                previous = text
                await asyncio.sleep(0.4)
            if not settled:
                raise IncompleteSync("Outlook reading pane did not settle")
            url = page.url
            self.store.document("outlook:" + ident, "outlook", title[:300], url, text)
            processed += 1
        # Virtualized mailbox rows are not evidence of full mailbox coverage.
        return f"Read {processed} visible messages only; historical/virtualized mailbox coverage is incomplete"

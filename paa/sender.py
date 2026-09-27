"""Fail-closed Outlook UI sender; selectors must be calibrated on the target tenant."""
import json
from .store import digest


async def send_confirmed(cfg, store, browser):
    scfg = cfg.get("outlook_send",{})
    if not scfg.get("enabled"):
        return
    required = ("compose_selector","to_selector","resolved_to_selector","subject_selector",
                "body_selector","send_selector","sent_selector")
    if not all(scfg.get(k) for k in required):
        store.status("mail_send","unconfigured","Missing verified UI selectors")
        return
    for row in store.db.execute("SELECT * FROM drafts WHERE status='confirmed'").fetchall():
        payload = json.loads(row["payload"])
        if payload.get("attachments"):
            store.status("mail_send","blocked","Attachment sending is not supported in v0.1")
            continue
        doc = store.db.execute("SELECT hash FROM docs WHERE id=?",(payload.get("source_id"),)).fetchone()
        if not doc or doc[0] != payload.get("source_hash"):
            with store.db:
                store.db.execute("UPDATE drafts SET status='review' WHERE id=?",(row["id"],))
            store.status("mail_send","blocked","Source changed; regenerate and reconfirm")
            continue
        claimed = False
        try:
            page = await browser.visit("outlook")
            await page.locator(scfg["compose_selector"]).click()
            await page.locator(scfg["to_selector"]).fill(payload["to"])
            await page.locator(scfg["to_selector"]).press("Enter")
            await page.locator(scfg["subject_selector"]).fill(payload["subject"])
            await page.locator(scfg["body_selector"]).fill(payload["body"])
            # Require one explicit email-address attribute; display names are insufficient.
            resolved = page.locator(scfg["resolved_to_selector"])
            if await resolved.count() != 1:
                raise ValueError("Recipient is ambiguous")
            address = await resolved.get_attribute(scfg.get("recipient_attribute","data-email"))
            subject = await page.locator(scfg["subject_selector"]).input_value()
            body = await page.locator(scfg["body_selector"]).inner_text()
            if address != payload["to"] or subject != payload["subject"] or body != payload["body"]:
                raise ValueError("Rendered draft differs from confirmed version")
            if digest(json.dumps(payload,sort_keys=True,ensure_ascii=False)) != row["version"]:
                raise ValueError("Draft version mismatch")
            sent = page.locator(scfg["sent_selector"])
            if await sent.is_visible():
                raise ValueError("Old sent indicator is visible; cannot verify this send")
            store.claim_send(row["id"],row["version"])
            claimed = True
            await page.locator(scfg["send_selector"]).click()
            await sent.wait_for(state="visible",timeout=15000)
            with store.db:
                store.db.execute("UPDATE drafts SET status='sent' WHERE id=?",(row["id"],))
            store.enqueue("sent:"+row["id"]+row["version"],f"Outlook 已显示发送成功：{payload['subject']}")
            store.status("mail_send","ok")
        except Exception as exc:
            with store.db:
                store.db.execute("UPDATE drafts SET status=? WHERE id=?",("unknown" if claimed else "review",row["id"]))
            store.status("mail_send","unknown" if claimed else "blocked",type(exc).__name__)
            store.enqueue("send-error:"+row["id"]+row["version"],
                          "邮件发送结果不明，请在 Outlook 已发送中核对；不会自动重试。" if claimed else
                          "邮件未发送：页面与已确认草稿不匹配或操作失败，请在桌面检查。")

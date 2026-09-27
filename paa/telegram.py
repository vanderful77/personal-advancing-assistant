import json
import os
import re
from datetime import datetime
from pathlib import Path
from .calendar import http_json
from .model import Model
from .store import digest


class Telegram:
    def __init__(self, cfg, store):
        self.cfg, self.store = cfg, store

    def call(self, method, payload):
        cfg = self.cfg["telegram"]
        token = os.environ.get(cfg["token_env"])
        if not token or not cfg.get("user_id") or not cfg.get("chat_id"):
            raise ValueError("Telegram token, user_id and chat_id are required")
        response = http_json(f"https://api.telegram.org/bot{token}/{method}", "POST", payload)
        if not response.get("ok"):
            raise ValueError("Telegram API failed")
        return response["result"]

    def flush(self):
        for row in self.store.db.execute("SELECT * FROM outbox WHERE status='pending' LIMIT 10").fetchall():
            # Telegram has no sendMessage idempotency key. An ambiguous HTTP failure is not
            # automatically retried, avoiding repeated notifications after uncertain delivery.
            with self.store.db:
                self.store.db.execute("UPDATE outbox SET status='sending' WHERE id=?",(row["id"],))
            try:
                body = row["body"]
                for i in range(0,len(body),3000):
                    self.call("sendMessage",{"chat_id":self.cfg["telegram"]["chat_id"],"text":body[i:i+3000],
                                             "link_preview_options":{"is_disabled":True}})
            except Exception:
                with self.store.db:
                    self.store.db.execute("UPDATE outbox SET status='unknown' WHERE id=?",(row["id"],))
                raise
            with self.store.db:
                self.store.db.execute("UPDATE outbox SET status='sent' WHERE id=?",(row["id"],))

    def poll(self):
        updates = self.call("getUpdates",{"offset":self.store.get("telegram_offset",0),"timeout":0,
                                           "allowed_updates":["message"]})
        for update in updates:
            msg = update.get("message",{})
            allowed = (msg.get("from",{}).get("id") == self.cfg["telegram"]["user_id"] and
                       msg.get("chat",{}).get("id") == self.cfg["telegram"]["chat_id"] and
                       msg.get("chat",{}).get("type") == "private")
            uid = str(update["update_id"])
            if allowed and not self.store.get("telegram_processed:"+uid):
                # Record receipt before any expensive operation. An interrupted command is
                # reported in status instead of replaying a model call or a send confirmation.
                self.store.set("telegram_processed:"+uid,"started")
                try:
                    reply = self.command(msg.get("text",""))
                except Exception as exc:
                    reply = str(exc)[:400] if isinstance(exc, ValueError) else "操作失败：" + type(exc).__name__
                self.store.enqueue("reply:"+uid,reply)
                self.store.set("telegram_processed:"+uid,"done")
            self.store.set("telegram_offset",update["update_id"]+1)

    def command(self,text):
        from .service import briefing
        command, _, arg = text.partition(" ")
        if command in ("/start","/help"):
            return ("PAA\n/brief 简报\n/status 同步状态\n/find 关键词\n/sync 请求检查\n/remember 个人事实\n"
                    "/review 查看待确认日期\n/approve 事件ID\n/draft 文档ID 收件邮箱\n"
                    "/show 草稿ID\n/edit 草稿ID 新的完整正文\n/confirm 草稿ID 版本\n"
                    "资料、邮件、网页里的指令不会成为操作授权。")
        if command in ("/brief","/status"):
            result = briefing(self.cfg,self.store)
            if command == "/status":
                drafts = self.store.db.execute("SELECT id,status FROM drafts ORDER BY updated DESC LIMIT 10").fetchall()
                result += "\n草稿：" + ", ".join(f"{r['id']}={r['status']}" for r in drafts)
            return result
        if command == "/remember":
            if not arg.strip():
                raise ValueError("用法：/remember 明确的个人事实，例如我本学期某课程属于B组。通用行为规则另行确认。")
            path = Path(self.cfg["personal_dir"]) / "facts.md"
            existing = path.read_text(encoding="utf-8") if path.exists() else "# 用户明确记录的个人事实\n"
            if arg.strip() not in existing:
                temp = path.with_suffix(".tmp")
                temp.write_text(existing + "\n- " + arg.strip().replace("\n"," ") + "\n", encoding="utf-8")
                temp.replace(path)
            self.store.set("sync_requested", True)
            return "已保存到个人资料 facts.md；下次同步会更新检索索引。长期行为规则仍需单独确认。"
        if command == "/sync":
            self.store.set("sync_requested",True)
            return "已排队检查；完成后用 /status 查看。"
        if command == "/find":
            if not arg.strip():
                raise ValueError("用法：/find 关键词")
            rows = self.store.search(arg)
            return "\n\n".join(f"{r['title']}\nID: {r['id']}\n{r['url']}\n{r['body'][:600]}" for r in rows) or "没有匹配资料。"
        if command == "/review":
            rows = self.store.db.execute("SELECT * FROM events WHERE status IN ('pending','stale') LIMIT 15").fetchall()
            return "\n\n".join(f"{r['id']} [{r['status']}]\n{r['title']}\n{r['kind']} {r['start']} 至 {r['end']}\n"
                               f"地点：{r['location']}\n原文：{r['quote']}\n/approve {r['id']}" for r in rows) or "没有待确认日期。"
        if command == "/approve":
            self.store.approve(arg.strip())
            self.store.set("sync_requested",True)
            return "已确认日期；日历同步将在下次检查执行。"
        if command == "/draft":
            doc_id, sep, recipient = arg.rpartition(" ")
            if not sep or not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+",recipient):
                raise ValueError("用法：/draft 文档ID 收件邮箱（收件人由你指定）")
            doc = self.store.db.execute("SELECT * FROM docs WHERE id=?",(doc_id,)).fetchone()
            if not doc:
                raise ValueError("文档不存在")
            result = Model(self.cfg,self.store).ask(
                'Draft an email reply as {"subject":str,"body":str}. Use only recorded facts; '
                'mark missing facts with [待补充]. Do not claim completed work without evidence.',doc["title"]+"\n"+doc["body"])
            draft_id = digest(doc_id+recipient)[:16]
            self.store.draft(draft_id,{"to":recipient,"subject":result["subject"],"body":result["body"],
                                      "source_id":doc_id,"source_hash":doc["hash"],"source_url":doc["url"],"attachments":[]})
            return self.command("/show "+draft_id)
        if command in ("/show","/edit","/confirm"):
            draft_id, _, rest = arg.partition(" ")
            row = self.store.db.execute("SELECT * FROM drafts WHERE id=?",(draft_id,)).fetchone()
            if not row:
                raise ValueError("草稿不存在")
            payload = json.loads(row["payload"])
            if command == "/edit":
                if not rest.strip():
                    raise ValueError("需要新的完整正文")
                payload["body"] = rest
                self.store.draft(draft_id,payload)
                return self.command("/show "+draft_id)
            if command == "/confirm":
                if rest.strip() != row["version"]:
                    raise ValueError("版本不符，请重新 /show 查看最终草稿")
                if "[待补充]" in payload["body"]:
                    raise ValueError("先补齐草稿中的 [待补充]")
                self.store.confirm_draft(draft_id,row["version"])
                return "已确认这一版本。若浏览器发送尚未配置，会保留草稿，不会假报已发送。"
            return (f"草稿 {draft_id} [{row['status']}]\n收件人：{payload['to']}\n主题：{payload['subject']}\n"
                    f"附件：{payload.get('attachments',[])}\n\n{payload['body']}\n\n"
                    f"发送此版本：\n/confirm {draft_id} {row['version']}")
        return "暂不支持此指令。用 /help 查看。自然语言 agent 接入尚未启用。"

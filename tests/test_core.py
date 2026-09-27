import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paa.store import Store
from paa.calendar import Calendar
from paa.telegram import Telegram
from paa.service import local_index, analyze


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.addCleanup(self.store.db.close)
        self.store.document("d","canvas","Lab","https://example.edu/d","Deadline 2030-01-02T12:00:00+01:00")

    def event(self):
        self.store.event("e","d","Lab","2030-01-02T12:00:00+01:00",None,
                         "deadline","","2030-01-02T12:00:00+01:00",approved=True)

    def test_unchanged_material_not_reprocessed(self):
        with self.store.db:
            self.store.db.execute("UPDATE jobs SET status='done'")
        self.assertFalse(self.store.document("d","canvas","Lab","https://example.edu/d","Deadline 2030-01-02T12:00:00+01:00"))
        self.assertEqual(self.store.db.execute("SELECT status FROM jobs").fetchone()[0],"done")

    def test_source_change_invalidates_approval(self):
        self.event()
        self.store.document("d","canvas","Lab","https://example.edu/d","new conflicting deadline")
        self.assertEqual(self.store.db.execute("SELECT status FROM events").fetchone()[0],"stale")
        with self.assertRaises(ValueError):
            self.store.approve("e")

    def test_retired_material_not_searchable_or_approvable(self):
        self.event()
        self.store.retire_missing("d",set())
        self.assertEqual(self.store.search("Deadline"),[])
        with self.assertRaises(ValueError):
            self.store.approve("e")

    def test_changed_draft_invalidates_confirmation(self):
        v = self.store.draft("mail",{"to":"test@example.com","body":"First"})
        self.store.confirm_draft("mail",v)
        v2 = self.store.draft("mail",{"to":"test@example.com","body":"Second"})
        with self.assertRaises(ValueError):
            self.store.claim_send("mail",v)
        with self.assertRaises(ValueError):
            self.store.confirm_draft("mail",v)
        self.store.confirm_draft("mail",v2)
        self.store.claim_send("mail",v2)
        with self.assertRaises(ValueError):
            self.store.claim_send("mail",v2)
        with self.assertRaises(ValueError):
            self.store.draft("mail",{"to":"test@example.com","body":"retry"})

    def test_exact_source_quote_and_timezone_required(self):
        with self.assertRaises(ValueError):
            self.store.event("e","d","x","2030-01-02T12:00:00",None,"deadline","","Deadline")
        with self.assertRaises(ValueError):
            self.store.event("e","d","x","2030-01-02T12:00:00Z",None,"deadline","","invented")

    def test_budget_survives_restart_and_denies_overrun(self):
        self.store.reserve("2030-01",19,20)
        with self.assertRaises(ValueError):
            self.store.reserve("2030-01",2,20)
        self.store.reserve("2030-01",1,20)
        with self.assertRaises(ValueError):
            self.store.reserve("2030-01",0.01,20)
        self.store.reserve("2030-02",1,20)

    def test_outbox_deduplication(self):
        self.store.enqueue("brief:today","first")
        self.store.enqueue("brief:today","second")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM outbox").fetchone()[0],1)

    def test_unauthorized_telegram_ignored(self):
        cfg={"telegram":{"user_id":10,"chat_id":10}}
        bot=Telegram(cfg,self.store)
        update={"update_id":5,"message":{"from":{"id":20},"chat":{"id":10,"type":"private"},"text":"/sync"}}
        with patch.object(bot,"call",return_value=[update]):
            bot.poll()
        self.assertFalse(self.store.get("sync_requested",False))
        self.assertEqual(self.store.get("telegram_offset"),6)

    def test_telegram_command_replay_deduplicated(self):
        cfg={"telegram":{"user_id":10,"chat_id":10}}
        bot=Telegram(cfg,self.store)
        update={"update_id":5,"message":{"from":{"id":10},"chat":{"id":10,"type":"private"},"text":"/sync"}}
        with patch.object(bot,"call",return_value=[update]), patch.object(bot,"command",return_value="ok") as cmd:
            bot.poll(); bot.poll()
            self.assertEqual(cmd.call_count,1)

    def test_calendar_primary_rejected(self):
        cfg={"data_dir":self.tmp.name,"calendar":{"calendar_id":"primary"}}
        with self.assertRaises(ValueError):
            Calendar(cfg,self.store).sync()

    def test_calendar_recovery_after_ambiguous_create(self):
        self.event()
        cfg={"data_dir":self.tmp.name,"calendar":{"calendar_id":"test@group.calendar.google.com"}}
        cal=Calendar(cfg,self.store)
        remote={"extendedProperties":{"private":{"paa_id":"e"}}}
        # Remote event exists even if previous process never persisted its local mapping.
        with patch.object(cal,"token",return_value="fake"),patch("paa.calendar.http_json",return_value=remote) as request:
            cal.sync()
        self.assertEqual([c.args[1] if len(c.args)>1 else "GET" for c in request.call_args_list],["GET","PATCH"])

    def test_calendar_skips_stale_source(self):
        self.event()
        self.store.document("d","canvas","Lab","https://example.edu/d","changed")
        cfg={"data_dir":self.tmp.name,"calendar":{"calendar_id":"test@group.calendar.google.com"}}
        cal=Calendar(cfg,self.store)
        with patch.object(cal,"token",return_value="fake"),patch("paa.calendar.http_json") as request:
            self.assertEqual(cal.sync(),0)
            request.assert_not_called()


if __name__ == "__main__":
    unittest.main()

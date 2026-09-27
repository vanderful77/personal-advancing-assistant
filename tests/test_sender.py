import json
import tempfile
import unittest
from unittest.mock import AsyncMock
from paa.sender import send_confirmed
from paa.store import Store


class SenderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name)
        self.addCleanup(self.store.db.close)
        self.store.document("mail","outlook","Question","https://outlook.office.com/mail/id/1","Please reply")
        source=self.store.db.execute("SELECT hash FROM docs").fetchone()[0]
        self.payload={"to":"x@example.com","subject":"Reply","body":"Confirmed content", "attachments":[],
                      "source_id":"mail","source_hash":source}
        self.version=self.store.draft("d",self.payload)
        self.store.confirm_draft("d",self.version)
        keys=("compose_selector","to_selector","resolved_to_selector","subject_selector","body_selector","send_selector","sent_selector")
        self.cfg={"outlook_send":{"enabled":True,**{k:k for k in keys}}}
        self.locators={k:AsyncMock() for k in keys}
        self.locators["resolved_to_selector"].count.return_value=1
        self.locators["resolved_to_selector"].get_attribute.return_value="x@example.com"
        self.locators["subject_selector"].input_value.return_value="Reply"
        self.locators["body_selector"].inner_text.return_value="Confirmed content"
        self.locators["sent_selector"].is_visible.return_value=False
        class Page:
            def __init__(self,locators): self.locators=locators
            def locator(self,selector): return self.locators[selector]
        self.browser=AsyncMock()
        self.browser.visit.return_value=Page(self.locators)

    def state(self):
        return self.store.db.execute("SELECT status FROM drafts WHERE id='d'").fetchone()[0]

    async def test_modified_rendered_body_never_sent(self):
        self.locators["body_selector"].inner_text.return_value="Other content"
        await send_confirmed(self.cfg,self.store,self.browser)
        self.locators["send_selector"].click.assert_not_called()
        self.assertEqual(self.state(),"review")

    async def test_send_timeout_not_retried(self):
        self.locators["sent_selector"].wait_for.side_effect=TimeoutError()
        await send_confirmed(self.cfg,self.store,self.browser)
        self.assertEqual(self.state(),"unknown")
        await send_confirmed(self.cfg,self.store,self.browser)
        self.assertEqual(self.locators["send_selector"].click.call_count,1)

    async def test_send_success_only_once(self):
        await send_confirmed(self.cfg,self.store,self.browser)
        await send_confirmed(self.cfg,self.store,self.browser)
        self.assertEqual(self.locators["send_selector"].click.call_count,1)
        self.assertEqual(self.state(),"sent")

    async def test_source_change_revokes_confirmation(self):
        self.store.document("mail","outlook","Question","https://outlook.office.com/mail/id/1","Actually cancel")
        await send_confirmed(self.cfg,self.store,self.browser)
        self.browser.visit.assert_not_called()
        self.assertEqual(self.state(),"review")

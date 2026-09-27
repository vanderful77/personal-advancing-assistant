# PAA development constraints

Read README.md and docs/ubuntu.md before extending connectors. Target is Ubuntu 24.04 desktop.
The owner chose normal browser sessions for Canvas and school Outlook; do not replace them with
Graph/Canvas token authorization without an explicit change of direction.

Never commit config.toml, browser profiles, credentials, mailbox/course data, runtime database or
personal memory. Only code, example config and confirmed general skills belong in Git.

Do not claim complete source coverage from a partially rendered/virtualized page. Login failure,
missing selectors, oversized documents and missing OCR must be visible source status.

Email confirmation binds all recipients, subject, body and attachments. Any edit invalidates it.
Persist send state before clicking Send. An ambiguous outcome is not retryable until reconciled.
No real mail/calendar writes or live account login in automated tests. Use fixtures/mocks.

Use python3 -m unittest discover -s tests -v for changes affecting these invariants.
Keep the runtime usable without a desktop conversation window. Document graph-session limitations.

"""Hosted CLI backends as adapters (#991).

Each backend is a transport (the provider CLI's process and its wire protocol) plus a pure translator from the
vendor's records to the neutral events in `harness.backends.events`. The Runner applies those events in one place
(`Runner._apply`): admission, credentials, approvals, taint, rate-limit stops and crash recovery never see a vendor's
format, and a translator can be tested on a recorded transcript with no Manager, database or Docker.
"""

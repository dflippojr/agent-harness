# Stand-in Stables shell for the Server's tests

Stables, the browser client, lives in dflippojr/stables (#334). The Server's own tests check how it serves a shell
(the `/` and `/static` mounts, gzip, ETags, the files allowed before sign-in, security headers) without the client:
`tests/conftest.py` points `HARNESS_STABLES_DIR` at this directory. The files only need the names the Server routes
by and enough text to compress; they are not the client. The Playwright journeys in `tools/ui-tests` run against the
real client at the pinned tag (`tools/stables/pin.json`).

"""Transactional outbox module (durable delivery plan P1, blueprint §19).

``outbox_events`` is the durable intent-to-publish queue that stands between
the API and the Redis broker: an accepted durable job and its
``job.dispatch_requested`` event are written in one PostgreSQL transaction, so
a broker outage can never lose an accepted job. This module owns the outbox
data contract, its strict internal payload contracts, its query helpers and
the service boundaries the coordinator and scheduling service build on in
later checkpoints. It is internal infrastructure: there is no API route, and
nothing in this module depends on HTTP, permissions or tenant sessions.
"""

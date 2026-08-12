"""The read-only serving layer over the platform metadata store.

Everything this package returns comes from PostgreSQL -- ``platform.*`` and
``gold.*`` -- and never from the lake. See ADR 0016 for why that is a boundary
rather than an implementation detail.

Nothing is re-exported here on purpose: importing this package must not pull in
FastAPI, so that ``confidentia.cli`` stays usable with only the base dependencies
installed. Import ``confidentia.api.app.create_app`` directly.
"""

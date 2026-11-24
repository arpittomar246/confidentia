# The API: FastAPI over the platform metadata store.
#
# The smallest image in the stack, and deliberately so. The API reads PostgreSQL
# and never the lake (ADR 0016), so it needs no JVM, no Spark, no Delta and no
# key material -- which is why it can be `python:3.11-slim` plus two extras while
# the Spark image is an order of magnitude larger.
FROM python:3.11-slim AS runtime

# PYTHONDONTWRITEBYTECODE: the image is read-only in practice, so .pyc files would
# be written once per container start and never reused.
# PYTHONUNBUFFERED: without it, structlog output sits in a pipe buffer and
# `docker logs` shows nothing until the buffer fills, which looks like a hang.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Debian's own security updates, applied at build time.
#
# The base is the floating `python:3.11-slim` tag, so the usual remedy for a CVE
# in the base -- bump to a rebuilt image -- only works once upstream rebuilds.
# The weekly scan of 2026-11-23 went red on zlib1g CVE-2026-85091, a heap
# overflow in gz_vacate(): Debian had published the fix as
# 1:1.3.dfsg+really1.3.1-1+deb13u1 and the python image had not been rebuilt
# against it. Upgrading here takes the fix on the next build instead of waiting
# for someone else's release cycle.
#
# This is the Debian half of what docker/frontend/Dockerfile does with
# `apk upgrade --no-cache`, for the same reason and after the same failure. It
# also means the next base CVE that has a published fix is picked up by the
# weekly rebuild with no edit here -- which is what a scheduled gate needs, since
# the whole point of running it on a schedule is to catch advisories that land
# while the code is standing still.
#
# `upgrade` rather than `dist-upgrade`: a security point release never needs to
# remove a package, and an image build is the wrong place to let it try.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/confidentia

# The build tooling that ships inside the base image, brought current. Trivy found
# fixable HIGH findings here and every one was build machinery rather than anything
# this project imports -- CVE-2026-24049 in `wheel`, CVE-2026-23949 in
# `jaraco.context`. Cheap to fix and pointless to argue about.
RUN pip install --no-cache-dir --upgrade "pip" "setuptools" "wheel>=0.46.2" "jaraco.context>=6.1.0"

# Project metadata first, as its own layer: pyproject.toml changes far less often
# than the source, so editing a router does not reinstall psycopg.
COPY pyproject.toml README.md ./
COPY src/confidentia/__init__.py src/confidentia/__init__.py

# Only [api,storage]. Adding `spark` here would pull ~400 MB of pyspark into an
# image that must never open a Spark session -- and would put the code that reads
# encrypted Delta inside a container that holds no keys, which is a confusing
# thing to leave lying around.
RUN pip install --no-cache-dir ".[api,storage]"

COPY src/ src/
COPY docs/api/openapi.json docs/api/openapi.json

# Installed after the source is present so the console script resolves. `--no-deps`
# because everything is already installed above and re-resolving would undo the
# layer caching this file is arranged around.
RUN pip install --no-cache-dir --no-deps -e .

# pip removed from the runtime image, last of all.
#
# Two reasons, and the second is the one that matters. Trivy reported
# `setuptools 70.3.0` in this image while site-packages held 84.0.0 -- the finding
# was pip's own **vendored** `pkg_resources`, which the scanner detects as a
# separate package. Removing pip removes it, and the number becomes true rather
# than argued about.
#
# And a runtime container has no business installing packages: `pip install`
# inside a running service is how a compromised process fetches its second stage.
# The editable install above keeps working -- it is a `.pth` file, not a pip
# feature.
RUN pip uninstall --yes pip setuptools wheel \
    && rm -rf /usr/local/lib/python3.11/ensurepip

# Non-root. The API holds no key material and writes nothing to disk, so there is
# no reason for it to run as root, and every reason not to given it is the one
# service in the stack with a port open to the outside.
RUN useradd --create-home --uid 10001 confidentia \
    && chown -R confidentia:confidentia /opt/confidentia
USER confidentia

EXPOSE 8000

# API_HOST is set to 0.0.0.0 by compose. It is *not* defaulted here: an image that
# binds all interfaces by default is one that exposes an unauthenticated service
# the first time someone runs it with --network host.
CMD ["confidentia", "api", "serve"]

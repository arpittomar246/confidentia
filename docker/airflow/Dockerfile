# Airflow: orchestration.
#
# Built on the official image rather than pip-installing Airflow onto a plain
# Python base. Airflow's dependency set is only installable against the upstream
# constraints file for its exact version and Python minor -- `scripts/install_airflow.sh`
# does exactly that locally -- and the official image is that resolution, already
# solved and tested.
#
# The version and Python minor must match `scripts/install_airflow.sh`, or the DAGs
# are being tested locally against a different Airflow than the one shipped.
FROM apache/airflow:2.11.2-python3.11

USER root

# The DAGs shell out to `confidentia ...` rather than importing the package
# (ADR 0013), so this image needs the CLI on PATH -- but it does **not** need
# Spark. A BashOperator that runs a Spark stage does so by invoking the CLI in the
# spark container, not in this one, and installing pyspark here would add ~400 MB
# to an image that would never use it.
#
# git is needed by nothing at runtime; build-essential is what psycopg's optional
# C paths want if a wheel is unavailable for this platform.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

USER airflow

WORKDIR /opt/confidentia

COPY --chown=airflow:root pyproject.toml README.md ./
COPY --chown=airflow:root src/confidentia/__init__.py src/confidentia/__init__.py

# Installed as the airflow user into its own environment, which is what puts
# `confidentia` on the PATH a BashOperator inherits.
#
# No constraints file here: these are the project's own dependencies, and pinning
# them to Airflow's constraints would silently downgrade psycopg to whatever
# Airflow happens to pin. If that ever conflicts, the conflict should be visible
# rather than resolved in Airflow's favour by default.
RUN pip install --no-cache-dir ".[storage,ingestion,cdc]"

COPY --chown=airflow:root src/ src/
RUN pip install --no-cache-dir --no-deps -e .

# Baked in as well as bind-mounted by compose. The mount is what makes editing a
# DAG a restart rather than a rebuild; the copy is what makes the image runnable
# on its own, which is what a cloud deployment needs.
COPY --chown=airflow:root orchestration/dags/ /opt/airflow/dags/

ENV AIRFLOW__CORE__DAGS_FOLDER=/opt/airflow/dags \
    AIRFLOW__CORE__LOAD_EXAMPLES=False \
    PYTHONUNBUFFERED=1

# `standalone` runs the scheduler, the API server and a triggerer in one process
# group with a LocalExecutor. That is the right shape for a single-node demo and
# the wrong shape for anything real -- production splits them so a scheduler
# restart does not take the UI with it. Named here rather than assumed.
CMD ["airflow", "standalone"]

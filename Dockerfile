FROM postgres:15-bookworm AS postgres15-tools
FROM postgres:16-bookworm AS postgres16-tools

FROM python:3.14.5-slim AS sqlite-build
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev && rm -rf /var/lib/apt/lists/*
WORKDIR /build
# Use a WAL-reset-fix release, independent of the base distribution's SQLite.
RUN python -c "import urllib.request,hashlib; b=urllib.request.urlopen('https://www.sqlite.org/2026/sqlite-autoconf-3530400.tar.gz',timeout=60).read(); assert hashlib.sha3_256(b).hexdigest() == '454e45f61c6bd75b7420e7190732dea03ce6639c63ada47bbc592f67fc340338'; open('sqlite.tar.gz','wb').write(b)"
RUN tar -xzf sqlite.tar.gz && mkdir /sqlite && gcc -O2 -fPIC -shared -DSQLITE_THREADSAFE=1 -DSQLITE_ENABLE_FTS5 -Wl,-soname,libsqlite3.so.0 sqlite-autoconf-3530400/sqlite3.c -o /sqlite/libsqlite3.so.0 -lm -ldl -lpthread

FROM python:3.14.5-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 \
    TOKENIZERS_PARALLELISM=false OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 \
    LD_LIBRARY_PATH=/opt/sqlite DATABASE_PROVIDER=sqlite DATABASE_URL=sqlite:///data/feedback.sqlite
COPY --from=sqlite-build /sqlite /opt/sqlite
WORKDIR /app
# Matching-major tools keep backup archives restorable on every supported server.
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client && rm -rf /var/lib/apt/lists/*
COPY --from=postgres15-tools /usr/lib/postgresql/15/bin/pg_dump /usr/lib/postgresql/15/bin/pg_restore /opt/postgresql/15/bin/
COPY --from=postgres16-tools /usr/lib/postgresql/16/bin/pg_dump /usr/lib/postgresql/16/bin/pg_restore /opt/postgresql/16/bin/
RUN /opt/postgresql/15/bin/pg_dump --version && /opt/postgresql/15/bin/pg_restore --version && \
    /opt/postgresql/16/bin/pg_dump --version && /opt/postgresql/16/bin/pg_restore --version && \
    /usr/lib/postgresql/17/bin/pg_dump --version && /usr/lib/postgresql/17/bin/pg_restore --version
COPY requirements.lock requirements-ai.lock pyproject.toml LICENSE ./
# Obtain the pinned CPU wheel from its official index; never resolve a CUDA build.
# Both lock files are complete, and no installation step acquires model weights.
RUN pip install --no-cache-dir --no-deps --only-binary=:all: torch==2.14.1+cpu --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir --no-deps --only-binary=:all: -r requirements.lock -r requirements-ai.lock && \
    pip check
COPY feedback ./feedback
RUN pip install --no-cache-dir --no-deps . && pip check && \
    useradd --uid 10001 --create-home feedback && mkdir /data && chown feedback:feedback /data && \
    python -c "import sqlite3; assert sqlite3.sqlite_version == '3.53.4'"
USER feedback
EXPOSE 8000
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8000')+'/readyz',timeout=3)"
CMD ["python", "-m", "feedback"]

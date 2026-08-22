# Ghost Network Watch monthly pipeline container.
# Runs the full chain: crawl, parse, refs, compact, flags, score, site, notify.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl unzip ca-certificates \
    && curl -sSL "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o /tmp/awscli.zip \
    && unzip -q /tmp/awscli.zip -d /tmp && /tmp/aws/install && rm -rf /tmp/aws /tmp/awscli.zip \
    && apt-get purge -y unzip && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /gnw

COPY pipeline/ pipeline/
RUN pip install --no-cache-dir ./pipeline requests ijson pyarrow duckdb jinja2 openpyxl

# Static inputs the pipeline reads from the repo (seed list, platform map,
# taxonomy archive, site templates and assets).
COPY scoping/data/mr-puf-2026.csv scoping/data/
COPY scoping/evidence/hosting_platform_landscape.json scoping/evidence/
COPY scoping/evidence/nucc_taxonomy_261.csv scoping/evidence/
COPY site/templates/ site/templates/
COPY site/assets/ site/assets/
COPY ops/run_monthly.sh ops/run_monthly.sh
RUN chmod +x ops/run_monthly.sh

ENTRYPOINT ["/gnw/ops/run_monthly.sh"]

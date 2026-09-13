FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . 'psycopg[binary]>=3.2,<4'
COPY benchmarks/__init__.py ./benchmarks/
COPY benchmarks/analysis/__init__.py benchmarks/analysis/cycle_report.py ./benchmarks/analysis/
COPY benchmarks/portal/__init__.py benchmarks/portal/store.py benchmarks/portal/server.py benchmarks/portal/documentation.py ./benchmarks/portal/
ENV OPENSSL_armcap=0
ENV HOST=0.0.0.0 PORT=8080 PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
CMD ["python", "-m", "benchmarks.portal.server"]

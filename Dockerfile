FROM python:3.12-slim

WORKDIR /app

# neonize depends on python-magic, which needs the actual libmagic shared
# library at runtime (not just the Python wrapper pip installs). The slim
# base image doesn't include it by default. Package name has diverged
# between distros/releases (libmagic1 vs libmagic1t64), so try both.
RUN apt-get update \
    && (apt-get install -y --no-install-recommends libmagic1 \
        || apt-get install -y --no-install-recommends libmagic1t64) \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first (separate layer) so code-only changes don't
# force a reinstall of neonize and friends on every deploy.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]

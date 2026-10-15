# One image for the whole agent stack. docker-compose.yml runs it twice: the API and the Streamlit UI.
# Secrets (ANTHROPIC_API_KEY, JWT_SECRET) are passed at RUN time; nothing secret is ever built into the image.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AIRLINE_DB=/app/data/airline.db

WORKDIR /app

# 1. dependencies first: this layer is cached until requirements-agent.txt changes
COPY requirements-agent.txt .
RUN pip install --no-cache-dir -r requirements-agent.txt

# 2. app code last: editing a .py file doesn't re-run the slow pip install above
COPY *.py agent_evals.json ./

# 3. don't run as root; the only writable place is the data folder for SQLite
RUN useradd --create-home appuser && mkdir -p /app/data && chown appuser /app/data
USER appuser

EXPOSE 8000 8501
CMD ["python", "-m", "uvicorn", "airline_service:app", "--host", "0.0.0.0", "--port", "8000"]

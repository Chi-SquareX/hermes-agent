FROM python:3.13-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OAUTHLIB_INSECURE_TRANSPORT=1 \
    OAUTHLIB_RELAX_TOKEN_SCOPE=1 \
    HERMES_PORT=8010

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py expenses.py profiles.py gauth.py hermes_oauth_server.py ./

EXPOSE 8010

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${HERMES_PORT}"]

FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY line_bot.py .
COPY supa.py .

ENV PORT=8080
EXPOSE 8080

CMD gunicorn --bind 0.0.0.0:$PORT --workers 2 --timeout 60 line_bot:app

FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py mlb_monitor.py message_formatter.py abs_tracker.py ./

CMD ["python", "-u", "bot.py"]

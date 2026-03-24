FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Default: single cycle (use CMD override or --loop for continuous mode)
ENTRYPOINT ["python", "-m", "bot.main"]
CMD ["--config", "config.yml"]

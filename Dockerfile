FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY fallback_todos.py .

EXPOSE 8000

CMD ["python", "fallback_todos.py", "--host", "0.0.0.0", "--port", "8000", "--logs-dir", "/app/logs/fallback"]

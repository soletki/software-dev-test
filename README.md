# Fallback TODO API

## Start with Docker

```bash
docker compose up -d --build
```

## Endpoints

- Todos: `http://localhost:8000/todos`
- Metrics: `http://localhost:8000/metrics`
- Prometheus UI: `http://localhost:9090`

## Simulate requests

Normal request:
```bash
curl "http://localhost:8000/todos?limit=3"
```

Fallback request:
```bash
curl "http://localhost:8000/todos?limit=1&force_fallback=true"
```

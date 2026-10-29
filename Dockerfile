FROM python:3.11-slim
WORKDIR /app
COPY . .
ENV HOST=0.0.0.0 SESSIONS=1 LLM_MODE=demo PYTHONUNBUFFERED=1
EXPOSE 8765
CMD ["python", "-m", "engine.server"]

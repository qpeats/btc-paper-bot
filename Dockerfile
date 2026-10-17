FROM python:3.12-slim
WORKDIR /app
COPY btcbot ./btcbot
COPY web ./web
COPY bot.py ./
ENV PYTHONUNBUFFERED=1 PORT=8000
EXPOSE 8000
CMD ["python", "web/server.py"]

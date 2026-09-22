FROM python:3.13-slim
WORKDIR /app
COPY gov_bot.py review_tools.py ./
COPY knowledge/ knowledge/
# Mount a Railway volume at /data so the "last seen proposal" survives redeploys.
ENV PYTHONUNBUFFERED=1 STATE_FILE=/data/state.json
CMD ["python", "gov_bot.py"]

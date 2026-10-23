FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY soga_line_bot/ soga_line_bot/
COPY weekly_defaults.txt .
# 登記資料在這裡，compose 掛成 volume，重建容器不會不見。
VOLUME /data
EXPOSE 2160
# 一個 worker：SQLite 檔只給一個程序寫最單純；幾條 thread 就夠應付一個群組的量。
CMD ["gunicorn", "--workers", "1", "--threads", "4", "--bind", "0.0.0.0:2160", \
     "--access-logfile", "-", "soga_line_bot.app:create_app()"]

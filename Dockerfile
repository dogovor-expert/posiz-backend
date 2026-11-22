FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# librsvg2-bin — растеризатор карточек (SVG → PNG). Он же тянет шрифты и библиотеки,
# которые нужны Chromium, если растр когда-нибудь переключат на него.
# Без него медиа-конвейер работает, но отдаёт только SVG — и release gate это показывает.
RUN apt-get update \
 && apt-get install -y --no-install-recommends librsvg2-bin ffmpeg fontconfig fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*
COPY src ./src
# схема и миграции нужны и api (alembic upgrade head), и воркеру
COPY alembic.ini ./
COPY migrations ./migrations
COPY schema.sql ./
COPY sql ./sql
ENV PYTHONPATH=/app/src
# ffmpeg/ffprobe ставим явно: кодировать ролики и проверять их длительность
ENV MEDIA_FFMPEG_BIN=/usr/bin/ffmpeg MEDIA_FFPROBE_BIN=/usr/bin/ffprobe
# каталог медиа-спула: общий том API и воркера, владелец — непривилегированный пользователь
RUN mkdir -p /var/lib/posiz/media
# непривилегированный пользователь: контейнер не должен работать от root
RUN useradd --create-home --uid 10001 orbita \
 && chown -R orbita:orbita /app /var/lib/posiz
USER orbita
EXPOSE 8000
CMD ["uvicorn","orbita.main:app","--host","0.0.0.0","--port","8000"]

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# fonts-dejavu-core нужен app/render.py: без кириллического шрифта картинка
# расписания не соберётся (см. FontsNotFound). ca-certificates — для SSL.
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-dejavu-core ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Корпоративный MITM: и pip install, и рантайм-соединение с TiMe идут через
# корпоративный прокси, чей корень не входит в системный store. Добавляем
# корпоративные корни к системным и подставляем общий бандл в SSL_CERT_FILE.
# certs/corporate-ca.crt не коммитится (см. .gitignore) — на других машинах
# положить свой бандл или убрать эти строки.
COPY certs/corporate-ca.crt /tmp/corporate-ca.crt
RUN cat /etc/ssl/certs/ca-certificates.crt /tmp/corporate-ca.crt > /etc/ssl/certs/all-ca.crt \
 && rm /tmp/corporate-ca.crt
ENV SSL_CERT_FILE=/etc/ssl/certs/all-ca.crt

WORKDIR /app

# зависимости отдельным слоем — чтобы правки кода не пересобирали pip install
# --trusted-host: pypi в корпоративной сети уходит через MITM-прокси, чей корень
# не в системном store; build-only, на рантайм не влияет.
COPY requirements.txt .
RUN pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org -r requirements.txt

COPY app ./app
COPY tests ./tests
COPY seed_trainings.py ./

RUN useradd --create-home --uid 1000 bot && chown -R bot:bot /app
USER bot

CMD ["python", "-m", "app.bot"]

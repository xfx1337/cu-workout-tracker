FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# fonts-dejavu-core нужен app/render.py: без кириллического шрифта картинка
# расписания не соберётся (см. FontsNotFound). ca-certificates — для SSL.
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-dejavu-core ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Корпоративный MITM: в сети ЦУ и pip, и соединение с TiMe идут через прокси,
# чей корень не входит в системный набор. Если в certs/ лежат .crt — подмешиваем
# их к системным; если нет — берём только системные.
#
# Сертификаты в git не попадают (см. .gitignore), в репозитории лежит только
# certs/.gitkeep. Благодаря этому образ собирается и там, где прокси нет:
# при обязательном COPY конкретного файла сборка падала бы на его отсутствии.
COPY certs/ /tmp/certs/
RUN if ls /tmp/certs/*.crt >/dev/null 2>&1; then \
        cat /etc/ssl/certs/ca-certificates.crt /tmp/certs/*.crt > /etc/ssl/certs/all-ca.crt; \
        echo "подмешаны корпоративные корни"; \
    else \
        cp /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/all-ca.crt; \
        echo "корпоративных корней нет, только системные"; \
    fi \
 && rm -rf /tmp/certs
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

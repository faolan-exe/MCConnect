# --- build the minecraft plugin and the official addons (the store) ---
FROM maven:3.9-eclipse-temurin-17 AS plugin
WORKDIR /build
COPY ["java plugin/", "./"]
# the sources of the addons are in /build/addons, the jars go to /out
RUN mvn -q -B package && mkdir -p /out/addons && cp MCDataLink/target/MCDataLink-*.jar /out/MCDataLink.jar \
    && cp addons/*/target/*.jar /out/addons/

# --- web server / socket server ---
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MCC_PLUGIN_JAR=/app/plugin/MCDataLink.jar \
    MCC_ADDON_DIR=/app/addons \
    MCC_UPLOAD_DIR=/app/uploads
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY database ./database
COPY mc_socket ./mc_socket
COPY web ./web
COPY --from=plugin /out/MCDataLink.jar ./plugin/MCDataLink.jar
COPY --from=plugin /out/addons ./addons

# /tls: the socket server's own certificate (a docker volume takes over the owner on the first start)
RUN useradd --system --create-home --uid 1000 mcconnect && mkdir -p logs uploads /tls && chown mcconnect logs uploads /tls
USER mcconnect

EXPOSE 8000 9991
# Web server by default; the socket server runs the same image with `python mc_socket/main.py`.
CMD ["gunicorn", "--worker-class", "gthread", "--workers", "2", "--threads", "32", \
     "--bind", "0.0.0.0:8000", "--error-logfile", "-", "web.main:create_app()"]

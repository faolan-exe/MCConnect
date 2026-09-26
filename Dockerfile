# --- build the minecraft plugin ---
FROM maven:3.9-eclipse-temurin-17 AS plugin
WORKDIR /build
COPY ["java plugin/MCDataLink/pom.xml", "./"]
RUN mvn -q -B dependency:go-offline
COPY ["java plugin/MCDataLink/src", "./src"]
RUN mvn -q -B package && cp target/MCDataLink-*.jar /build/MCDataLink.jar

# --- web server / socket server ---
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MCC_PLUGIN_JAR=/app/plugin/MCDataLink.jar \
    MCC_UPLOAD_DIR=/app/uploads
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY database ./database
COPY mc_socket ./mc_socket
COPY web ./web
COPY --from=plugin /build/MCDataLink.jar ./plugin/MCDataLink.jar

RUN useradd --system --create-home --uid 1000 mcconnect && mkdir -p logs uploads && chown mcconnect logs uploads
USER mcconnect

EXPOSE 8000 9991
# Web server by default; the socket server runs the same image with `python mc_socket/main.py`.
CMD ["gunicorn", "--worker-class", "gthread", "--workers", "2", "--threads", "32", \
     "--bind", "0.0.0.0:8000", "--error-logfile", "-", "web.main:create_app()"]

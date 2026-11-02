FROM python:3.12-alpine
RUN apk add --no-cache 7zip
WORKDIR /app
COPY fastshare_arr ./fastshare_arr
ENV PYTHONUNBUFFERED=1 CONFIG_DIR=/config PORT=8085
EXPOSE 8085
VOLUME /config
HEALTHCHECK --interval=30s CMD wget -qO- http://127.0.0.1:8085/healthz || exit 1
CMD ["python", "-m", "fastshare_arr"]

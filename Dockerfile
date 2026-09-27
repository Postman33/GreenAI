FROM golang:1.25.5-bookworm AS go-builder

WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY parser/dxf_extract_go ./parser/dxf_extract_go
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -o /out/dxf_extract_go ./parser/dxf_extract_go

FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MPLBACKEND=Agg

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash libgomp1 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY scripts ./scripts
COPY config ./config
COPY models/utility_detector/latest ./models/utility_detector/latest
COPY --from=go-builder /out/dxf_extract_go /usr/local/bin/dxf_extract_go

RUN chmod +x /app/scripts/run_pipeline.sh /usr/local/bin/dxf_extract_go \
    && mkdir -p /data/output

ENTRYPOINT ["/app/scripts/run_pipeline.sh"]
CMD ["/data/input.dxf", "/data/output"]

# Build (embedded — model weights bundled in the binary)
FROM golang:1.26-alpine AS build
RUN apk add --no-cache git
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=1 go build -tags embedded -o /out/hyatlas-go .

# Runtime
FROM alpine:3.19
RUN apk add --no-cache ca-certificates libgcc
COPY --from=build /out/hyatlas-go /usr/local/bin/hyatlas-go
EXPOSE 19528
ENV HYATLAS_PORT=19528
HEALTHCHECK --interval=15s --timeout=5s --retries=8 \
  CMD wget -qO- http://127.0.0.1:19528/healthz || exit 1
ENTRYPOINT ["/usr/local/bin/hyatlas-go"]

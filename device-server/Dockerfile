# syntax=docker/dockerfile:1
FROM golang:1.26.10 AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -o /out/device-server ./cmd/server

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/device-server /device-server
ENTRYPOINT ["/device-server"]

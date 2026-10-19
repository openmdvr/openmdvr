package storage

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// TestUpload_SendsExplicitContentLength is a regression test for a bug found
// with the first clip uploaded by a real JC261: passing an http.Request body
// straight to s3.PutObjectInput.Body (an io.Reader of unknown length) let the
// AWS SDK attempt streaming without Content-Length, which Cloudflare R2
// rejects with "411 MissingContentLength". This test runs a fake
// S3-compatible server (it only captures the request the SDK sends, no real
// SigV4 needed) to confirm Upload() always sends an explicit Content-Length,
// without requiring real R2 credentials.
func TestUpload_SendsExplicitContentLength(t *testing.T) {
	const body = "test content of a fake .ts clip"

	var gotContentLength int64 = -1
	var gotTransferEncoding string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotContentLength = r.ContentLength
		gotTransferEncoding = strings.Join(r.TransferEncoding, ",")
		w.Header().Set("ETag", `"fake-etag"`)
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	client := New(Config{
		Endpoint:  srv.URL,
		AccessKey: "test",
		SecretKey: "test",
		Bucket:    "test-bucket",
	})

	err := client.Upload(context.Background(), "tenants/fake/alarm-clips/fake.ts", strings.NewReader(body))
	if err != nil {
		t.Fatalf("Upload() error = %v, want nil", err)
	}

	if gotContentLength != int64(len(body)) {
		t.Errorf("received Content-Length = %d, want %d -- without it, R2 rejects the upload with 411 MissingContentLength", gotContentLength, len(body))
	}
	if gotTransferEncoding == "chunked" {
		t.Errorf("Transfer-Encoding = chunked, want an explicit Content-Length (no length-less streaming)")
	}
}

func TestUpload_NotConfigured_ReturnsClearError(t *testing.T) {
	client := New(Config{})
	err := client.Upload(context.Background(), "k", strings.NewReader("x"))
	if err == nil {
		t.Fatal("Upload() error = nil, want error -- a client without ENDPOINT/BUCKET must fail clearly, not with a nil pointer panic")
	}
}

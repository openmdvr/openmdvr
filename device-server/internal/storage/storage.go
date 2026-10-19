// Package storage is the object storage abstraction layer: business code
// never uses a proprietary SDK directly. Its first consumer is alarm video
// clip retrieval -- the server receives the file the device uploads and
// stores it here.
//
// Provider: Cloudflare R2 (S3-compatible, no egress fees). It uses the
// official AWS SDK for Go (aws-sdk-go-v2/service/s3) pointed at the R2
// endpoint via BaseEndpoint, which is the integration path R2 documents.
package storage

import (
	"bytes"
	"context"
	"fmt"
	"io"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
)

// Config holds the bucket credentials/location -- always from environment
// variables, never committed.
type Config struct {
	// Endpoint is the account's S3-compatible endpoint URL, e.g.
	// "https://<account_id>.r2.cloudflarestorage.com" -- WITHOUT the bucket
	// name (that goes in Bucket).
	Endpoint  string
	AccessKey string
	SecretKey string
	Bucket    string
}

// Client uploads objects to the configured bucket. It deliberately exposes
// no reads or signed URLs -- those live in the API (Python,
// api/app/storage.py), which is what talks to the frontend; this Go client
// only writes the bytes the server receives from devices.
type Client struct {
	s3     *s3.Client
	bucket string
}

// New builds the client. Empty-safe: if cfg.Endpoint/Bucket are empty
// (deployment without this integration configured, or tests), it returns a
// *Client whose Upload fails with a clear error instead of a nil pointer
// panic.
func New(cfg Config) *Client {
	if cfg.Endpoint == "" || cfg.Bucket == "" {
		return &Client{bucket: ""}
	}
	awsCfg := aws.Config{
		Region:      "auto", // R2 has no real regions -- "auto" is the documented value
		Credentials: credentials.NewStaticCredentialsProvider(cfg.AccessKey, cfg.SecretKey, ""),
	}
	client := s3.New(s3.Options{
		Region:       awsCfg.Region,
		Credentials:  awsCfg.Credentials,
		BaseEndpoint: aws.String(cfg.Endpoint),
	})
	return &Client{s3: client, bucket: cfg.Bucket}
}

// Upload stores the contents of r in the bucket under key. The caller must
// ensure key follows the mandatory `tenants/<tenant_id>/...` convention (the
// same one enforced by the CHECKs on alarms.video_evidence_key and
// alarm_video_clips.storage_key in the database -- that is the real barrier;
// this function does not re-validate it).
//
// It buffers the WHOLE body before calling PutObject. Passing r (an
// io.Reader of unknown length, e.g. an http.Request body) directly as Body
// made the AWS SDK attempt streaming without Content-Length, which R2 rejects
// with "411 MissingContentLength: You must provide the Content-Length HTTP
// header" (unlike real S3). The caller (alarmclip.handleUpload) already caps
// the body size with http.MaxBytesReader before reaching here.
func (c *Client) Upload(ctx context.Context, key string, r io.Reader) error {
	if c.s3 == nil {
		return fmt.Errorf("storage: not configured in this deployment (missing ENDPOINT/BUCKET)")
	}
	data, err := io.ReadAll(r)
	if err != nil {
		return fmt.Errorf("storage: reading body for %s: %w", key, err)
	}
	_, err = c.s3.PutObject(ctx, &s3.PutObjectInput{
		Bucket:        aws.String(c.bucket),
		Key:           aws.String(key),
		Body:          bytes.NewReader(data),
		ContentLength: aws.Int64(int64(len(data))),
	})
	if err != nil {
		return fmt.Errorf("storage: uploading %s: %w", key, err)
	}
	return nil
}

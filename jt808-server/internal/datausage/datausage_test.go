package datausage

import (
	"net"
	"testing"
)

func TestCountingConn_CountsReadAndWrite(t *testing.T) {
	server, client := net.Pipe()
	defer client.Close()
	cc := Wrap(server)
	defer cc.Close()

	done := make(chan struct{})
	go func() {
		defer close(done)
		buf := make([]byte, 5)
		n, err := cc.Read(buf)
		if err != nil {
			t.Errorf("Read() error = %v", err)
		}
		if n != 5 {
			t.Errorf("Read() n = %d, want 5", n)
		}
		if _, err := cc.Write([]byte("resp")); err != nil {
			t.Errorf("Write() error = %v", err)
		}
	}()

	if _, err := client.Write([]byte("hello")); err != nil {
		t.Fatal(err)
	}
	respBuf := make([]byte, 4)
	if _, err := client.Read(respBuf); err != nil {
		t.Fatal(err)
	}
	<-done

	rx, tx := cc.TakeDelta()
	if rx != 5 {
		t.Errorf("rx = %d, want 5", rx)
	}
	if tx != 4 {
		t.Errorf("tx = %d, want 4", tx)
	}
}

func TestCountingConn_TakeDeltaResets(t *testing.T) {
	server, client := net.Pipe()
	defer client.Close()
	cc := Wrap(server)
	defer cc.Close()

	readDone := make(chan struct{})
	go func() {
		defer close(readDone)
		buf := make([]byte, 3)
		_, _ = cc.Read(buf)
	}()
	_, _ = client.Write([]byte("abc"))
	<-readDone // wait until cc.Read() has returned and added to the counter

	rx1, _ := cc.TakeDelta()
	if rx1 != 3 {
		t.Fatalf("first TakeDelta() rx = %d, want 3", rx1)
	}
	rx2, tx2 := cc.TakeDelta()
	if rx2 != 0 || tx2 != 0 {
		t.Errorf("second TakeDelta() = (%d, %d), want (0, 0) -- it must reset, not accumulate forever", rx2, tx2)
	}
}

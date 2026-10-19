package jt1078bridge

import (
	"testing"

	"github.com/cuteLittleDevil/go-jt808/protocol/jt1078"
)

func TestReassembler_Atomic(t *testing.T) {
	var r Reassembler
	p := &jt1078.Packet{
		SubcontractType: jt1078.SubcontractTypeAtomic,
		DataType:        jt1078.DataTypeI,
		Timestamp:       1234,
		Body:            []byte{1, 2, 3},
	}
	frame, ok := r.Feed(p)
	if !ok {
		t.Fatal("an atomic packet must complete a frame immediately")
	}
	if string(frame.Data) != string([]byte{1, 2, 3}) {
		t.Errorf("frame.Data = %v, want [1 2 3]", frame.Data)
	}
	if frame.TimestampMs != 1234 {
		t.Errorf("frame.TimestampMs = %d, want 1234", frame.TimestampMs)
	}
}

func TestReassembler_FirstMiddleLast(t *testing.T) {
	var r Reassembler

	if _, ok := r.Feed(&jt1078.Packet{SubcontractType: jt1078.SubcontractTypeFirst, Timestamp: 500, Body: []byte{1, 2}}); ok {
		t.Fatal("the 'first' fragment must not complete the frame")
	}
	if _, ok := r.Feed(&jt1078.Packet{SubcontractType: jt1078.SubcontractTypeMiddle, Body: []byte{3, 4}}); ok {
		t.Fatal("the 'middle' fragment must not complete the frame")
	}
	frame, ok := r.Feed(&jt1078.Packet{SubcontractType: jt1078.SubcontractTypeLast, Body: []byte{5, 6}})
	if !ok {
		t.Fatal("the 'last' fragment must complete the frame")
	}
	want := []byte{1, 2, 3, 4, 5, 6}
	if string(frame.Data) != string(want) {
		t.Errorf("frame.Data = %v, want %v", frame.Data, want)
	}
	// The reassembled frame's timestamp must be the FIRST fragment's (all
	// NALUs of a frame share one RTP timestamp; the 'last' fragment carries
	// none relevant to the whole frame).
	if frame.TimestampMs != 500 {
		t.Errorf("frame.TimestampMs = %d, want 500 (from the 'first' fragment)", frame.TimestampMs)
	}
}

func TestReassembler_MiddleWithoutFirst_Ignored(t *testing.T) {
	var r Reassembler
	// A middle fragment without a preceding "first" (lost packet, or a
	// connection that started mid-frame) must not produce a corrupt frame nor
	// contaminate the next legitimate frame.
	if _, ok := r.Feed(&jt1078.Packet{SubcontractType: jt1078.SubcontractTypeMiddle, Body: []byte{0xFF}}); ok {
		t.Fatal("an orphan middle fragment must not complete anything")
	}
	frame, ok := r.Feed(&jt1078.Packet{SubcontractType: jt1078.SubcontractTypeAtomic, Body: []byte{1, 2}})
	if !ok || string(frame.Data) != string([]byte{1, 2}) {
		t.Fatalf("the next atomic frame must be processed cleanly, got %v ok=%v", frame.Data, ok)
	}
}

func TestReassembler_LastWithoutFirst_Ignored(t *testing.T) {
	var r Reassembler
	if _, ok := r.Feed(&jt1078.Packet{SubcontractType: jt1078.SubcontractTypeLast, Body: []byte{0xFF}}); ok {
		t.Fatal("an orphan 'last' fragment must not complete anything")
	}
}

func TestReassembler_ExceedsMaxBytes_Aborted(t *testing.T) {
	var r Reassembler
	r.Feed(&jt1078.Packet{SubcontractType: jt1078.SubcontractTypeFirst, Body: make([]byte, 100)})
	big := make([]byte, maxReassemblyBytes)
	r.Feed(&jt1078.Packet{SubcontractType: jt1078.SubcontractTypeMiddle, Body: big})
	// The frame was aborted internally; a later 'last' must not complete
	// anything because inProgress is already off.
	if _, ok := r.Feed(&jt1078.Packet{SubcontractType: jt1078.SubcontractTypeLast, Body: []byte{1}}); ok {
		t.Fatal("a frame that exceeded the size limit must not complete")
	}
}

func TestReassembler_ReusableAfterCompletion(t *testing.T) {
	var r Reassembler
	r.Feed(&jt1078.Packet{SubcontractType: jt1078.SubcontractTypeFirst, Body: []byte{1}})
	r.Feed(&jt1078.Packet{SubcontractType: jt1078.SubcontractTypeLast, Body: []byte{2}})

	// A second complete frame must reassemble cleanly, with no bytes from
	// the previous frame mixed in.
	r.Feed(&jt1078.Packet{SubcontractType: jt1078.SubcontractTypeFirst, Body: []byte{9}})
	frame, ok := r.Feed(&jt1078.Packet{SubcontractType: jt1078.SubcontractTypeLast, Body: []byte{10}})
	if !ok {
		t.Fatal("the second frame must complete")
	}
	want := []byte{9, 10}
	if string(frame.Data) != string(want) {
		t.Errorf("frame.Data = %v, want %v (no leftovers from the previous frame)", frame.Data, want)
	}
}

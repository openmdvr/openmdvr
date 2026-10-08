package main

import (
	"context"
	"log"
	"net"
	"net/http"
	"os/signal"
	"syscall"
	"time"

	"golang.org/x/sync/errgroup"

	"github.com/openmdvr/openmdvr/jt808-server/internal/alarmclip"
	"github.com/openmdvr/openmdvr/jt808-server/internal/commands"
	"github.com/openmdvr/openmdvr/jt808-server/internal/config"
	"github.com/openmdvr/openmdvr/jt808-server/internal/db"
	"github.com/openmdvr/openmdvr/jt808-server/internal/gt06server"
	"github.com/openmdvr/openmdvr/jt808-server/internal/gt06videobridge"
	"github.com/openmdvr/openmdvr/jt808-server/internal/jt1078bridge"
	"github.com/openmdvr/openmdvr/jt808-server/internal/jt808server"
	"github.com/openmdvr/openmdvr/jt808-server/internal/session"
	"github.com/openmdvr/openmdvr/jt808-server/internal/storage"
	"github.com/openmdvr/openmdvr/jt808-server/internal/videobridge"
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	cfg, err := config.Load()
	if err != nil {
		log.Fatalf("config: %v", err)
	}

	pool, err := db.NewPool(ctx, cfg)
	if err != nil {
		log.Fatalf("db: %v", err)
	}
	defer pool.Close()

	// The session registry is shared by the JT808 server (signaling: it
	// registers each connection) and the JT1078 bridge (which must find a
	// device's open connection to send it a 0x9101 when someone requests its
	// video).
	registry := session.NewRegistry()

	jt808Srv := jt808server.New(pool, registry, cfg.ListenAddr, cfg.MaxConnections)

	// gt06Registry is GT06's own registry (not the JT808 one above), see the
	// gt06server package doc. gt06Dispatcher consumes it to send active
	// commands (engine cut/resume, RTMP video start/stop, configuration);
	// gt06Srv only writes to it (on login), never reads it. Created BEFORE
	// gt06videobridge, which needs it to request video from a gt06_video
	// device (Jimi IoT JC261/JC400) over the SAME authenticated GT06
	// connection -- see internal/gt06videobridge/bridge.go.
	gt06Registry := gt06server.NewRegistry()
	gt06Srv := gt06server.New(pool, cfg.GT06ListenAddr, cfg.GT06MaxConnections, gt06Registry)
	gt06Dispatcher := gt06server.NewDispatcher(gt06Registry)

	// active/zlm/tickets are the SHARED live video infrastructure (see
	// internal/videobridge), built once here and passed by reference to EVERY
	// protocol, so a single active-stream map and a single ticket store serve
	// them all. Adding a video protocol means building it with these same
	// objects and registering it in videobridge.NewDispatcher below -- never
	// instantiating its own copy of this infrastructure.
	activeStreams := videobridge.NewActiveStreams()
	zlmClient := videobridge.NewZLMClient(cfg.ZLMBaseURL, cfg.ZLMSecret)
	ticketStore := videobridge.NewTicketStore()

	bridge := jt1078bridge.New(jt1078bridge.Config{
		ListenAddr:           cfg.JT1078ListenAddr,
		ListenPort:           cfg.JT1078ListenPort,
		PublicIP:             cfg.PublicIP,
		ZLMBaseURL:           cfg.ZLMBaseURL,
		ZLMSecret:            cfg.ZLMSecret,
		ZLMPlayURLFormat:     cfg.ZLMPlayURLFormat,
		ZLMWebrtcPlayBaseURL: cfg.ZLMWebrtcPlayBaseURL,
	}, registry, pool, activeStreams, zlmClient)

	gt06Video := gt06videobridge.New(gt06videobridge.Config{
		GT06VideoApp:         cfg.GT06VideoApp,
		ZLMGT06PlayBaseURL:   cfg.ZLMGT06PlayBaseURL,
		ZLMWebrtcPlayBaseURL: cfg.ZLMWebrtcPlayBaseURL,
	}, activeStreams, zlmClient, pool, gt06Dispatcher)

	videoDispatcher := videobridge.NewDispatcher(pool, ticketStore, zlmClient, bridge, gt06Video)

	// CENTRAL live view time meter (see videobridge.LiveMeter): one object
	// shared by all protocols, like activeStreams/tickets. Closed at the end
	// (writes the pending segment).
	liveMeter := videobridge.NewLiveMeter(pool)
	go liveMeter.Run()
	defer liveMeter.Close(10 * time.Second)
	bridge.SetLiveMeter(liveMeter)
	gt06Video.SetLiveMeter(liveMeter)
	videoDispatcher.SetLiveMeter(liveMeter)

	// storageClient uploads alarm video clips to R2 -- empty-safe if the
	// deployment has no bucket configured (see internal/storage.New); it
	// breaks nothing else in the process.
	storageClient := storage.New(storage.Config{
		Endpoint:  cfg.R2Endpoint,
		AccessKey: cfg.R2AccessKey,
		SecretKey: cfg.R2SecretKey,
		Bucket:    cfg.R2Bucket,
	})
	alarmClips := alarmclip.New(pool, gt06Dispatcher, storageClient)

	// Automatic clip request when a camera event (0x95) arrives -- see
	// gt06server.ClipRequester/alarmclip.Bridge.RequestClipForAlarm. A setter
	// (not a gt06server.New parameter) because alarmClips needs
	// gt06Dispatcher (built from gt06Registry, not gt06Srv); there is no real
	// circular dependency, only a construction order a constructor parameter
	// would force to change needlessly.
	gt06Srv.SetClipRequester(alarmClips)
	// Native JC261/JC400 photo ("Picture,out#"/"Picture,in#"): preview
	// snapshots prefer it over opening the video stream.
	gt06Video.SetPhotoCapturer(alarmClips)
	// Photos the camera uploads late (the JC261 front camera sometimes takes
	// 30s+) go to the preview cache instead of being discarded.
	alarmClips.SetPhotoSink(func(imei string, channel uint8, data []byte) {
		videoDispatcher.CacheNativeSnapshot(gt06Video.Name(), imei, channel, data)
	})

	// httpMux combines each video protocol's own routes (POST /api/v1/9101,
	// POST /api/v1/gt06-video) with the Dispatcher's shared endpoints
	// (tickets + ZLMediaKit hooks), the protocol-AGNOSTIC remote command
	// channel (commands.Handler -- only "gt06" has a Sender today; adding a
	// protocol with commands is one more map entry), and the internal alarm
	// clip request trigger (alarmClips.RegisterInternalRoutes). This mux is
	// internal control ONLY (docker network), never exposed beyond that.
	httpMux := http.NewServeMux()
	bridge.RegisterRoutes(httpMux)
	gt06Video.RegisterRoutes(httpMux)
	videoDispatcher.RegisterRoutes(httpMux)
	alarmClips.RegisterInternalRoutes(httpMux)
	httpMux.HandleFunc("POST /api/v1/commands", commands.Handler(map[string]commands.Sender{
		"gt06": gt06Dispatcher,
	}))
	// Configuration command channel (SERVER/APN/TIMEZONE/UPLOAD/etc.) -- raw
	// text built by the API, never decided here.
	httpMux.HandleFunc("POST /api/v1/gt06-raw-command", commands.RawHandler(map[string]commands.RawSender{
		"gt06": gt06Dispatcher,
	}))

	httpSrv := &http.Server{
		Addr:    cfg.HTTPListenAddr,
		Handler: httpMux,
	}

	// alarmClipMux is PUBLIC -- unlike httpMux above, the device uploads clip
	// files directly to this port from its own cellular network (configured
	// on the device with the UPLOAD/FILELIST commands), like the
	// JT808/JT1078/GT06 ports. It has its own port so untrusted device
	// traffic never mixes with the internal control channel.
	alarmClipMux := http.NewServeMux()
	alarmClips.RegisterRoutes(alarmClipMux)
	// Explicit timeouts + newLimitListener (see limitlistener.go) -- a
	// security review found this was the only public server in the process
	// with neither. ReadTimeout is generous (real cellular links can be slow
	// uploading up to 100MB); WriteTimeout is short because this server's
	// responses are tiny (a couple of JSON fields or an empty 20x).
	alarmClipSrv := &http.Server{
		Addr:              cfg.AlarmClipListenAddr,
		Handler:           alarmClipMux,
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       120 * time.Second,
		WriteTimeout:      15 * time.Second,
		IdleTimeout:       60 * time.Second,
		MaxHeaderBytes:    1 << 20,
	}

	g, gctx := errgroup.WithContext(ctx)
	g.Go(func() error { return jt808Srv.ListenAndServe(gctx) })
	g.Go(func() error { return bridge.ListenAndServe(gctx, cfg.JT1078MaxConnections) })
	g.Go(func() error { return gt06Srv.ListenAndServe(gctx) })
	g.Go(func() error {
		bridge.RunPendingSweeper(gctx)
		return nil
	})
	g.Go(func() error {
		log.Printf("internal api: listening on %s (jt1078/gt06 video + remote commands)", cfg.HTTPListenAddr)
		errCh := make(chan error, 1)
		go func() { errCh <- httpSrv.ListenAndServe() }()
		select {
		case <-gctx.Done():
			return httpSrv.Close()
		case err := <-errCh:
			return err
		}
	})
	g.Go(func() error {
		ln, err := net.Listen("tcp", cfg.AlarmClipListenAddr)
		if err != nil {
			return err
		}
		ln = newLimitListener(ln, cfg.AlarmClipMaxConnections)
		log.Printf("alarm-clip: listening on %s (alarm video clip upload/filelist, max %d concurrent connections)", cfg.AlarmClipListenAddr, cfg.AlarmClipMaxConnections)
		errCh := make(chan error, 1)
		go func() { errCh <- alarmClipSrv.Serve(ln) }()
		select {
		case <-gctx.Done():
			return alarmClipSrv.Close()
		case err := <-errCh:
			return err
		}
	})

	if err := g.Wait(); err != nil && ctx.Err() == nil {
		liveMeter.Close(10 * time.Second) // log.Fatalf does not run deferred calls
		log.Fatalf("server: %v", err)
	}
}

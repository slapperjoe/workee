(function () {
  if (window.__workeeHooked) return;
  window.__workeeHooked = true;

  var tracks = [];
  var deviceMap = {};
  var screenCam = false;
  var camOpts = { width: 1280, height: 720, fps: 30, smooth: 0 };

  function report() {
    var audio = false;
    var video = false;
    var audioDevice = null;
    var videoDevice = null;
    for (var i = 0; i < tracks.length; i++) {
      var t = tracks[i];
      if (t.readyState !== 'live') continue;
      if (t.kind === 'audio' && t.enabled) {
        audio = true;
        if (!audioDevice && t.getSettings) {
          var s = t.getSettings();
          audioDevice = (s && deviceMap[s.deviceId]) || null;
        }
      } else if (t.kind === 'video' && t.enabled) {
        video = true;
        if (!videoDevice && t.getSettings) {
          var s2 = t.getSettings();
          videoDevice = (s2 && deviceMap[s2.deviceId]) || null;
        }
      }
    }
    try {
      window.__workee.report({ audio: audio, video: video, audioDevice: audioDevice, videoDevice: videoDevice });
    } catch (e) {}
  }

  function updateDevices() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
    navigator.mediaDevices.enumerateDevices().then(function (devs) {
      deviceMap = {};
      var out = { audioinput: null, audiooutput: null, videoinput: null };
      for (var i = 0; i < devs.length; i++) {
        var d = devs[i];
        if (d.label) deviceMap[d.deviceId] = d.label;
        if (d.kind === 'audioinput' && !out.audioinput && d.label) out.audioinput = d.label;
        if (d.kind === 'audiooutput' && !out.audiooutput && d.label) out.audiooutput = d.label;
        if (d.kind === 'videoinput' && !out.videoinput && d.label) out.videoinput = d.label;
      }
      try { window.__workee.devices(out); } catch (e) {}
    }).catch(function () {});
  }

  function track(stream) {
    if (!stream) return;
    stream.getTracks().forEach(function (t) {
      if (t.kind === 'audio' || t.kind === 'video') {
        tracks.push(t);
        t.addEventListener('ended', report);
        t.addEventListener('mute', report);
        t.addEventListener('unmute', report);
      }
    });
  }

  window.__workeeSetMic = function (enabled) {
    for (var i = 0; i < tracks.length; i++) {
      if (tracks[i].kind === 'audio') tracks[i].enabled = enabled;
    }
    report();
  };

  window.__workeeSetScreenCam = function (enabled, opts) {
    screenCam = !!enabled;
    if (opts && opts.width && opts.height && opts.fps) {
      camOpts = {
        width: opts.width,
        height: opts.height,
        fps: opts.fps,
        smooth: opts.smooth || 0,
      };
    }
  };

  function smoothStream(screenStream) {
    var smooth = camOpts.smooth || 0;
    if (!smooth) return Promise.resolve(screenStream);

    var video = document.createElement('video');
    video.autoplay = true;
    video.muted = true;
    video.playsInline = true;
    video.srcObject = screenStream;
    video.play().catch(function () {});

    var canvas = document.createElement('canvas');
    canvas.width = camOpts.width;
    canvas.height = camOpts.height;
    var ctx = canvas.getContext('2d');
    var blurPx = smooth === 2 ? 2.5 : 1.2;

    function draw() {
      var vw = video.videoWidth, vh = video.videoHeight;
      if (!vw || !vh) return;
      var scale = Math.min(canvas.width / vw, canvas.height / vh);
      var dw = vw * scale, dh = vh * scale;
      var dx = (canvas.width - dw) / 2, dy = (canvas.height - dh) / 2;
      ctx.fillStyle = '#000';
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.filter = 'blur(' + blurPx + 'px)';
      ctx.drawImage(video, dx, dy, dw, dh);
      ctx.filter = 'none';
    }

    var out = canvas.captureStream(camOpts.fps);
    var vt = out.getVideoTracks()[0];
    if (vt && 'contentHint' in vt) { try { vt.contentHint = 'detail'; } catch (e) {} }
    var timer = setInterval(draw, Math.round(1000 / camOpts.fps));
    vt.addEventListener('ended', function () { clearInterval(timer); video.srcObject = null; });
    return Promise.resolve(out);
  }

  var md = navigator.mediaDevices;
  if (md && md.getUserMedia) {
    var orig = md.getUserMedia.bind(md);

    md.getUserMedia = function (constraints) {
      var wantVideo = !!(constraints && constraints.video);
      var wantAudio = !!(constraints && constraints.audio);

      if (screenCam && wantVideo && md.getDisplayMedia) {
        return md.getDisplayMedia({
          video: { frameRate: { max: camOpts.fps }, width: { max: camOpts.width }, height: { max: camOpts.height } },
          audio: false,
        }).then(function (screenStream) {
          return smoothStream(screenStream).then(function (videoStream) {
            var out = new MediaStream();
            videoStream.getVideoTracks().forEach(function (t) {
              try { if ('contentHint' in t) t.contentHint = 'detail'; } catch (e) {}
              out.addTrack(t);
            });
            var audioDone = Promise.resolve();
            if (wantAudio) {
              audioDone = orig({ audio: constraints.audio, video: false }).then(function (micStream) {
                micStream.getAudioTracks().forEach(function (t) { out.addTrack(t); });
              }).catch(function () {});
            }
            return audioDone.then(function () {
              track(out);
              report();
              updateDevices();
              return out;
            });
          });
        }).catch(function () {
          return orig(constraints).then(function (stream) {
            track(stream);
            report();
            updateDevices();
            return stream;
          });
        });
      }

      return orig(constraints).then(function (stream) {
        track(stream);
        report();
        updateDevices();
        return stream;
      });
    };
  }

  if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
    navigator.mediaDevices.addEventListener('devicechange', updateDevices);
  }

  updateDevices();
  report();
})();

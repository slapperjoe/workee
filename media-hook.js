(function () {
  if (window.__workeeHooked) return;
  window.__workeeHooked = true;

  var tracks = [];
  var deviceMap = {};
  var obsDeviceId = null;
  var screenCam = false;
  var micEnabled = true;
  var camOpts = { width: 1280, height: 720, fps: 30, smooth: 0 };

  function report() {
    var audio = false;
    var video = false;
    var audioDevice = null;
    var videoDevice = null;
    var videoSource = null;
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
          videoSource = (s2 && s2.deviceId === obsDeviceId) ? 'obs' : 'camera';
        }
      }
    }
    try {
      window.__workee.report({ audio: audio, video: video, audioDevice: audioDevice, videoDevice: videoDevice, videoSource: videoSource });
    } catch (e) {}
  }

  function updateDevices() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
    navigator.mediaDevices.enumerateDevices().then(function (devs) {
      deviceMap = {};
      obsDeviceId = null;
      var out = { audioinput: null, audiooutput: null, videoinput: null };
      for (var i = 0; i < devs.length; i++) {
        var d = devs[i];
        if (d.label) deviceMap[d.deviceId] = d.label;
        if (d.kind === 'videoinput' && d.label && /obs|virtual camera|v4l2loopback|hardware isp camera/i.test(d.label) && !obsDeviceId) {
          obsDeviceId = d.deviceId;
        }
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
        if (t.kind === 'audio') t.enabled = micEnabled;
        tracks.push(t);
        t.addEventListener('ended', report);
        t.addEventListener('mute', report);
        t.addEventListener('unmute', report);
      }
    });
  }

  window.__workeeSetMic = function (enabled) {
    micEnabled = !!enabled;
    for (var i = 0; i < tracks.length; i++) {
      if (tracks[i].kind === 'audio') tracks[i].enabled = micEnabled;
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


  var md = navigator.mediaDevices;
  if (md && md.getUserMedia) {
    var orig = md.getUserMedia.bind(md);

    md.getUserMedia = function (constraints) {
      var wantVideo = !!(constraints && constraints.video);
      var wantAudio = !!(constraints && constraints.audio);

      if (screenCam && wantVideo) {
        if (!obsDeviceId) {
          return Promise.reject(new Error('AVD Electron: screen-cam requested but OBS virtual camera is not available'));
        }
        var obsVideo = {};
        if (constraints.video !== true) {
          for (var key in constraints.video) obsVideo[key] = constraints.video[key];
        }
        obsVideo.deviceId = { exact: obsDeviceId };
        return orig({ audio: constraints.audio, video: obsVideo }).then(function (stream) {
          track(stream);
          report();
          updateDevices();
          return stream;
        }).catch(function () {
          return Promise.reject(new Error('AVD Electron: OBS virtual camera is not available'));
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

  var __kaMark = 0;
  window.__workeeMarkKA = function () {
    __kaMark = Date.now() + 500;
  };
  ['keydown', 'mousedown', 'wheel', 'contextmenu'].forEach(function (type) {
    document.addEventListener(type, function (e) {
      if (Date.now() < __kaMark) return;
      if (type === 'keydown' && (e.key === 'Control' || e.code === 'ControlLeft')) return;
      (window.__workee && window.__workee.keepAliveInput) ? window.__workee.keepAliveInput() : null;
    }, { passive: true, capture: true });
  });

  var _fsDoc = document.createElement('div');
  var _fsEl = _fsDoc;
  var __origDocFull = Document.prototype.requestFullscreen;
  var __origElFull = Element.prototype.requestFullscreen;
  var __blockFullscreen = false;
  window.__workeeSetBlockFullscreen = function (enabled) {
    __blockFullscreen = !!enabled;
    if (__blockFullscreen) {
      __origDocFull = __origDocFull || Document.prototype.requestFullscreen;
      __origElFull = __origElFull || Element.prototype.requestFullscreen;
      Document.prototype.requestFullscreen = function () {
        return Promise.reject(new Error('Fullscreen disabled by AVD Electron'));
      };
      Element.prototype.requestFullscreen = function () {
        return Promise.reject(new Error('Fullscreen disabled by AVD Electron'));
      };
    } else {
      Document.prototype.requestFullscreen = __origDocFull;
      Element.prototype.requestFullscreen = __origElFull;
    }
  };

  updateDevices();
  report();
})();

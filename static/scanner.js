(() => {
  const root = document.querySelector('[data-qr-scanner]');
  if (!root) return;

  const video = root.querySelector('video');
  const startBtn = root.querySelector('[data-start-camera]');
  const stopBtn = root.querySelector('[data-stop-camera]');
  const status = root.querySelector('[data-scan-status]');
  const permissionStrip = root.querySelector('[data-camera-permission]');
  const canvas = document.createElement('canvas');
  const ctx = canvas.getContext('2d', { willReadFrequently: true });

  let stream = null;
  let detector = null;
  let scanning = false;
  let starting = false;

  const setStatus = (text) => {
    if (status) status.textContent = text;
  };

  const setPermission = (text, state) => {
    if (!permissionStrip) return;
    const span = permissionStrip.querySelector('span');
    if (span) span.textContent = text;
    permissionStrip.dataset.permissionState = state || '';
  };

  const setStartVisible = (visible, label) => {
    if (!startBtn) return;
    startBtn.hidden = !visible;
    startBtn.disabled = false;
    if (label) startBtn.textContent = label;
  };

  const stop = () => {
    scanning = false;
    starting = false;
    if (stream) {
      stream.getTracks().forEach((track) => track.stop());
      stream = null;
    }
    if (video) {
      video.pause();
      video.srcObject = null;
    }
    if (stopBtn) stopBtn.disabled = true;
  };

  const normalizeToken = (value) => {
    try {
      const parsed = new URL(value);
      return parsed.searchParams.get('token') || value;
    } catch (_) {
      return value;
    }
  };

  const confirmToken = (raw) => {
    const token = normalizeToken(raw || '');
    if (!token) return false;
    setStatus('QR rilevato.');
    stop();
    window.location.href = `/presence?token=${encodeURIComponent(token)}`;
    return true;
  };

  const decodeWithCanvas = () => {
    if (!window.jsQR || !ctx || !video || !video.videoWidth || !video.videoHeight) return false;
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
    const imageData = ctx.getImageData(0, 0, canvas.width, canvas.height);
    const code = window.jsQR(imageData.data, imageData.width, imageData.height, { inversionAttempts: 'attemptBoth' });
    return Boolean(code && confirmToken(code.data));
  };

  const constraintsList = () => ([
    { video: { facingMode: { exact: 'environment' }, width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false },
    { video: { facingMode: { ideal: 'environment' }, width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false },
    { video: { width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false },
    { video: true, audio: false },
  ]);

  const getCameraStream = async () => {
    let lastError = null;
    for (const constraints of constraintsList()) {
      try {
        return await navigator.mediaDevices.getUserMedia(constraints);
      } catch (error) {
        lastError = error;
        if (error && (error.name === 'NotAllowedError' || error.name === 'SecurityError')) throw error;
      }
    }
    throw lastError || new Error('Camera unavailable');
  };

  const setupDetector = async () => {
    detector = null;
    if (!('BarcodeDetector' in window)) return;
    try {
      const formats = await window.BarcodeDetector.getSupportedFormats();
      if (formats.includes('qr_code')) detector = new window.BarcodeDetector({ formats: ['qr_code'] });
    } catch (_) {}
  };

  const waitForVideo = () => new Promise((resolve) => {
    if (!video || video.readyState >= 2) {
      resolve();
      return;
    }
    const done = () => resolve();
    video.addEventListener('loadedmetadata', done, { once: true });
    window.setTimeout(done, 1200);
  });

  const scanLoop = async () => {
    if (!scanning || !video || video.readyState < 2) {
      if (scanning) requestAnimationFrame(scanLoop);
      return;
    }
    if (detector) {
      try {
        const codes = await detector.detect(video);
        if (codes && codes.length && confirmToken(codes[0].rawValue || '')) return;
      } catch (_) {}
    }
    if (decodeWithCanvas()) return;
    if (scanning) requestAnimationFrame(scanLoop);
  };

  const redirectZeroHost = () => {
    if (location.hostname !== '0.0.0.0') return false;
    const port = location.port ? `:${location.port}` : '';
    window.location.replace(`http://127.0.0.1${port}${location.pathname}${location.search}${location.hash}`);
    return true;
  };

  const start = async () => {
    if (starting || scanning) return;
    if (redirectZeroHost()) return;
    if (!window.isSecureContext) {
      setPermission('HTTPS richiesto.', 'denied');
      setStatus('HTTPS richiesto.');
      setStartVisible(true, 'Riprova');
      return;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      setPermission('Fotocamera non disponibile.', 'denied');
      setStatus('Fotocamera non disponibile.');
      setStartVisible(false);
      return;
    }

    starting = true;
    setStartVisible(false);
    setStatus('Autorizzazione richiesta.');
    setPermission('Consenti l\'accesso alla fotocamera.', 'prompt');

    try {
      stream = await getCameraStream();
      await setupDetector();
      if (video) {
        video.setAttribute('playsinline', '');
        video.setAttribute('autoplay', '');
        video.muted = true;
        video.srcObject = stream;
        await waitForVideo();
        await video.play();
      }
      scanning = true;
      starting = false;
      setPermission('Fotocamera attiva.', 'granted');
      if (stopBtn) stopBtn.disabled = false;
      setStatus('Scanner attivo.');
      requestAnimationFrame(scanLoop);
    } catch (error) {
      stop();
      if (error && (error.name === 'NotAllowedError' || error.name === 'SecurityError')) {
        setPermission('Fotocamera bloccata.', 'denied');
        setStatus('Fotocamera bloccata.');
      } else {
        setPermission('Fotocamera non disponibile.', 'denied');
        setStatus('Fotocamera non disponibile.');
      }
      setStartVisible(true, 'Consenti fotocamera');
    }
  };

  const boot = () => {
    setPermission('Consenti l\'accesso alla fotocamera.', 'prompt');
    setStatus('Autorizzazione richiesta.');
    setStartVisible(false);
    start();
  };

  boot();
  if (startBtn) startBtn.addEventListener('click', start);
  if (stopBtn) stopBtn.addEventListener('click', () => {
    stop();
    setStatus('Scanner sospeso.');
    setStartVisible(true, 'Consenti fotocamera');
  });
  window.addEventListener('pagehide', stop);
})();

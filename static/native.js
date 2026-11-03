/* ClubLifter — native bridge.
   Loaded only in the Capacitor (native) build — index.html requests it after its
   own script, so the functions below replace the web versions. Upgrades the web
   app's behavior: real barcode scanning, native geolocation, and background
   "welcome to <state>" local notifications.
   In a plain browser this file is a no-op, so the same index.html works both places. */
(function () {
  const Cap = window.Capacitor;
  if (!Cap || !Cap.isNativePlatform || !Cap.isNativePlatform()) {
    return; // running in a normal browser — keep the web behavior
  }
  const Plugins = Cap.Plugins || {};
  const Barcode = Plugins.BarcodeScanner;          // @capacitor-mlkit/barcode-scanning
  const Geolocation = Plugins.Geolocation;         // @capacitor/geolocation
  const Notifications = Plugins.LocalNotifications; // @capacitor/local-notifications
  const StatusBar = Plugins.StatusBar;
  const SplashScreen = Plugins.SplashScreen;

  // ── Status bar + splash ──────────────────────────────────────────────
  try { StatusBar && StatusBar.setStyle({ style: 'DARK' }); } catch (e) {}
  document.addEventListener('DOMContentLoaded', () => {
    setTimeout(() => { try { SplashScreen && SplashScreen.hide(); } catch (e) {} }, 400);
  });

  // Session token, 21+ answer, name and phone are persisted by index.html itself
  // (storeGet/storeSet → Preferences on native). Don't duplicate that here.

  // ── Real QR scanning via ML Kit ──────────────────────────────────────
  // Replace the web camera loop with the native scanner. The scan goes to
  // handleScanned() (guest redeem, or a staff code check in Admin mode).
  // Android's ready-made scanner is a Google Play services module: until it's
  // installed (we start the install here), the web camera scanner is used.
  const webOpenScanner = window.openScanner;
  async function nativeScannerReady() {
    if (!Barcode) return false;
    try {
      const s = await Barcode.isSupported();
      if (!s || !s.supported) return false;
      if (Cap.getPlatform && Cap.getPlatform() === 'android') {
        const m = await Barcode.isGoogleBarcodeScannerModuleAvailable();
        if (!m || !m.available) {
          Barcode.installGoogleBarcodeScannerModule().catch(() => {});
          return false;
        }
      }
      return true;
    } catch (e) {
      return false;
    }
  }
  function backFromScan() {
    try { scanCancelled(); } catch (e) { try { showScreen('home'); setNav('navExplore'); } catch (e2) {} }
  }
  window.openScanner = async function () {
    if (!(await nativeScannerReady())) return webOpenScanner();
    try { showScreen('scan'); } catch (e) {}
    try {
      // Ensure permission
      const perm = await Barcode.requestPermissions();
      if (perm.camera !== 'granted' && perm.camera !== 'limited') {
        alert('Camera permission is needed to scan QR codes.');
        backFromScan();
        return;
      }
      // Make the webview transparent so the camera shows through
      document.body.classList.add('scanner-active');
      const { barcodes } = await Barcode.scan();
      document.body.classList.remove('scanner-active');
      const raw = barcodes && barcodes.length ? (barcodes[0].rawValue || barcodes[0].displayValue || '') : '';
      if (raw && typeof handleScanned === 'function') handleScanned(raw.trim());
      else backFromScan();
    } catch (e) {
      // Closing the scanner lands here too
      document.body.classList.remove('scanner-active');
      backFromScan();
    }
  };
  // The manual "Enter code" path still works unchanged.

  // ── Native geolocation ───────────────────────────────────────────────
  window.getLocation = function () {
    return new Promise(async (resolve) => {
      try {
        await Geolocation.requestPermissions();
        const pos = await Geolocation.getCurrentPosition({ enableHighAccuracy: true, timeout: 8000 });
        resolve({ lat: pos.coords.latitude, lng: pos.coords.longitude });
      } catch (e) {
        // No fix: Vegas, flagged approx so it's never used as "where you are"
        resolve({ lat: 36.1147, lng: -115.1728, approx: true });
      }
    });
  };
  // Live position for in-app directions (same permission as getLocation).
  window.watchLocation = function (onPos) {
    let id = null, stopped = false;
    Geolocation.watchPosition({ enableHighAccuracy: true, timeout: 15000, maximumAge: 5000 }, (pos) => {
      if (pos && pos.coords) onPos({ lat: pos.coords.latitude, lng: pos.coords.longitude });
    }).then((cid) => {
      if (stopped) Geolocation.clearWatch({ id: cid });
      else id = cid;
    }).catch(() => {});
    return { clear() { stopped = true; if (id) Geolocation.clearWatch({ id }); } };
  };

  // ── "Welcome to <state>" phone notification ──────────────────────────
  // With the app open, index.html welcomes them in-app (noteArrival). This is
  // the phone notification for when it isn't: its own Android channel (high
  // importance, so it pops up), the ClubLifter icon (ic_stat_icon, added by the
  // Codemagic build), and copy from the server (/api/app/detect-state).
  // Tapping it opens Explore with the in-app welcome.
  const CHANNEL = 'arrival';
  let channelReady = null;
  function ensureChannel() {
    if (!channelReady) {
      channelReady = (Cap.getPlatform && Cap.getPlatform() === 'android'
        ? Notifications.createChannel({ id: CHANNEL, name: 'Arrival offers', importance: 4, visibility: 1,
            description: 'Free-drink offers when you arrive somewhere with ClubLifter clubs' })
        : Promise.resolve()).catch(() => {});
    }
    return channelReady;
  }
  async function notificationsAllowed() {
    try {
      let p = await Notifications.checkPermissions();
      if (p.display === 'prompt' || p.display === 'prompt-with-rationale') p = await Notifications.requestPermissions();
      return p.display === 'granted';
    } catch (e) { return false; }
  }
  // info: {state, title, body, message}; delaySec > 0 only for the staff test.
  window.sendArrivalAlert = async function (info, delaySec) {
    if (!Notifications || !(await notificationsAllowed())) return false;
    await ensureChannel();
    const n = {
      id: Math.floor(Date.now() % 2000000000),
      title: info.title || `Welcome to ${info.state}! 🎉`,
      body: info.body || 'Visit one of our clubs and get a free drink 🍸',
      channelId: CHANNEL, smallIcon: 'ic_stat_icon', iconColor: '#8a5cf6',
      extra: { kind: 'arrival', state: info.state || '', message: info.message || '' },
    };
    if (delaySec > 0) n.schedule = { at: new Date(Date.now() + delaySec * 1000), allowWhileIdle: true };
    try { await Notifications.schedule({ notifications: [n] }); return true; } catch (e) { return false; }
  };
  try {
    Notifications && Notifications.addListener('localNotificationActionPerformed', (a) => {
      const x = (a && a.notification && a.notification.extra) || {};
      if (x.kind !== 'arrival') return;
      try {
        showScreen('home'); setNav('navExplore');
        if (x.state) showWelcome(x.state, x.message);
      } catch (e) {}
    });
  } catch (e) {}
})();

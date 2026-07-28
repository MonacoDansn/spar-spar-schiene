package at.sparsparschiene.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.os.IBinder;
import android.util.Base64;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * Pollt den Scan-Status (GET /api/scan/<id>?light=1) unabhaengig vom WebView
 * und zeigt eine Fortschritts-Benachrichtigung - auch bei Display aus.
 * Der WebView pausiert im Hintergrund, dieser Service nicht.
 */
public class ScanWatchService extends Service {

    private static final String CHANNEL = "scan_progress";
    private static final int NOTIF_ID = 1;
    private static final int POLL_MS = 10_000;
    private static final long MAX_RUNTIME_MS = 30 * 60_000L;

    private volatile Thread worker;

    @Override
    public IBinder onBind(Intent intent) { return null; }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String jobId = intent != null ? intent.getStringExtra("jobId") : null;
        String baseUrl = intent != null ? intent.getStringExtra("baseUrl") : null;
        if (jobId == null || baseUrl == null) { stopSelf(); return START_NOT_STICKY; }
        String user = intent.getStringExtra("user");
        String pass = intent.getStringExtra("pass");

        createChannel();
        startForeground(NOTIF_ID, build("Scan läuft…", 0, 0, true));

        if (worker != null) worker.interrupt();  // alter Scan -> neuer gewinnt
        worker = new Thread(() -> poll(baseUrl, jobId, user, pass));
        worker.setDaemon(true);
        worker.start();
        return START_NOT_STICKY;
    }

    private void poll(String baseUrl, String jobId, String user, String pass) {
        long startTime = System.currentTimeMillis();
        while (!Thread.currentThread().isInterrupted()
                && System.currentTimeMillis() - startTime < MAX_RUNTIME_MS) {
            try {
                HttpURLConnection c = (HttpURLConnection)
                        new URL(baseUrl + "/api/scan/" + jobId + "?light=1").openConnection();
                c.setConnectTimeout(15000);
                c.setReadTimeout(15000);
                if (user != null && pass != null) {
                    String cred = Base64.encodeToString(
                            (user + ":" + pass).getBytes("UTF-8"), Base64.NO_WRAP);
                    c.setRequestProperty("Authorization", "Basic " + cred);
                }
                int status = c.getResponseCode();
                if (status == 404) {
                    finish("⚠️ Scan verloren (Server-Neustart)");
                    return;
                }
                if (status == 200) {
                    StringBuilder sb = new StringBuilder();
                    try (BufferedReader r = new BufferedReader(
                            new InputStreamReader(c.getInputStream(), "UTF-8"))) {
                        String line;
                        while ((line = r.readLine()) != null) sb.append(line);
                    }
                    JSONObject snap = new JSONObject(sb.toString());
                    if (snap.optBoolean("finished")) {
                        finish("✅ Scan fertig: " + snap.optInt("resultCount") + " Tickets gefunden");
                        return;
                    }
                    JSONObject ph = snap.optJSONObject("phase");
                    if (ph != null) {
                        int done = ph.optInt("done");
                        int total = ph.optInt("total");
                        String txt = phaseLabel(ph.optString("name"))
                                + " · " + done + "/" + total + " Abfragen"
                                + " · " + ph.optInt("found") + " Treffer" + etaText(ph);
                        notify(build(txt, done, total, true));
                    }
                }
            } catch (Exception ignored) {
                // Netzwerkfehler (Funkloch, Server wacht auf): naechster Versuch
            }
            try { Thread.sleep(POLL_MS); } catch (InterruptedException e) { return; }
        }
        stopSelf();
    }

    private static String phaseLabel(String name) {
        switch (name) {
            case "A": return "Abfahrtsbahnhöfe";
            case "B": return "Ankunftsbahnhöfe";
            case "C": return "Kreuzverbindungen";
            case "bus": return "Bushaltestellen";
            default: return "Scan";
        }
    }

    private static String etaText(JSONObject ph) {
        if (ph.isNull("eta")) return "";
        int eta = ph.optInt("eta");
        String t = eta < 90 ? "~" + Math.max(10, eta / 10 * 10) + " s"
                            : "~" + Math.round(eta / 60.0) + " min";
        return " · noch " + (ph.optBoolean("etaMin") ? "mind. " : "") + t;
    }

    private void finish(String message) {
        notify(build(message, 0, 0, false));
        stopForeground(false);  // Benachrichtigung stehen lassen
        stopSelf();
    }

    private void notify(Notification n) {
        ((NotificationManager) getSystemService(NOTIFICATION_SERVICE)).notify(NOTIF_ID, n);
    }

    private Notification build(String text, int done, int total, boolean ongoing) {
        Intent open = new Intent(this, MainActivity.class);
        PendingIntent pi = PendingIntent.getActivity(this, 0, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder b = new Notification.Builder(this, CHANNEL)
                .setSmallIcon(R.drawable.ic_fg)
                .setContentTitle("Spar Spar Schiene")
                .setContentText(text)
                .setStyle(new Notification.BigTextStyle().bigText(text))
                .setContentIntent(pi)
                .setOnlyAlertOnce(true)
                .setOngoing(ongoing);
        if (total > 0) b.setProgress(total, done, false);
        return b.build();
    }

    private void createChannel() {
        NotificationChannel ch = new NotificationChannel(CHANNEL,
                "Scan-Fortschritt", NotificationManager.IMPORTANCE_LOW);
        ch.setDescription("Live-Fortschritt laufender Ticket-Scans");
        ((NotificationManager) getSystemService(NOTIFICATION_SERVICE))
                .createNotificationChannel(ch);
    }

    @Override
    public void onDestroy() {
        if (worker != null) worker.interrupt();
        super.onDestroy();
    }
}

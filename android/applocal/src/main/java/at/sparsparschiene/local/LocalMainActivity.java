package at.sparsparschiene.local;

import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.view.Menu;
import android.view.MenuItem;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;

/**
 * "Spar Spar Schiene Lokal" - komplett serverlos: der unveraenderte Python-
 * Server (server.py) laeuft eingebettet (Chaquopy) auf 127.0.0.1:8325,
 * der WebView zeigt die gewohnte Oberflaeche. Keine Anmeldung, kein Render.
 */
public class LocalMainActivity extends Activity {

    static final String BASE_URL = "http://127.0.0.1:8325";
    private static volatile boolean serverStarted = false;  // pro Prozess nur 1x binden

    private WebView webView;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        webView = new WebView(this);
        webView.getSettings().setJavaScriptEnabled(true);
        webView.getSettings().setDomStorageEnabled(true);
        webView.addJavascriptInterface(new SparBridge(), "SparApp");
        webView.setWebViewClient(new WebViewClient());
        setContentView(webView);

        showMessage("&#128649; Spar Spar Schiene Lokal",
                "Lokaler Server startet&#8230; (erster Start dauert einige Sekunden)");
        new Thread(this::bootAndLoad).start();
    }

    private void bootAndLoad() {
        try {
            File root = prepareFiles();
            startPythonServer(root);
            waitForPort(30_000);
            runOnUiThread(() -> webView.loadUrl(BASE_URL));
        } catch (Exception e) {
            runOnUiThread(() -> showMessage("&#9888;&#65039; Start fehlgeschlagen",
                    String.valueOf(e).replace("<", "&lt;")));
        }
    }

    /** Oberflaeche + Stationsdaten aus den Assets in den App-Speicher kopieren.
     *  Cache-Dateien (places_cache/bus_cache) bleiben dabei unangetastet. */
    private File prepareFiles() throws Exception {
        File root = new File(getFilesDir(), "spar");
        copyAssetDir("public", new File(root, "public"));
        copyAssetDir("data", new File(root, "data"));
        return root;
    }

    private void copyAssetDir(String assetPath, File target) throws Exception {
        String[] entries = getAssets().list(assetPath);
        if (entries == null || entries.length == 0) return;
        target.mkdirs();
        for (String name : entries) {
            String child = assetPath + "/" + name;
            String[] sub = getAssets().list(child);
            if (sub != null && sub.length > 0) {
                copyAssetDir(child, new File(target, name));
            } else {
                try (InputStream in = getAssets().open(child);
                     OutputStream out = new FileOutputStream(new File(target, name))) {
                    byte[] buf = new byte[65536];
                    int n;
                    while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
                }
            }
        }
    }

    private synchronized void startPythonServer(File root) {
        if (serverStarted) return;
        serverStarted = true;
        if (!Python.isStarted()) Python.start(new AndroidPlatform(this));
        Python py = Python.getInstance();
        var environ = py.getModule("os").get("environ");
        environ.callAttr("__setitem__", "SPAR_DATA_DIR", new File(root, "data").getAbsolutePath());
        environ.callAttr("__setitem__", "SPAR_PUBLIC_DIR", new File(root, "public").getAbsolutePath());
        environ.callAttr("__setitem__", "HOST", "127.0.0.1");
        environ.callAttr("__setitem__", "PORT", "8325");
        Thread t = new Thread(() -> py.getModule("server").callAttr("main"));
        t.setDaemon(true);
        t.start();
    }

    private void waitForPort(long timeoutMs) throws Exception {
        long deadline = System.currentTimeMillis() + timeoutMs;
        while (System.currentTimeMillis() < deadline) {
            try (Socket s = new Socket()) {
                s.connect(new InetSocketAddress("127.0.0.1", 8325), 1000);
                return;
            } catch (Exception ignored) {
                Thread.sleep(300);
            }
        }
        throw new RuntimeException("Lokaler Server antwortet nicht (Port 8325)");
    }

    private void showMessage(String title, String body) {
        webView.loadData("<html><body style='font-family:sans-serif;padding:2em;text-align:center'>"
                        + "<h2>" + title + "</h2><p>" + body + "</p></body></html>",
                "text/html; charset=utf-8", null);
    }

    /** Von public/app.js aufgerufen (window.SparApp) - startet den Fortschritts-Service. */
    private class SparBridge {
        @JavascriptInterface
        public void scanStarted(String jobId) {
            runOnUiThread(() -> {
                if (Build.VERSION.SDK_INT >= 33 &&
                        checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS)
                                != PackageManager.PERMISSION_GRANTED) {
                    requestPermissions(
                            new String[]{android.Manifest.permission.POST_NOTIFICATIONS}, 1);
                }
                Intent i = new Intent(LocalMainActivity.this, ScanWatchService.class);
                i.putExtra("jobId", jobId);
                try {
                    startForegroundService(i);
                } catch (Exception e) {
                    // Android 12+: FGS-Start aus dem Hintergrund verboten - Scan laeuft
                    // trotzdem (gleicher Prozess), nur ohne Benachrichtigung.
                }
            });
        }

        @JavascriptInterface
        public void scanFinished(String ignored) {
            // Der Service erkennt das Ende selbst ueber den Snapshot.
        }
    }

    @Override
    public boolean onCreateOptionsMenu(Menu menu) {
        menu.add(0, 1, 0, "Neu laden");
        return true;
    }

    @Override
    public boolean onOptionsItemSelected(MenuItem item) {
        if (item.getItemId() == 1) {
            webView.loadUrl(BASE_URL);
            return true;
        }
        return super.onOptionsItemSelected(item);
    }

    @Override
    public void onBackPressed() {
        if (webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }
}

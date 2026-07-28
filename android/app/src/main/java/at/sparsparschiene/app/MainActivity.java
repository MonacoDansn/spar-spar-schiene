package at.sparsparschiene.app;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.text.InputType;
import android.view.Menu;
import android.view.MenuItem;
import android.webkit.HttpAuthHandler;
import android.webkit.JavascriptInterface;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.LinearLayout;

/**
 * Spar Spar Schiene - duenner WebView-Wrapper.
 * Die eigentliche App laeuft auf dem eigenen Server (Render oder PC daheim);
 * die Server-URL ist beim ersten Start einstellbar und jederzeit aenderbar.
 */
public class MainActivity extends Activity {

    private static final String DEFAULT_URL = "https://spar-spar-schiene.onrender.com";
    private WebView webView;
    private SharedPreferences prefs;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences("sss", MODE_PRIVATE);

        webView = new WebView(this);
        webView.getSettings().setJavaScriptEnabled(true);
        webView.getSettings().setDomStorageEnabled(true);
        webView.addJavascriptInterface(new SparBridge(), "SparApp");
        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                String base = Uri.parse(baseUrl()).getHost();
                if (uri.getHost() != null && !uri.getHost().equals(base)) {
                    // Externe Links (z.B. OeBB-Ticketshop) im richtigen Browser oeffnen
                    startActivity(new Intent(Intent.ACTION_VIEW, uri));
                    return true;
                }
                return false;
            }

            @Override
            public void onReceivedHttpAuthRequest(WebView view, HttpAuthHandler handler,
                                                  String host, String realm) {
                String user = prefs.getString("auth_user", null);
                String pass = prefs.getString("auth_pass", null);
                if (user != null && pass != null && !prefs.getBoolean("auth_failed", false)) {
                    prefs.edit().putBoolean("auth_failed", true).apply(); // Schleifenschutz
                    handler.proceed(user, pass);
                } else {
                    showAuthDialog(handler);
                }
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                // Seite geladen -> gespeicherte Zugangsdaten haben funktioniert
                prefs.edit().putBoolean("auth_failed", false).apply();
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request,
                                        WebResourceError error) {
                if (request.isForMainFrame()) {
                    showUrlDialog("Server nicht erreichbar - URL pruefen:");
                }
            }
        });
        setContentView(webView);

        if (prefs.getString("base_url", null) == null) {
            showUrlDialog("Server-URL der Spar-Spar-Schiene-Instanz:");
        } else {
            webView.loadUrl(baseUrl());
        }
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
                Intent i = new Intent(MainActivity.this, ScanWatchService.class);
                i.putExtra("jobId", jobId);
                i.putExtra("baseUrl", baseUrl());
                i.putExtra("user", prefs.getString("auth_user", null));
                i.putExtra("pass", prefs.getString("auth_pass", null));
                startForegroundService(i);
            });
        }

        @JavascriptInterface
        public void scanFinished() {
            // Nur Hinweis - der Service erkennt das Ende selbst ueber den Snapshot.
        }
    }

    private String baseUrl() {
        return prefs.getString("base_url", DEFAULT_URL);
    }

    private void showUrlDialog(String message) {
        EditText input = new EditText(this);
        input.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        input.setText(baseUrl());
        new AlertDialog.Builder(this)
                .setTitle("Spar Spar Schiene")
                .setMessage(message)
                .setView(input)
                .setCancelable(false)
                .setPositiveButton("Laden", (d, w) -> {
                    String url = input.getText().toString().trim();
                    if (!url.startsWith("http")) url = "https://" + url;
                    prefs.edit().putString("base_url", url).apply();
                    webView.loadUrl(url);
                })
                .show();
    }

    private void showAuthDialog(HttpAuthHandler handler) {
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        int pad = (int) (16 * getResources().getDisplayMetrics().density);
        layout.setPadding(pad, pad, pad, 0);
        EditText user = new EditText(this);
        user.setHint("Benutzername (beliebig)");
        user.setText(prefs.getString("auth_user", "daniel"));
        EditText pass = new EditText(this);
        pass.setHint("Passwort");
        pass.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        layout.addView(user);
        layout.addView(pass);
        new AlertDialog.Builder(this)
                .setTitle("Anmeldung")
                .setView(layout)
                .setCancelable(false)
                .setPositiveButton("OK", (d, w) -> {
                    String u = user.getText().toString();
                    String p = pass.getText().toString();
                    prefs.edit().putString("auth_user", u).putString("auth_pass", p)
                            .putBoolean("auth_failed", false).apply();
                    handler.proceed(u, p);
                })
                .setNegativeButton("Abbrechen", (d, w) -> handler.cancel())
                .show();
    }

    @Override
    public boolean onCreateOptionsMenu(Menu menu) {
        menu.add(0, 1, 0, "Neu laden");
        menu.add(0, 2, 0, "Server-URL aendern");
        menu.add(0, 3, 0, "Zugangsdaten loeschen");
        return true;
    }

    @Override
    public boolean onOptionsItemSelected(MenuItem item) {
        switch (item.getItemId()) {
            case 1:
                webView.reload();
                return true;
            case 2:
                showUrlDialog("Neue Server-URL:");
                return true;
            case 3:
                prefs.edit().remove("auth_user").remove("auth_pass")
                        .putBoolean("auth_failed", false).apply();
                webView.reload();
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

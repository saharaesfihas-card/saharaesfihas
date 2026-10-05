package br.com.saharaesfihas.app;

import android.Manifest;
import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.net.http.SslError;
import android.os.Build;
import android.os.Bundle;
import android.os.Message;
import android.view.Gravity;
import android.view.View;
import android.view.WindowInsets;
import android.webkit.CookieManager;
import android.webkit.GeolocationPermissions;
import android.webkit.SslErrorHandler;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;
import java.util.ArrayList;

public final class MainActivity extends Activity {
    private static final String HOST = "saharaesfihas-card.github.io";
    private static final String START = "https://" + HOST + "/saharaesfihas/?app=1";
    private static final int LOCATION = 41;
    private WebView web;
    private ProgressBar progress;
    private LinearLayout errorPanel;
    private GeolocationPermissions.Callback locationCallback;
    private String locationOrigin;
    private final ArrayList<WebView> popups = new ArrayList<>();

    private boolean secureHost(Uri uri) {
        return uri != null && "https".equalsIgnoreCase(uri.getScheme())
            && HOST.equalsIgnoreCase(uri.getHost()) && uri.getUserInfo() == null
            && (uri.getPort() == -1 || uri.getPort() == 443);
    }

    private boolean internal(String url) {
        if (url == null) return false;
        Uri uri = Uri.parse(url);
        String path = uri.getPath();
        return secureHost(uri) && path != null
            && (path.equals("/saharaesfihas") || path.startsWith("/saharaesfihas/"));
    }

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.rgb(255, 252, 247));
        getWindow().setStatusBarColor(Color.rgb(255, 252, 247));
        getWindow().setNavigationBarColor(Color.rgb(255, 252, 247));
        int flags = View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR;
        if (Build.VERSION.SDK_INT >= 26) flags |= View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR;
        getWindow().getDecorView().setSystemUiVisibility(flags);
        if (Build.VERSION.SDK_INT >= 30) getWindow().setDecorFitsSystemWindows(false);
        root.setOnApplyWindowInsetsListener(new View.OnApplyWindowInsetsListener() {
            @Override public WindowInsets onApplyWindowInsets(View view, WindowInsets insets) {
            if (Build.VERSION.SDK_INT >= 30) {
                android.graphics.Insets bars = insets.getInsets(WindowInsets.Type.systemBars());
                view.setPadding(bars.left, bars.top, bars.right, bars.bottom);
            } else {
                view.setPadding(insets.getSystemWindowInsetLeft(), insets.getSystemWindowInsetTop(),
                    insets.getSystemWindowInsetRight(), insets.getSystemWindowInsetBottom());
            }
            return insets;
            }
        });
        web = new WebView(this);
        root.addView(web, new FrameLayout.LayoutParams(-1, -1));
        progress = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        root.addView(progress, new FrameLayout.LayoutParams(-1, 8, Gravity.TOP));
        errorPanel = new LinearLayout(this);
        errorPanel.setOrientation(LinearLayout.VERTICAL);
        errorPanel.setGravity(Gravity.CENTER);
        errorPanel.setPadding(32, 32, 32, 32);
        errorPanel.setBackgroundColor(Color.rgb(255, 252, 247));
        TextView title = new TextView(this);
        title.setText("Sahara Esfihas"); title.setTextSize(26); title.setGravity(Gravity.CENTER);
        title.setTextColor(Color.rgb(97, 30, 45));
        TextView description = new TextView(this);
        description.setText("Não foi possível abrir o cardápio. Confira sua conexão e tente novamente.");
        description.setTextSize(17); description.setGravity(Gravity.CENTER);
        Button retry = new Button(this); retry.setText("Tentar novamente");
        retry.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View view) { errorPanel.setVisibility(View.GONE); web.loadUrl(START); }
        });
        errorPanel.addView(title); errorPanel.addView(description); errorPanel.addView(retry);
        root.addView(errorPanel, new FrameLayout.LayoutParams(-1, -1));
        errorPanel.setVisibility(View.GONE);
        setContentView(root);
        configure(web);
        web.getSettings().setUserAgentString(web.getSettings().getUserAgentString() + " SaharaAndroid/1");
        web.getSettings().setSupportMultipleWindows(true);
        web.setDownloadListener(new android.webkit.DownloadListener() {
            @Override public void onDownloadStart(String url, String userAgent, String contentDisposition,
                String mimeType, long contentLength) { openExternal(url); }
        });
        web.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                return navigate(request.getUrl().toString());
            }
            @Override public boolean shouldOverrideUrlLoading(WebView view, String url) { return navigate(url); }
            @Override public void onPageStarted(WebView view, String url, android.graphics.Bitmap icon) {
                progress.setVisibility(View.VISIBLE); errorPanel.setVisibility(View.GONE);
            }
            @Override public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) showError();
            }
            @Override public void onReceivedHttpError(WebView view, WebResourceRequest request, WebResourceResponse response) {
                if (request.isForMainFrame() && response.getStatusCode() >= 400) showError();
            }
            @Override public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
                handler.cancel(); showError();
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override public void onProgressChanged(WebView view, int value) {
                progress.setProgress(value); if (value == 100) progress.setVisibility(View.GONE);
            }
            @Override public void onGeolocationPermissionsShowPrompt(String origin, GeolocationPermissions.Callback callback) {
                Uri uri = Uri.parse(origin);
                if (!secureHost(uri) || !internal(web.getUrl())) { callback.invoke(origin, false, false); return; }
                if (hasLocation()) { callback.invoke(origin, true, false); return; }
                finishLocation(false);
                locationOrigin = origin; locationCallback = callback;
                requestPermissions(new String[]{Manifest.permission.ACCESS_FINE_LOCATION,
                    Manifest.permission.ACCESS_COARSE_LOCATION}, LOCATION);
            }
            @Override public boolean onCreateWindow(WebView view, boolean dialog, boolean userGesture, Message message) {
                if (!userGesture || popups.size() >= 4) return false;
                WebView popup = new WebView(MainActivity.this);
                configure(popup); popups.add(popup);
                popup.setWebViewClient(new WebViewClient() {
                    private boolean target(String url) {
                        if ("about:blank".equals(url)) return false;
                        if (internal(url)) web.loadUrl(url); else openExternal(url);
                        web.post(new Runnable() { @Override public void run() { removePopup(popup); } });
                        return true;
                    }
                    @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) { return target(request.getUrl().toString()); }
                    @Override public boolean shouldOverrideUrlLoading(WebView view, String url) { return target(url); }
                    @Override public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) { handler.cancel(); }
                });
                popup.setWebChromeClient(new WebChromeClient() {
                    @Override public void onGeolocationPermissionsShowPrompt(String origin, GeolocationPermissions.Callback callback) { callback.invoke(origin, false, false); }
                    @Override public void onCloseWindow(WebView view) { removePopup(view); }
                });
                ((WebView.WebViewTransport)message.obj).setWebView(popup);
                message.sendToTarget(); return true;
            }
        });
        if (state == null || web.restoreState(state) == null) web.loadUrl(START);
    }

    private void configure(WebView view) {
        WebSettings settings = view.getSettings();
        settings.setJavaScriptEnabled(true); settings.setDomStorageEnabled(true);
        settings.setGeolocationEnabled(true); settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false); settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setJavaScriptCanOpenWindowsAutomatically(false);
        if (Build.VERSION.SDK_INT >= 26) settings.setSafeBrowsingEnabled(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(view, false);
    }

    private boolean navigate(String url) {
        if (internal(url)) return false;
        openExternal(url); return true;
    }

    private void openExternal(String url) {
        Uri uri = Uri.parse(url);
        if (!"https".equalsIgnoreCase(uri.getScheme()) || uri.getHost() == null || uri.getUserInfo() != null) {
            Toast.makeText(this, "Este link não pode ser aberto pelo app.", Toast.LENGTH_SHORT).show(); return;
        }
        try { startActivity(Intent.createChooser(new Intent(Intent.ACTION_VIEW, uri), "Abrir com")); }
        catch (ActivityNotFoundException exception) {
            Toast.makeText(this, "Instale um navegador ou o WhatsApp para abrir este link.", Toast.LENGTH_LONG).show();
        }
    }

    private boolean hasLocation() {
        return checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED
            || checkSelfPermission(Manifest.permission.ACCESS_COARSE_LOCATION) == PackageManager.PERMISSION_GRANTED;
    }

    private void finishLocation(boolean allowed) {
        if (locationCallback != null) locationCallback.invoke(locationOrigin, allowed, false);
        locationCallback = null; locationOrigin = null;
    }

    @Override public void onRequestPermissionsResult(int request, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(request, permissions, results);
        if (request == LOCATION) finishLocation(hasLocation());
    }

    private void removePopup(WebView popup) {
        if (popups.remove(popup)) { popup.stopLoading(); popup.destroy(); }
    }

    private void showError() { progress.setVisibility(View.GONE); errorPanel.setVisibility(View.VISIBLE); }
    @Override public void onBackPressed() { if (web.canGoBack()) web.goBack(); else super.onBackPressed(); }
    @Override protected void onSaveInstanceState(Bundle state) { web.saveState(state); super.onSaveInstanceState(state); }
    @Override protected void onPause() { web.onPause(); super.onPause(); }
    @Override protected void onResume() { super.onResume(); web.onResume(); }
    @Override protected void onDestroy() {
        finishLocation(false);
        for (WebView popup : new ArrayList<>(popups)) removePopup(popup);
        web.destroy(); super.onDestroy();
    }
}

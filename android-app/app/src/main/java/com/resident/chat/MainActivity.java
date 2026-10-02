package com.resident.chat;

import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.util.Base64;
import android.webkit.JavascriptInterface;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.widget.Toast;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;

/** 她的专属聊天 App。界面 = assets/chat.html；常驻轮询在 PollService。 */
public class MainActivity extends Activity {

    private WebView web;
    private ValueCallback<Uri[]> filePathCallback;
    private static final int REQ_PICK = 1001;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        web = new WebView(this);
        setContentView(web);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        // 界面是 file:// 本地页，要请求云机接口（跨域）。这个开关必须开：
        // 关掉后 fetch 被同源策略静默拦掉，表现成 "Failed to fetch"。
        // 残余风险靠"只加载本地 asset、从不加载远端 URL"兜着。想彻底去掉得改用
        // WebViewAssetLoader 把页面挂到 https://appassets.androidplatform.net 下。
        s.setAllowUniversalAccessFromFileURLs(true);
        // 这条不需要：它允许 file:// 页读其它本地文件，而本页面从不读。关掉只减攻击面
        s.setAllowFileAccessFromFileURLs(false);
        s.setLoadWithOverviewMode(false);
        s.setUseWideViewPort(true);
        s.setSupportZoom(false);
        s.setCacheMode(WebSettings.LOAD_NO_CACHE);
        // file:// 源下混合内容策略本来不生效。留着是为了将来换 WebViewAssetLoader
        // （https 源）时记得改成 COMPATIBILITY_MODE
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        s.setUserAgentString(s.getUserAgentString() + " ZhixiaChat/1.0");

        web.addJavascriptInterface(new JsBridge(), "Android");
        web.setWebViewClient(new android.webkit.WebViewClient());
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView v,
                                             ValueCallback<Uri[]> cb,
                                             FileChooserParams params) {
                filePathCallback = cb;
                Intent i = params.createIntent();
                i.setType("image/*");
                try {
                    startActivityForResult(i, REQ_PICK);
                } catch (Exception e) {
                    filePathCallback = null;
                    return false;
                }
                return true;
            }
        });

        if (Build.VERSION.SDK_INT >= 33) {
            requestPermissions(new String[]{"android.permission.POST_NOTIFICATIONS"}, 7);
        }

        // 常驻轮询服务：App 在后台/被划掉也继续检查她的新消息
        Intent svc = new Intent(this, PollService.class);
        if (Build.VERSION.SDK_INT >= 26) startForegroundService(svc);
        else startService(svc);

        web.loadUrl("file:///android_asset/chat.html");
        // 页面自己的 localStorage 会被"清除 WebView 数据 / 换包名重装"清空，
        // 原生这边存的 base/token 却还在（清 WebView 不清它）。回灌一次，
        // 否则用户明明填过，页面却当没填过，每个请求都发成相对路径
        SharedPreferences sp = getSharedPreferences("zx", MODE_PRIVATE);
        final String savedBase = sp.getString("base", "");
        final String savedToken = sp.getString("token", "");
        web.postDelayed(new Runnable() {
            @Override public void run() {
                web.evaluateJavascript(
                        "(function(){try{"
                        + "if(!localStorage.getItem('zx_base')&&" + jsStr(savedBase) + ")"
                        + "localStorage.setItem('zx_base'," + jsStr(savedBase) + ");"
                        + "if(!localStorage.getItem('zx_token')&&" + jsStr(savedToken) + ")"
                        + "localStorage.setItem('zx_token'," + jsStr(savedToken) + ");"
                        + "return !!localStorage.getItem('zx_base');}catch(e){return false}})()",
                        value -> {
                            // 回灌成功且页面本来就空 -> 让它按新地址重新拉一次
                            if ("true".equals(value)) web.evaluateJavascript("location.reload()", null);
                        });
            }
        }, 800);
    }

    /** 把 Java 字符串安全地转成 JS 字面量（防注入 + 防反斜杠/引号被吃掉）。 */
    private static String jsStr(String s) {
        StringBuilder sb = new StringBuilder("\"");
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '"' || c == '\\') sb.append('\\').append(c);
            else if (c == '\n' || c == '\r') sb.append("\\n");
            else if (c < 0x20) sb.append(String.format("\\u%04x", (int) c));
            else sb.append(c);
        }
        return sb.append('"').toString();
    }

    @Override
    protected void onActivityResult(int req, int res, Intent data) {
        super.onActivityResult(req, res, data);
        if (req != REQ_PICK) return;
        Uri[] uris = null;
        if (res == RESULT_OK && data != null) {
            if (data.getClipData() != null && data.getClipData().getItemCount() > 0) {
                uris = new Uri[]{data.getClipData().getItemAt(0).getUri()};
            } else if (data.getData() != null) {
                uris = new Uri[]{data.getData()};
            }
        }
        ValueCallback<Uri[]> cb = filePathCallback;
        filePathCallback = null;
        if (cb != null) cb.onReceiveValue(uris);

        if (uris != null && uris.length > 0) {
            String b64 = readImageBase64(uris[0]);
            if (b64 != null) {
                web.evaluateJavascript(
                        "window.__onPickGlobal ? window.__onPickGlobal('" + b64 + "')"
                        + " : (window.__onPick && window.__onPick('" + b64 + "'))",
                        null);
            } else {
                Toast.makeText(this, "这张图读不出来，换一张试试", Toast.LENGTH_SHORT).show();
            }
        }
    }

    /** 微信式返回：聊天→列表、朋友圈→发现、设置→我，最后才退出 App */
    @Override
    public void onBackPressed() {
        web.evaluateJavascript("androidBack()", value -> {
            if (value == null || !value.contains("true")) {
                // 退到后台而不是销毁：常驻服务继续收她的消息
                Intent home = new Intent(Intent.ACTION_MAIN);
                home.addCategory(Intent.CATEGORY_HOME);
                startActivity(home);
            }
        });
    }

    private String readImageBase64(Uri uri) {
        try {
            InputStream in = getContentResolver().openInputStream(uri);
            if (in == null) return null;
            Bitmap bm = android.graphics.BitmapFactory.decodeStream(in);
            in.close();
            if (bm == null) return null;
            int w = bm.getWidth(), h = bm.getHeight();
            float scale = Math.min(1f, 1280f / Math.max(w, h));
            if (scale < 1f) {
                bm = Bitmap.createScaledBitmap(bm, Math.round(w * scale),
                        Math.round(h * scale), true);
            }
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            bm.compress(Bitmap.CompressFormat.JPEG, 82, out);
            return Base64.encodeToString(out.toByteArray(), Base64.NO_WRAP);
        } catch (Exception e) {
            return null;
        }
    }

    private class JsBridge {
        @JavascriptInterface
        public void toast(String msg) {
            runOnUiThread(() -> Toast.makeText(MainActivity.this, msg,
                    Toast.LENGTH_SHORT).show());
        }

        /** 页面读过消息后同步"读到哪了"，服务据此判断要不要弹通知 */
        @JavascriptInterface
        public void markSeen(String t) {
            getSharedPreferences("zx", MODE_PRIVATE)
                    .edit().putString("last_seen_t", t == null ? "" : t).apply();
        }

        /** 页面里改了连接地址/口令时，同步给常驻服务 */
        @JavascriptInterface
        public void saveConn(String base, String token) {
            getSharedPreferences("zx", MODE_PRIVATE).edit()
                    .putString("base", base).putString("token", token).apply();
        }

        /** 页面从 /api/persona 拿到人设名后同步过来，好让通知标题用她的名字而不是写死的 */
        @JavascriptInterface
        public void saveWho(String name) {
            getSharedPreferences("zx", MODE_PRIVATE).edit()
                    .putString("who", name == null ? "" : name).apply();
        }

        /** 内置更新：跳到浏览器下载新 APK */
        @JavascriptInterface
        public void openUrl(String url) {
            runOnUiThread(() -> {
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
                } catch (Exception e) {
                    Toast.makeText(MainActivity.this, "没有能打开链接的应用",
                            Toast.LENGTH_SHORT).show();
                }
            });
        }

        /** 内置更新：App 内直接下载新 APK（系统下载器，完成后点通知安装） */
        @JavascriptInterface
        public void installUpdate(String url) {
            runOnUiThread(() -> {
                try {
                    android.app.DownloadManager.Request req =
                            new android.app.DownloadManager.Request(Uri.parse(url));
                    req.setTitle("新版本");
                    req.setDescription("下载完成后点通知安装");
                    req.setMimeType("application/vnd.android.package-archive");
                    req.setNotificationVisibility(
                            android.app.DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                    android.app.DownloadManager dm =
                            (android.app.DownloadManager) getSystemService(DOWNLOAD_SERVICE);
                    if (dm != null) {
                        dm.enqueue(req);
                        Toast.makeText(MainActivity.this, "开始下载，完成后点通知安装",
                                Toast.LENGTH_LONG).show();
                    }
                } catch (Exception e) {
                    Toast.makeText(MainActivity.this, "下载失败：" + e.getMessage(),
                            Toast.LENGTH_LONG).show();
                }
            });
        }

        /** 导出错误日志：写成 txt 放到「下载」目录。
         * Android 10 以上走 MediaStore，不用申请存储权限；老机器退到 App 自己的
         * 目录（在 Android/data/下面，文件管理器能翻到）。 */
        @JavascriptInterface
        public void saveLog(String name, String text) {
            String n = (name == null || name.trim().isEmpty())
                    ? "chat_log.txt" : name.trim();
            String body = (text == null) ? "" : text;
            String where;
            try {
                if (Build.VERSION.SDK_INT >= 29) {
                    android.content.ContentValues cv = new android.content.ContentValues();
                    cv.put(android.provider.MediaStore.Downloads.DISPLAY_NAME, n);
                    cv.put(android.provider.MediaStore.Downloads.MIME_TYPE, "text/plain");
                    cv.put(android.provider.MediaStore.Downloads.IS_PENDING, 1);
                    Uri uri = getContentResolver().insert(
                            android.provider.MediaStore.Downloads.EXTERNAL_CONTENT_URI, cv);
                    if (uri == null) throw new Exception("写不进下载目录");
                    OutputStream os = getContentResolver().openOutputStream(uri);
                    if (os == null) throw new Exception("打不开输出流");
                    os.write(body.getBytes("UTF-8"));
                    os.flush();
                    os.close();
                    cv.clear();
                    cv.put(android.provider.MediaStore.Downloads.IS_PENDING, 0);
                    getContentResolver().update(uri, cv, null, null);
                    where = "已保存到「下载」目录：" + n;
                } else {
                    File dir = getExternalFilesDir(android.os.Environment.DIRECTORY_DOWNLOADS);
                    if (dir != null && !dir.exists()) dir.mkdirs();
                    File f = new File(dir, n);
                    FileOutputStream fos = new FileOutputStream(f);
                    fos.write(body.getBytes("UTF-8"));
                    fos.flush();
                    fos.close();
                    where = "已保存到 " + f.getAbsolutePath();
                }
            } catch (Exception e) {
                where = "保存失败：" + e.getMessage();
            }
            final String msg = where;
            runOnUiThread(() -> Toast.makeText(MainActivity.this, msg,
                    Toast.LENGTH_LONG).show());
        }

        /** 把日志当一段文字分享出去（微信 / 邮件 / 备忘录都行）。
         * 走系统分享，不用任何权限 —— 出问题时最快的一条路。 */
        @JavascriptInterface
        public void shareText(String text) {
            runOnUiThread(() -> {
                try {
                    Intent i = new Intent(Intent.ACTION_SEND);
                    i.setType("text/plain");
                    i.putExtra(Intent.EXTRA_SUBJECT, "错误日志");
                    i.putExtra(Intent.EXTRA_TEXT, (text == null) ? "" : text);
                    startActivity(Intent.createChooser(i, "把日志发给…"));
                } catch (Exception e) {
                    Toast.makeText(MainActivity.this, "分享失败：" + e.getMessage(),
                            Toast.LENGTH_LONG).show();
                }
            });
        }
        /** 表情包/图片保存：存进相册「知夏图片」目录。图片字节由页面传 base64 过来。
         * Android 10+ 走 MediaStore 免权限；老机器退到 App 私有目录。 */
        @JavascriptInterface
        public void saveImage(String name, String b64) {
            String n = (name == null || name.trim().isEmpty())
                    ? ("chat_" + System.currentTimeMillis() + ".jpg") : name.trim();
            String msg;
            try {
                byte[] data = Base64.decode(b64 == null ? "" : b64, Base64.NO_WRAP);
                if (data.length == 0) throw new Exception("图是空的");
                if (Build.VERSION.SDK_INT >= 29) {
                    android.content.ContentValues cv = new android.content.ContentValues();
                    cv.put(android.provider.MediaStore.Images.Media.DISPLAY_NAME, n);
                    cv.put(android.provider.MediaStore.Images.Media.MIME_TYPE, "image/jpeg");
                    cv.put(android.provider.MediaStore.Images.Media.RELATIVE_PATH, "Pictures/知夏图片");
                    cv.put(android.provider.MediaStore.Images.Media.IS_PENDING, 1);
                    Uri uri = getContentResolver().insert(
                            android.provider.MediaStore.Images.Media.EXTERNAL_CONTENT_URI, cv);
                    if (uri == null) throw new Exception("写不进相册");
                    OutputStream os = getContentResolver().openOutputStream(uri);
                    if (os == null) throw new Exception("打不开输出流");
                    os.write(data);
                    os.flush();
                    os.close();
                    cv.clear();
                    cv.put(android.provider.MediaStore.Images.Media.IS_PENDING, 0);
                    getContentResolver().update(uri, cv, null, null);
                    msg = "已保存到相册「知夏图片」";
                } else {
                    File dir = getExternalFilesDir(android.os.Environment.DIRECTORY_PICTURES);
                    if (dir != null && !dir.exists()) dir.mkdirs();
                    File f = new File(dir, n);
                    FileOutputStream fos = new FileOutputStream(f);
                    fos.write(data);
                    fos.flush();
                    fos.close();
                    msg = "已保存到 " + f.getAbsolutePath();
                }
            } catch (Exception e) {
                msg = "保存失败：" + e.getMessage();
            }
            final String m = msg;
            runOnUiThread(() -> Toast.makeText(MainActivity.this, m,
                    Toast.LENGTH_LONG).show());
        }

        /** 删除之前保存的那张图。只能删本 App 自己写进相册的（MediaStore 按
         * 文件名 + 目录查），别人 App 的图系统会拒，删不动就提示。 */
        @JavascriptInterface
        public void deleteImage(String name) {
            String n = (name == null || name.trim().isEmpty()) ? "" : name.trim();
            String msg;
            try {
                if (Build.VERSION.SDK_INT >= 29) {
                    String sel = android.provider.MediaStore.Images.Media.DISPLAY_NAME
                            + "=? AND " + android.provider.MediaStore.Images.Media.RELATIVE_PATH + "=?";
                    int k = getContentResolver().delete(
                            android.provider.MediaStore.Images.Media.EXTERNAL_CONTENT_URI,
                            sel, new String[]{n, "Pictures/知夏图片/"});
                    msg = k > 0 ? "已删除：" + n : "相册里没找到这张（可能已经删了）";
                } else {
                    File dir = getExternalFilesDir(android.os.Environment.DIRECTORY_PICTURES);
                    File f = dir == null ? null : new File(dir, n);
                    msg = (f != null && f.exists() && f.delete())
                            ? "已删除：" + n : "没找到这张图";
                }
            } catch (Exception e) {
                msg = "删除失败：" + e.getMessage();
            }
            final String m = msg;
            runOnUiThread(() -> Toast.makeText(MainActivity.this, m,
                    Toast.LENGTH_LONG).show());
        }
    }
}

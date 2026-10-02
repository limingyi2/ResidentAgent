package com.resident.chat;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Intent;
import android.os.Build;
import android.os.IBinder;

import org.json.JSONObject;

import java.net.HttpURLConnection;
import java.net.URL;

/** 常驻前台服务：App 退到后台甚至被划掉后，仍每 30 秒检查她有没有新消息。 */
public class PollService extends Service {

    private static final String CHAN = "messages";
    private volatile boolean running = false;

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        Notification.Builder b = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, CHAN)
                : new Notification.Builder(this);
        b.setSmallIcon(android.R.drawable.stat_notify_chat)
         .setContentTitle("守护中")
         .setContentText("正在守护你们的聊天")
         .setOngoing(true);
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationManager nm = getSystemService(NotificationManager.class);
            if (nm != null) {
                nm.createNotificationChannel(new NotificationChannel(
                        CHAN, "她的消息", NotificationManager.IMPORTANCE_DEFAULT));
            }
        }
        startForeground(2, b.build());

        if (!running) {
            running = true;
            Thread t = new Thread(this::pollLoop);
            t.setDaemon(true);
            t.start();
        }
        return START_STICKY;
    }

    /**
     * 带鉴权的 GET。token 走 Authorization 头，不再拼进 URL ——
     * URL 会进浏览器历史、Referer 和各级访问日志。
     * （只有 <img>/<audio> 这类设不了头的媒体请求才继续用 ?token=，服务端两种都认。）
     */
    private HttpURLConnection open(String base, String path, String token)
            throws Exception {
        HttpURLConnection c =
                (HttpURLConnection) new URL(base + path).openConnection();
        c.setConnectTimeout(8000);
        c.setReadTimeout(8000);
        if (token != null && !token.isEmpty()) {
            c.setRequestProperty("Authorization", "Bearer " + token);
        }
        return c;
    }

    private void pollLoop() {
        while (running) {
            try {
                Thread.sleep(30000);
                android.content.SharedPreferences p =
                        getSharedPreferences("zx", MODE_PRIVATE);
                String base = p.getString("base", "")
                        .replaceAll("/+$", "");
                String token = p.getString("token", "");
                HttpURLConnection c = open(base, "/api/history?limit=1", token);
                if (c.getResponseCode() == 200) {
                    StringBuilder sb = new StringBuilder();
                    try (java.io.Reader r = new java.io.InputStreamReader(
                            c.getInputStream(), java.nio.charset.StandardCharsets.UTF_8)) {
                        char[] buf = new char[2048];
                        int n;
                        while ((n = r.read(buf)) > 0) sb.append(buf, 0, n);
                    }
                    JSONObject d = new JSONObject(sb.toString());
                    org.json.JSONArray items = d.optJSONArray("items");
                    if (items != null && items.length() > 0) {
                        JSONObject last = items.getJSONObject(items.length() - 1);
                        String ts = last.optString("t", "");
                        String role = last.optString("role", "");
                        String text = last.optString("text", "");
                        // 游标必须带上正文：t 只精确到分钟，同一分钟内来的第二条
                        // 会被当成"这条已经通知过"而整个漏掉（她连发两条时很常见）
                        String cur = ts + "\n" + text;
                        String seen = p.getString("last_seen_t", "");
                        if ("assistant".equals(role) && !cur.equals(seen)) {
                            p.edit().putString("last_seen_t", cur).apply();
                            notifyMsg(text.isEmpty() ? "有新消息" : text);
                        }
                    }
                }
                c.disconnect();

                // 内置更新检查：服务器上有更新版就通知一次。
                // 版本号取 BuildConfig（由 build.gradle 从仓库根 version.json 读），
                // 不再手写常量 —— 手写过四份，每份都不同步，装完照样提示更新。
                int installedCode = BuildConfig.VERSION_CODE;
                HttpURLConnection vc = open(base, "/api/app/version", token);
                if (vc.getResponseCode() == 200) {
                    StringBuilder sb = new StringBuilder();
                    try (java.io.Reader r = new java.io.InputStreamReader(
                            vc.getInputStream(), java.nio.charset.StandardCharsets.UTF_8)) {
                        char[] buf = new char[2048];
                        int n;
                        while ((n = r.read(buf)) > 0) sb.append(buf, 0, n);
                    }
                    JSONObject v = new JSONObject(sb.toString());
                    int code = v.optInt("code", 0);
                    int notified = p.getInt("update_notified", 0);
                    if (code > installedCode && notified < code) {
                        p.edit().putInt("update_notified", code).apply();
                        notifyUpdate();
                    }
                }
                vc.disconnect();
            } catch (Exception e) {
                /* 静默重试 */
            }
        }
    }

    private void notifyMsg(String text) {
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm == null) return;
        Notification.Builder b = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, CHAN)
                : new Notification.Builder(this);
        // 通知标题用人设名（页面启动时通过 Android.saveWho 同步过来），不写死角色名
        String who = getSharedPreferences("zx", MODE_PRIVATE)
                .getString("who", "").trim();
        b.setSmallIcon(android.R.drawable.stat_notify_chat)
         .setContentTitle(who.isEmpty() ? "新消息" : who)
         .setContentText(text.length() > 60 ? text.substring(0, 60) + "…" : text)
         .setAutoCancel(true);
        android.content.Intent i = new android.content.Intent(this, MainActivity.class);
        android.app.PendingIntent pi = android.app.PendingIntent.getActivity(
                this, 0, i, android.app.PendingIntent.FLAG_UPDATE_CURRENT
                        | (Build.VERSION.SDK_INT >= 23
                           ? android.app.PendingIntent.FLAG_IMMUTABLE : 0));
        b.setContentIntent(pi);
        nm.notify(1, b.build());
    }

    private void notifyUpdate() {
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm == null) return;
        Notification.Builder b = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, CHAN)
                : new Notification.Builder(this);
        b.setSmallIcon(android.R.drawable.stat_sys_download_done)
         .setContentTitle("有新版本")
         .setContentText("打开 App 会自动下载安装")
         .setAutoCancel(true);
        android.content.Intent i = new android.content.Intent(this, MainActivity.class);
        android.app.PendingIntent pi = android.app.PendingIntent.getActivity(
                this, 1, i, android.app.PendingIntent.FLAG_UPDATE_CURRENT
                        | (Build.VERSION.SDK_INT >= 23
                           ? android.app.PendingIntent.FLAG_IMMUTABLE : 0));
        b.setContentIntent(pi);
        nm.notify(2, b.build());
    }

    @Override
    public void onDestroy() {
        running = false;
        super.onDestroy();
    }
}

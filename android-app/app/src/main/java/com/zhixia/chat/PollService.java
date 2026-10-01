package com.zhixia.chat;

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
         .setContentTitle("角色")
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

    private void pollLoop() {
        while (running) {
            try {
                Thread.sleep(30000);
                android.content.SharedPreferences p =
                        getSharedPreferences("zx", MODE_PRIVATE);
                String base = p.getString("base", "")
                        .replaceAll("/+$", "");
                String token = p.getString("token", "");
                URL u = new URL(base + "/api/history?limit=1&token=" + token);
                HttpURLConnection c = (HttpURLConnection) u.openConnection();
                c.setConnectTimeout(8000);
                c.setReadTimeout(8000);
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
                        String seen = p.getString("last_seen_t", "");
                        if ("assistant".equals(role) && !ts.equals(seen)) {
                            p.edit().putString("last_seen_t", ts).apply();
                            notifyMsg(last.optString("text", "有新消息"));
                        }
                    }
                }
                c.disconnect();

                // 内置更新检查：服务器上有更新版就通知一次
                int installedCode = 21;   // 跟着 APK 版本走，每次发版改这里（与 chat.html APP_CODE 一致）
                URL vu = new URL(base + "/api/app/version?token=" + token);
                HttpURLConnection vc = (HttpURLConnection) vu.openConnection();
                vc.setConnectTimeout(8000);
                vc.setReadTimeout(8000);
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
        b.setSmallIcon(android.R.drawable.stat_notify_chat)
         .setContentTitle("角色")
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
         .setContentTitle("角色有新版本")
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

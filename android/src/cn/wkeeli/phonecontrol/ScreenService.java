package cn.wkeeli.phonecontrol;

import android.app.Activity;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.PixelFormat;
import android.graphics.Rect;
import android.hardware.display.DisplayManager;
import android.hardware.display.VirtualDisplay;
import android.media.AudioManager;
import android.media.Image;
import android.media.ImageReader;
import android.media.projection.MediaProjection;
import android.media.projection.MediaProjectionManager;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import android.os.SystemClock;
import android.util.DisplayMetrics;
import android.view.Display;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.nio.ByteBuffer;

/**
 * 前台服务：持有 MediaProjection，抓屏 → 缩放到最大边 540 → JPEG(q=60) → WS 二进制帧。
 * 同时负责与服务器的心跳、指令分发（ack/error）、viewers 控制推流。
 *
 * <p>关键顺序（Android 14 要求）：先 startForeground(type=mediaProjection)，再 getMediaProjection。
 */
@SuppressWarnings("deprecation")
public class ScreenService extends Service {

    public static final String ACTION_STATUS = "cn.wkeeli.phonecontrol.internal.STATUS";
    public static final String EXTRA_STATUS_TEXT = "status_text";

    public static final String ACTION_STOP = "cn.wkeeli.phonecontrol.action.STOP";

    public static final String EXTRA_RESULT_CODE = "result_code";
    public static final String EXTRA_RESULT_DATA = "result_data";
    public static final String EXTRA_SERVER_URL = "server_url";
    public static final String EXTRA_DEVICE_KEY = "device_key";

    private static final String CHANNEL_ID = "phonecontrol";
    private static final int NOTIF_ID = 0x1001;

    // 画面质量：按「宽度」缩放（竖屏手机按长边缩会窄到不可读），可被浏览器 quality 指令实时调整。
    private static final int MIN_TARGET_WIDTH = 240;
    private static final int MAX_TARGET_WIDTH = 1080;
    private static final int DEFAULT_TARGET_WIDTH = 540;
    private static final int MAX_TARGET_HEIGHT = 2200;
    private static final int DEFAULT_JPEG_QUALITY = 65;
    private static final double DEFAULT_FPS = 8;
    private static final int MAX_TEXT_LENGTH = 500;

    private volatile int targetWidth = DEFAULT_TARGET_WIDTH;
    private volatile int jpegQuality = DEFAULT_JPEG_QUALITY;
    private volatile long frameIntervalMs = (long) (1000.0 / DEFAULT_FPS);

    private MediaProjection projection;
    private VirtualDisplay virtualDisplay;
    private ImageReader imageReader;
    private HandlerThread captureThread;
    private Handler captureHandler;
    private Handler mainHandler;

    private WsClient ws;
    private volatile boolean destroyed;

    private int screenW;
    private int screenH;
    private int densityDpi;

    private volatile int viewers;
    private volatile boolean streaming;
    private boolean lastReportedFrames;
    private boolean lastAccessibilityState;

    private Bitmap rawBitmap;
    private Bitmap frameBitmap;
    private Canvas frameCanvas;
    private final Paint scalePaint = new Paint(Paint.FILTER_BITMAP_FLAG);
    private final Rect srcRect = new Rect();
    private final Rect dstRect = new Rect();
    private final ByteArrayOutputStream jpegBuffer = new ByteArrayOutputStream();
    private long lastFrameAt;

    // ---------------------------------------------------------------- 生命周期

    @Override
    public void onCreate() {
        super.onCreate();
        mainHandler = new Handler(Looper.getMainLooper());
        createNotificationChannel();
        mainHandler.post(accessibilityWatch);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null) {
            stopSelf();
            return START_NOT_STICKY;
        }
        if (ACTION_STOP.equals(intent.getAction())) {
            broadcastStatus("已手动停止");
            stopSelf();
            return START_NOT_STICKY;
        }
        int resultCode = intent.getIntExtra(EXTRA_RESULT_CODE, Activity.RESULT_CANCELED);
        Intent resultData = intent.getParcelableExtra(EXTRA_RESULT_DATA);
        String serverUrl = intent.getStringExtra(EXTRA_SERVER_URL);
        String deviceKey = intent.getStringExtra(EXTRA_DEVICE_KEY);

        if (resultCode != Activity.RESULT_OK || resultData == null) {
            broadcastStatus("未获得投屏授权");
            stopSelf();
            return START_NOT_STICKY;
        }

        // Android 14：必须先进入 mediaProjection 类型前台，才允许 getMediaProjection
        startForegroundCompat("正在启动…");
        if (!setupProjection(resultCode, resultData)) {
            stopSelf();
            return START_NOT_STICKY;
        }
        connect(serverUrl, deviceKey);
        return START_NOT_STICKY;
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onDestroy() {
        destroyed = true;
        mainHandler.removeCallbacksAndMessages(null);
        if (ws != null) {
            ws.stop();
            ws = null;
        }
        try {
            DisplayManager dm = (DisplayManager) getSystemService(Context.DISPLAY_SERVICE);
            if (dm != null) {
                dm.unregisterDisplayListener(displayListener);
            }
        } catch (RuntimeException ignored) {
        }
        releaseVirtualDisplay();
        if (projection != null) {
            try {
                projection.stop();
            } catch (RuntimeException ignored) {
            }
            projection = null;
        }
        if (captureThread != null) {
            captureThread.quitSafely();
            captureThread = null;
            captureHandler = null;
        }
        rawBitmap = null;
        frameBitmap = null;
        frameCanvas = null;
        broadcastStatus("服务已停止");
        super.onDestroy();
    }

    // ---------------------------------------------------------------- 投屏

    private boolean setupProjection(int resultCode, Intent resultData) {
        MediaProjectionManager manager =
                (MediaProjectionManager) getSystemService(Context.MEDIA_PROJECTION_SERVICE);
        if (manager == null) {
            broadcastStatus("本机不支持投屏");
            return false;
        }
        try {
            projection = manager.getMediaProjection(resultCode, resultData);
        } catch (RuntimeException e) {
            broadcastStatus("获取投屏失败: " + e.getMessage());
            return false;
        }
        if (projection == null) {
            broadcastStatus("投屏授权无效");
            return false;
        }
        projection.registerCallback(new MediaProjection.Callback() {
            @Override
            public void onStop() {
                if (destroyed) {
                    return;
                }
                broadcastStatus("投屏已停止");
                stopSelf();
            }
        }, mainHandler);

        captureThread = new HandlerThread("phonecontrol-capture");
        captureThread.start();
        captureHandler = new Handler(captureThread.getLooper());

        createVirtualDisplay();

        DisplayManager dm = (DisplayManager) getSystemService(Context.DISPLAY_SERVICE);
        if (dm != null) {
            dm.registerDisplayListener(displayListener, mainHandler);
        }
        return true;
    }

    private DisplayMetrics readScreenMetrics() {
        DisplayMetrics metrics = new DisplayMetrics();
        try {
            DisplayManager dm = (DisplayManager) getSystemService(Context.DISPLAY_SERVICE);
            Display display = dm == null ? null : dm.getDisplay(Display.DEFAULT_DISPLAY);
            if (display != null) {
                display.getRealMetrics(metrics);
                return metrics;
            }
        } catch (RuntimeException ignored) {
        }
        return getResources().getDisplayMetrics();
    }

    private void createVirtualDisplay() {
        if (projection == null) {
            return;
        }
        DisplayMetrics metrics = readScreenMetrics();
        screenW = metrics.widthPixels;
        screenH = metrics.heightPixels;
        densityDpi = metrics.densityDpi;

        releaseVirtualDisplay();

        try {
            imageReader = ImageReader.newInstance(screenW, screenH, PixelFormat.RGBA_8888, 2);
            imageReader.setOnImageAvailableListener(new ImageReader.OnImageAvailableListener() {
                @Override
                public void onImageAvailable(ImageReader reader) {
                    handleImageAvailable(reader);
                }
            }, captureHandler);
            virtualDisplay = projection.createVirtualDisplay(
                    "phonecontrol",
                    screenW, screenH, densityDpi,
                    DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
                    imageReader.getSurface(),
                    null,
                    captureHandler);
        } catch (RuntimeException e) {
            broadcastStatus("创建虚拟屏幕失败: " + e.getMessage());
        }
    }

    private void recreateVirtualDisplay() {
        if (destroyed || projection == null) {
            return;
        }
        rawBitmap = null;
        frameBitmap = null;
        frameCanvas = null;
        createVirtualDisplay();
    }

    private void releaseVirtualDisplay() {
        if (imageReader != null) {
            try {
                imageReader.setOnImageAvailableListener(null, null);
            } catch (RuntimeException ignored) {
            }
            try {
                imageReader.close();
            } catch (RuntimeException ignored) {
            }
            imageReader = null;
        }
        if (virtualDisplay != null) {
            try {
                virtualDisplay.release();
            } catch (RuntimeException ignored) {
            }
            virtualDisplay = null;
        }
    }

    private final DisplayManager.DisplayListener displayListener = new DisplayManager.DisplayListener() {
        @Override
        public void onDisplayAdded(int displayId) {
        }

        @Override
        public void onDisplayRemoved(int displayId) {
        }

        @Override
        public void onDisplayChanged(int displayId) {
            if (displayId != Display.DEFAULT_DISPLAY) {
                return;
            }
            mainHandler.removeCallbacks(geometryCheck);
            mainHandler.postDelayed(geometryCheck, 350);
        }
    };

    private final Runnable geometryCheck = new Runnable() {
        @Override
        public void run() {
            if (destroyed) {
                return;
            }
            DisplayMetrics metrics = readScreenMetrics();
            if (metrics.widthPixels == screenW && metrics.heightPixels == screenH) {
                return;
            }
            recreateVirtualDisplay();
            if (ws != null && ws.isOpen()) {
                sendHello();
            }
            broadcastStatus("屏幕分辨率已更新为 " + screenW + "x" + screenH);
        }
    };

    // ---------------------------------------------------------------- 抓帧

    private void handleImageAvailable(ImageReader reader) {
        Image image = null;
        try {
            image = reader.acquireLatestImage();
        } catch (RuntimeException e) {
            return;
        }
        if (image == null) {
            return;
        }
        try {
            if (!streaming || viewers <= 0) {
                return;
            }
            WsClient client = ws;
            if (client == null || !client.isOpen()) {
                return;
            }
            long now = SystemClock.elapsedRealtime();
            if (now - lastFrameAt < frameIntervalMs) {
                return;
            }
            lastFrameAt = now;
            Bitmap frame = convertToFrame(image);
            if (frame == null) {
                return;
            }
            jpegBuffer.reset();
            if (!frame.compress(Bitmap.CompressFormat.JPEG, jpegQuality, jpegBuffer)) {
                return;
            }
            client.sendBinary(jpegBuffer.toByteArray());
        } catch (Throwable ignored) {
            // 单帧失败不影响后续
        } finally {
            try {
                image.close();
            } catch (RuntimeException ignored) {
            }
        }
    }

    private Bitmap convertToFrame(Image image) {
        Image.Plane[] planes = image.getPlanes();
        if (planes == null || planes.length == 0) {
            return null;
        }
        Image.Plane plane = planes[0];
        int pixelStride = plane.getPixelStride();
        if (pixelStride != 4) {
            return null;
        }
        int width = image.getWidth();
        int height = image.getHeight();
        int rowStride = plane.getRowStride();
        int rowPadding = rowStride - pixelStride * width;
        if (rowPadding < 0) {
            rowPadding = 0;
        }
        int rawWidth = width + rowPadding / pixelStride;

        if (rawBitmap == null || rawBitmap.getWidth() != rawWidth || rawBitmap.getHeight() != height) {
            rawBitmap = Bitmap.createBitmap(rawWidth, height, Bitmap.Config.ARGB_8888);
        }
        ByteBuffer buffer = plane.getBuffer();
        buffer.rewind();
        rawBitmap.copyPixelsFromBuffer(buffer);

        return scaleRawBitmap(width, height);
    }

    /** 把 rawBitmap（可能是带行内边距的整屏）缩放成待编码的帧；按宽度缩放，防 OOM 限高。 */
    private Bitmap scaleRawBitmap(int width, int height) {
        if (rawBitmap == null) {
            return null;
        }
        // 按宽度等比缩放（竖屏手机按长边缩会窄到看不清），并给高度兜个上限防 OOM
        float scale = (float) targetWidth / (float) width;
        if (scale > 1f) {
            scale = 1f;
        }
        int dstW = Math.max(1, Math.round(width * scale));
        int dstH = Math.max(1, Math.round(height * scale));
        if (dstH > MAX_TARGET_HEIGHT) {
            float shrink = (float) MAX_TARGET_HEIGHT / (float) dstH;
            dstW = Math.max(1, Math.round(dstW * shrink));
            dstH = MAX_TARGET_HEIGHT;
        }

        if (frameBitmap == null || frameBitmap.getWidth() != dstW || frameBitmap.getHeight() != dstH) {
            frameBitmap = Bitmap.createBitmap(dstW, dstH, Bitmap.Config.ARGB_8888);
            frameCanvas = new Canvas(frameBitmap);
        }
        srcRect.set(0, 0, width, height);
        dstRect.set(0, 0, dstW, dstH);
        frameCanvas.drawBitmap(rawBitmap, srcRect, dstRect, scalePaint);
        return frameBitmap;
    }

    /**
     * 改清晰度后立刻用上一帧重新编码推送：MediaProjection 只在画面变化时出图，
     * 静止屏幕上不主动补一帧的话，用户会以为清晰度没生效。
     * 必须在抓帧线程（captureHandler）上执行，避免和 handleImageAvailable 抢 bitmap。
     */
    private void republishLastFrame() {
        if (destroyed || !streaming || viewers <= 0) {
            return;
        }
        WsClient client = ws;
        if (client == null || !client.isOpen() || rawBitmap == null) {
            return;
        }
        try {
            Bitmap frame = scaleRawBitmap(screenW, screenH);
            if (frame == null) {
                return;
            }
            jpegBuffer.reset();
            if (frame.compress(Bitmap.CompressFormat.JPEG, jpegQuality, jpegBuffer)) {
                client.sendBinary(jpegBuffer.toByteArray());
            }
        } catch (Throwable ignored) {
            // 补帧失败无所谓，等下一次画面变化
        }
    }

    /** 把「补推上一帧」丢到抓帧线程执行（bitmap 只能在该线程使用）。 */
    private void postRepublishLastFrame() {
        Handler handler = captureHandler;
        if (handler == null) {
            return;
        }
        handler.post(new Runnable() {
            @Override
            public void run() {
                republishLastFrame();
            }
        });
    }

    // ---------------------------------------------------------------- WebSocket

    private void connect(String serverUrl, String deviceKey) {
        String base = serverUrl;
        if (base == null || base.trim().isEmpty()) {
            base = BuildConfig.SERVER_URL;
        }
        String url = buildWsUrl(base, deviceKey);
        if (url == null) {
            broadcastStatus("服务器地址无效，请在 App 里重新填写");
            return;
        }
        ws = new WsClient(url, wsListener);
        ws.start();
        broadcastStatus("正在连接 " + url);
        updateNotification("正在连接服务器…");
    }

    /** 把用户填的地址规范成 ws/wss URL，并拼 key 参数。 */
    static String buildWsUrl(String base, String deviceKey) {
        if (base == null) {
            return null;
        }
        String url = base.trim();
        if (url.isEmpty()) {
            return null;
        }
        String lower = url.toLowerCase();
        if (lower.startsWith("https://")) {
            url = "wss://" + url.substring(8);
        } else if (lower.startsWith("http://")) {
            url = "ws://" + url.substring(7);
        } else if (!lower.startsWith("wss://") && !lower.startsWith("ws://")) {
            url = "ws://" + url;
        }
        while (url.endsWith("/")) {
            url = url.substring(0, url.length() - 1);
        }
        int schemeEnd = url.indexOf("://");
        int pathStart = schemeEnd < 0 ? -1 : url.indexOf('/', schemeEnd + 3);
        if (pathStart < 0) {
            url = url + "/agent/ws/device";
        }
        if (deviceKey != null && !deviceKey.isEmpty()) {
            try {
                String encoded = java.net.URLEncoder.encode(deviceKey, "UTF-8");
                url = url + (url.contains("?") ? "&" : "?") + "key=" + encoded;
            } catch (Exception ignored) {
                return url;
            }
        }
        return url;
    }

    private final WsClient.Listener wsListener = new WsClient.Listener() {
        @Override
        public void onOpen() {
            broadcastStatus("已连接服务器");
            updateNotification("已连接，等待电脑观看…");
            sendHello();
            updateStreamingState();
        }

        @Override
        public void onText(String text) {
            handleMessage(text);
        }

        @Override
        public void onBinary(byte[] data) {
            // 服务端不会给设备发二进制帧，忽略
        }

        @Override
        public void onClosed(String reason) {
            streaming = false;
            lastReportedFrames = false;
            broadcastStatus("连接断开（" + reason + "），正在重连…");
            updateNotification("连接断开，正在重连…");
        }

        @Override
        public void onError(String message) {
            streaming = false;
            broadcastStatus("连接失败（" + message + "），正在重连…");
            updateNotification("连接失败，正在重连…");
        }
    };

    private void handleMessage(String text) {
        if (text == null) {
            return;
        }
        try {
            JSONObject message = new JSONObject(text);
            String type = message.optString("t", "");
            if ("viewers".equals(type)) {
                int previous = viewers;
                viewers = message.optInt("n", 0);
                updateStreamingState();
                if (viewers > 0 && previous <= 0) {
                    // 刚有人观看：手机屏幕可能静止（不出新帧），先把上一帧补推过去
                    postRepublishLastFrame();
                }
                if (viewers > 0) {
                    updateNotification("电脑正在观看（" + viewers + " 个连接）");
                } else {
                    updateNotification("已连接，等待电脑观看…");
                }
            } else if ("cmd".equals(type)) {
                handleCommand(message);
            } else if ("ping".equals(type)) {
                sendPong();
            } else if ("bye".equals(type)) {
                String reason = message.optString("reason", "");
                broadcastStatus("服务端断开连接: " + reason);
                if ("replaced".equals(reason) && ws != null) {
                    // 另一台设备上线替换了本机；不要立刻重连去互相踢，交给用户决定
                    ws.stop();
                    updateNotification("已被其他设备替换");
                }
            }
        } catch (Exception ignored) {
            // 非 JSON 或字段异常，忽略
        }
    }

    private void updateStreamingState() {
        WsClient client = ws;
        boolean on = viewers > 0 && client != null && client.isOpen();
        streaming = on;
        if (on != lastReportedFrames) {
            lastReportedFrames = on;
            try {
                JSONObject message = new JSONObject();
                message.put("t", "frames");
                message.put("on", on);
                client.sendText(message.toString());
            } catch (Exception ignored) {
            }
        }
    }

    private void sendHello() {
        WsClient client = ws;
        if (client == null || !client.isOpen()) {
            return;
        }
        try {
            JSONObject hello = new JSONObject();
            hello.put("t", "hello");
            hello.put("name", Build.MODEL == null ? "Android" : Build.MODEL);
            hello.put("w", screenW);
            hello.put("h", screenH);
            boolean accessibility = RemoteAccessibilityService.isEnabled();
            hello.put("accessibility", accessibility);
            hello.put("version", 1);
            client.sendText(hello.toString());
            lastAccessibilityState = accessibility;
        } catch (Exception ignored) {
        }
    }

    private void sendPong() {
        WsClient client = ws;
        if (client != null) {
            client.sendText("{\"t\":\"pong\"}");
        }
    }

    private void sendAck(String id) {
        WsClient client = ws;
        if (client == null) {
            return;
        }
        try {
            JSONObject message = new JSONObject();
            message.put("t", "ack");
            if (id != null) {
                message.put("id", id);
            }
            client.sendText(message.toString());
        } catch (Exception ignored) {
        }
    }

    private void sendError(String id, String reason) {
        WsClient client = ws;
        if (client == null) {
            return;
        }
        try {
            JSONObject message = new JSONObject();
            message.put("t", "error");
            if (id != null) {
                message.put("id", id);
            }
            message.put("message", reason == null ? "执行失败" : reason);
            client.sendText(message.toString());
        } catch (Exception ignored) {
        }
    }

    // ---------------------------------------------------------------- 指令

    private void handleCommand(JSONObject command) {
        String id = command.optString("id", "");
        if (id.isEmpty()) {
            id = null;
        }
        String action = command.optString("action", "");
        switch (action) {
            case "tap": {
                Double nx = optCoordinate(command, "x");
                Double ny = optCoordinate(command, "y");
                if (nx == null || ny == null) {
                    sendError(id, "tap 缺少坐标 x/y");
                    return;
                }
                RemoteAccessibilityService service = requireAccessibility(id);
                if (service == null) {
                    return;
                }
                service.tap(toPixelX(nx), toPixelY(ny), gestureCallback(id));
                return;
            }
            case "swipe": {
                Double x1 = optCoordinate(command, "x1");
                Double y1 = optCoordinate(command, "y1");
                Double x2 = optCoordinate(command, "x2");
                Double y2 = optCoordinate(command, "y2");
                if (x1 == null || y1 == null || x2 == null || y2 == null) {
                    sendError(id, "swipe 缺少坐标");
                    return;
                }
                long ms = (long) command.optDouble("ms", 300);
                RemoteAccessibilityService service = requireAccessibility(id);
                if (service == null) {
                    return;
                }
                service.swipe(toPixelX(x1), toPixelY(y1), toPixelX(x2), toPixelY(y2), ms, gestureCallback(id));
                return;
            }
            case "longpress": {
                Double nx = optCoordinate(command, "x");
                Double ny = optCoordinate(command, "y");
                if (nx == null || ny == null) {
                    sendError(id, "longpress 缺少坐标 x/y");
                    return;
                }
                long ms = (long) command.optDouble("ms", 600);
                RemoteAccessibilityService service = requireAccessibility(id);
                if (service == null) {
                    return;
                }
                service.longPress(toPixelX(nx), toPixelY(ny), ms, gestureCallback(id));
                return;
            }
            case "key": {
                handleKey(id, command.optString("name", ""));
                return;
            }
            case "text": {
                String value = command.optString("text", "");
                if (value.length() > MAX_TEXT_LENGTH) {
                    sendError(id, "文本超过 " + MAX_TEXT_LENGTH + " 字");
                    return;
                }
                RemoteAccessibilityService service = requireAccessibility(id);
                if (service == null) {
                    return;
                }
                String error = service.inputText(value);
                if (error == null) {
                    sendAck(id);
                } else {
                    sendError(id, error);
                }
                return;
            }
            case "quality": {
                // 浏览器调整清晰度：{"action":"quality","width":540,"q":65,"fps":8}
                int nextWidth = (int) command.optDouble("width", targetWidth);
                int nextQuality = (int) command.optDouble("q", jpegQuality);
                double nextFps = command.optDouble("fps", 1000.0 / Math.max(1, frameIntervalMs));
                targetWidth = Math.max(MIN_TARGET_WIDTH, Math.min(MAX_TARGET_WIDTH, nextWidth));
                jpegQuality = Math.max(30, Math.min(90, nextQuality));
                double clampedFps = Math.max(1.0, Math.min(15.0, nextFps));
                frameIntervalMs = Math.max(30L, (long) (1000.0 / clampedFps));
                broadcastStatus("清晰度已切换: 宽 " + targetWidth + "px / q" + jpegQuality
                        + " / " + Math.round(1000.0 / frameIntervalMs) + "fps");
                // 静止屏幕上不会自动出新帧，立刻用上一帧按新参数重推一次
                postRepublishLastFrame();
                sendAck(id);
                return;
            }
            default:
                sendError(id, "未知指令: " + action);
        }
    }

    private void handleKey(String id, String name) {
        switch (name) {
            case "volume_up":
            case "volume_down":
                adjustVolume("volume_up".equals(name));
                sendAck(id);
                return;
            case "wake":
                wakeScreen();
                sendAck(id);
                return;
            case "enter": {
                RemoteAccessibilityService service = requireAccessibility(id);
                if (service == null) {
                    return;
                }
                String error = service.pressEnter();
                if (error == null) {
                    sendAck(id);
                } else {
                    sendError(id, error);
                }
                return;
            }
            case "back":
            case "home":
            case "recents":
            case "notifications":
            case "sleep": {
                RemoteAccessibilityService service = requireAccessibility(id);
                if (service == null) {
                    return;
                }
                String error = service.globalKey(name);
                if (error == null) {
                    sendAck(id);
                } else {
                    sendError(id, error);
                }
                return;
            }
            default:
                sendError(id, "未知按键: " + name);
        }
    }

    private RemoteAccessibilityService requireAccessibility(String id) {
        RemoteAccessibilityService service = RemoteAccessibilityService.get();
        if (service == null) {
            sendError(id, "accessibility not enabled");
            broadcastStatus("指令失败：无障碍服务未开启");
            return null;
        }
        return service;
    }

    private RemoteAccessibilityService.Callback gestureCallback(final String id) {
        return new RemoteAccessibilityService.Callback() {
            @Override
            public void done(boolean ok, String error) {
                if (ok) {
                    sendAck(id);
                } else {
                    sendError(id, error);
                }
            }
        };
    }

    private static Double optCoordinate(JSONObject command, String name) {
        if (!command.has(name) || command.isNull(name)) {
            return null;
        }
        double value = command.optDouble(name, Double.NaN);
        if (Double.isNaN(value)) {
            return null;
        }
        return value;
    }

    private float toPixelX(double normalized) {
        double clamped = Math.max(0.0, Math.min(1.0, normalized));
        return (float) (clamped * screenW);
    }

    private float toPixelY(double normalized) {
        double clamped = Math.max(0.0, Math.min(1.0, normalized));
        return (float) (clamped * screenH);
    }

    private void adjustVolume(boolean up) {
        try {
            AudioManager audio = (AudioManager) getSystemService(Context.AUDIO_SERVICE);
            if (audio == null) {
                return;
            }
            audio.adjustStreamVolume(
                    AudioManager.STREAM_MUSIC,
                    up ? AudioManager.ADJUST_RAISE : AudioManager.ADJUST_LOWER,
                    AudioManager.FLAG_SHOW_UI);
        } catch (RuntimeException ignored) {
        }
    }

    @SuppressWarnings("deprecation")
    private void wakeScreen() {
        try {
            PowerManager power = (PowerManager) getSystemService(Context.POWER_SERVICE);
            if (power == null) {
                return;
            }
            final PowerManager.WakeLock lock = power.newWakeLock(
                    PowerManager.SCREEN_BRIGHT_WAKE_LOCK | PowerManager.ACQUIRE_CAUSES_WAKEUP,
                    "phonecontrol:wake");
            lock.acquire(3000);
            mainHandler.postDelayed(new Runnable() {
                @Override
                public void run() {
                    try {
                        if (lock.isHeld()) {
                            lock.release();
                        }
                    } catch (RuntimeException ignored) {
                    }
                }
            }, 3000);
        } catch (RuntimeException ignored) {
        }
    }

    // ---------------------------------------------------------------- 无障碍状态轮询

    private final Runnable accessibilityWatch = new Runnable() {
        @Override
        public void run() {
            if (destroyed) {
                return;
            }
            boolean enabled = RemoteAccessibilityService.isEnabled();
            if (enabled != lastAccessibilityState) {
                lastAccessibilityState = enabled;
                if (ws != null && ws.isOpen()) {
                    sendHello();
                }
                broadcastStatus(enabled ? "无障碍已开启" : "无障碍已关闭");
            }
            mainHandler.postDelayed(this, 3000);
        }
    };

    // ---------------------------------------------------------------- 通知

    private void createNotificationChannel() {
        NotificationManager manager = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        if (manager == null) {
            return;
        }
        NotificationChannel channel = new NotificationChannel(
                CHANNEL_ID, "远程控制", NotificationManager.IMPORTANCE_LOW);
        channel.setDescription("投屏与远程控制期间常驻通知");
        manager.createNotificationChannel(channel);
    }

    private Notification buildNotification(String text) {
        Intent openIntent = new Intent(this, MainActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent openPending = PendingIntent.getActivity(
                this, 0, openIntent, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);

        Intent stopIntent = new Intent(this, ScreenService.class).setAction(ACTION_STOP);
        PendingIntent stopPending = PendingIntent.getService(
                this, 1, stopIntent, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);

        android.graphics.drawable.Icon stopIcon = android.graphics.drawable.Icon.createWithResource(
                this, android.R.drawable.ic_menu_close_clear_cancel);
        return new Notification.Builder(this, CHANNEL_ID)
                .setContentTitle("手机控制")
                .setContentText(text)
                .setSmallIcon(android.R.drawable.stat_notify_sync)
                .setOngoing(true)
                .setContentIntent(openPending)
                .addAction(new Notification.Action.Builder(stopIcon, "停止", stopPending).build())
                .build();
    }

    private void startForegroundCompat(String text) {
        Notification notification = buildNotification(text);
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(NOTIF_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION);
        } else {
            startForeground(NOTIF_ID, notification);
        }
    }

    private void updateNotification(String text) {
        try {
            NotificationManager manager = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
            if (manager != null) {
                manager.notify(NOTIF_ID, buildNotification(text));
            }
        } catch (RuntimeException ignored) {
        }
    }

    private void broadcastStatus(String text) {
        try {
            Intent intent = new Intent(ACTION_STATUS);
            intent.setPackage(getPackageName());
            intent.putExtra(EXTRA_STATUS_TEXT, text);
            sendBroadcast(intent);
        } catch (RuntimeException ignored) {
        }
    }
}

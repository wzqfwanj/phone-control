package cn.wkeeli.phonecontrol;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.content.Intent;
import android.graphics.Path;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;

/**
 * 无障碍服务：负责所有「需要触碰屏幕/窗口」的远控动作。
 *
 * <p>tap / swipe / longpress 用 GestureDescription#dispatchGesture 实现；
 * back / home / recents / notifications / 锁屏 用 performGlobalAction；
 * 文本输入用 ACTION_SET_TEXT（针对当前焦点输入框）；enter 用 ACTION_IME_ENTER。
 */
public class RemoteAccessibilityService extends AccessibilityService {

    /** 异步动作（手势）完成回调。 */
    public interface Callback {
        void done(boolean ok, String error);
    }

    private static volatile RemoteAccessibilityService instance;

    private final Handler mainHandler = new Handler(Looper.getMainLooper());

    /** 已连接的无障碍服务实例；null 表示未开启无障碍。 */
    public static RemoteAccessibilityService get() {
        return instance;
    }

    public static boolean isEnabled() {
        return instance != null;
    }

    /** 无障碍服务连接后通知外面（例如让投屏服务刷新 hello 里的 accessibility 状态）。 */
    public interface StateListener {
        void onAccessibilityStateChanged(boolean enabled);
    }

    private static volatile StateListener stateListener;

    public static void setStateListener(StateListener listener) {
        stateListener = listener;
    }

    private static void notifyState(boolean enabled) {
        StateListener l = stateListener;
        if (l != null) {
            try {
                l.onAccessibilityStateChanged(enabled);
            } catch (RuntimeException ignored) {
            }
        }
    }

    @Override
    protected void onServiceConnected() {
        super.onServiceConnected();
        instance = this;
        notifyState(true);
    }

    @Override
    public void onAccessibilityEvent(AccessibilityEvent event) {
        // 不需要事件流，远控动作都是主动发起的
    }

    @Override
    public void onInterrupt() {
        // 无需处理
    }

    @Override
    public boolean onUnbind(Intent intent) {
        if (instance == this) {
            instance = null;
        }
        notifyState(false);
        return super.onUnbind(intent);
    }

    @Override
    public void onDestroy() {
        if (instance == this) {
            instance = null;
        }
        notifyState(false);
        super.onDestroy();
    }

    // ---------------------------------------------------------------- 手势

    public void tap(float px, float py, Callback callback) {
        Path path = new Path();
        path.moveTo(px, py);
        dispatchGesture(path, 0, 60, callback);
    }

    public void longPress(float px, float py, long ms, Callback callback) {
        Path path = new Path();
        path.moveTo(px, py);
        dispatchGesture(path, 0, Math.max(100, ms), callback);
    }

    public void swipe(float x1, float y1, float x2, float y2, long ms, Callback callback) {
        Path path = new Path();
        path.moveTo(x1, y1);
        path.lineTo(x2, y2);
        dispatchGesture(path, 0, Math.max(50, ms), callback);
    }

    private void dispatchGesture(Path path, long startTimeMs, long durationMs, final Callback callback) {
        final boolean[] finished = new boolean[]{false};
        GestureDescription.StrokeDescription stroke =
                new GestureDescription.StrokeDescription(path, startTimeMs, durationMs);
        GestureDescription gesture = new GestureDescription.Builder().addStroke(stroke).build();
        boolean dispatched = dispatchGesture(gesture, new GestureResultCallback() {
            @Override
            public void onCompleted(GestureDescription gestureDescription) {
                finish(true, null);
            }

            @Override
            public void onCancelled(GestureDescription gestureDescription) {
                finish(false, "手势被系统取消");
            }

            private void finish(boolean ok, String error) {
                if (finished[0]) {
                    return;
                }
                finished[0] = true;
                if (callback != null) {
                    callback.done(ok, error);
                }
            }
        }, mainHandler);
        if (!dispatched) {
            if (!finished[0]) {
                finished[0] = true;
                if (callback != null) {
                    callback.done(false, "dispatchGesture 返回失败");
                }
            }
        }
    }

    // ---------------------------------------------------------------- 全局动作

    /** 返回 null 表示已执行，否则返回错误原因。 */
    public String globalKey(String name) {
        if (name == null) {
            return "缺少 key 名称";
        }
        switch (name) {
            case "back":
                return perform(android.accessibilityservice.AccessibilityService.GLOBAL_ACTION_BACK);
            case "home":
                return perform(GLOBAL_ACTION_HOME);
            case "recents":
                return perform(GLOBAL_ACTION_RECENTS);
            case "notifications":
                return perform(GLOBAL_ACTION_NOTIFICATIONS);
            case "sleep":
                // 锁屏 global action 是 Android 9（API 28）新增
                if (Build.VERSION.SDK_INT >= 28) {
                    return perform(GLOBAL_ACTION_LOCK_SCREEN);
                }
                return "锁屏需要 Android 9 及以上";
            default:
                return "不支持的无障碍动作: " + name;
        }
    }

    private String perform(int action) {
        boolean ok = performGlobalAction(action);
        return ok ? null : "performGlobalAction 失败";
    }

    // ---------------------------------------------------------------- 文本

    /** 返回 null 表示已执行，否则返回错误原因。 */
    public String inputText(String text) {
        if (text == null) {
            return "缺少文本";
        }
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root == null) {
            return "当前没有可访问窗口";
        }
        AccessibilityNodeInfo node = root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
        if (node == null) {
            node = root.findFocus(AccessibilityNodeInfo.FOCUS_ACCESSIBILITY);
        }
        if (node == null) {
            return "没有找到焦点输入框";
        }
        if (!node.isEditable()) {
            node.performAction(AccessibilityNodeInfo.ACTION_FOCUS);
        }
        Bundle args = new Bundle();
        args.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text);
        boolean ok = node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args);
        return ok ? null : "ACTION_SET_TEXT 执行失败";
    }

    /** 模拟回车（部分输入框支持）。返回 null 表示已执行。 */
    public String pressEnter() {
        if (Build.VERSION.SDK_INT < 30) {
            return "回车需要 Android 11 及以上";
        }
        AccessibilityNodeInfo root = getRootInActiveWindow();
        if (root == null) {
            return "当前没有可访问窗口";
        }
        AccessibilityNodeInfo node = root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
        if (node == null) {
            return "没有找到焦点输入框";
        }
        // ACTION_IME_ENTER 是 API 30 的 AccessibilityAction（int id 通过 getId() 取），
        // 上面的版本判断保证旧系统不会走到这里。
        boolean ok = node.performAction(
                AccessibilityNodeInfo.AccessibilityAction.ACTION_IME_ENTER.getId());
        return ok ? null : "ACTION_IME_ENTER 执行失败";
    }
}

package cn.wkeeli.phonecontrol;

import android.Manifest;
import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.media.projection.MediaProjectionManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.provider.Settings;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

/**
 * 主界面：填写服务器地址 + 配对码 → 授权投屏 → 启动前台投屏服务。
 * 无第三方依赖，界面全部用代码搭建以规避资源/主题兼容问题。
 */
public class MainActivity extends Activity {

    private static final int REQ_PROJECTION = 1001;
    private static final int REQ_NOTIFICATIONS = 1002;

    private static final String PREFS = "phonecontrol";
    private static final String PREF_SERVER_URL = "server_url";
    private static final String PREF_DEVICE_KEY = "device_key";

    private EditText urlInput;
    private EditText keyInput;
    private TextView statusText;
    private TextView accessibilityText;
    private Button startButton;

    private boolean receiverRegistered;

    private final BroadcastReceiver statusReceiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            if (intent == null) {
                return;
            }
            String status = intent.getStringExtra(ScreenService.EXTRA_STATUS_TEXT);
            if (status != null) {
                statusText.setText("状态：" + status);
            }
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(buildLayout());
        restorePrefs();
        registerStatusReceiver();
        updateAccessibilityText();
    }

    @Override
    protected void onResume() {
        super.onResume();
        updateAccessibilityText();
    }

    @Override
    protected void onDestroy() {
        if (receiverRegistered) {
            try {
                unregisterReceiver(statusReceiver);
            } catch (RuntimeException ignored) {
            }
            receiverRegistered = false;
        }
        super.onDestroy();
    }

    // ---------------------------------------------------------------- 界面

    private int dp(float value) {
        return Math.round(getResources().getDisplayMetrics().density * value);
    }

    private View buildLayout() {
        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(20);
        root.setPadding(pad, pad, pad, pad);
        scroll.addView(root, new ViewGroup.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        TextView title = new TextView(this);
        title.setText("手机控制");
        title.setTextSize(22);
        title.setGravity(Gravity.START);
        addView(root, title, 0);

        TextView subtitle = new TextView(this);
        subtitle.setText("让电脑浏览器实时查看并操作这台手机");
        subtitle.setTextSize(13);
        subtitle.setAlpha(0.7f);
        addView(root, subtitle, dp(4));

        TextView urlLabel = new TextView(this);
        urlLabel.setText("服务器地址");
        addView(root, urlLabel, dp(18));

        urlInput = new EditText(this);
        urlInput.setSingleLine(true);
        urlInput.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        urlInput.setHint("wss://wkeeli.cn/agent/ws/device");
        addView(root, urlInput, dp(4));

        TextView keyLabel = new TextView(this);
        keyLabel.setText("配对码（CONTROL_DEVICE_KEY）");
        addView(root, keyLabel, dp(12));

        keyInput = new EditText(this);
        keyInput.setSingleLine(true);
        keyInput.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        keyInput.setHint("服务器端设置的配对码");
        addView(root, keyInput, dp(4));

        LinearLayout buttonRow = new LinearLayout(this);
        buttonRow.setOrientation(LinearLayout.HORIZONTAL);
        LinearLayout.LayoutParams buttonRowParams = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        buttonRowParams.topMargin = dp(20);
        root.addView(buttonRow, buttonRowParams);

        Button accessibilityButton = new Button(this);
        accessibilityButton.setText("无障碍设置");
        accessibilityButton.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                openAccessibilitySettings();
            }
        });
        buttonRow.addView(accessibilityButton, new LinearLayout.LayoutParams(
                0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f));

        startButton = new Button(this);
        startButton.setText("开始投屏");
        startButton.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                onStartClicked();
            }
        });
        LinearLayout.LayoutParams startParams = new LinearLayout.LayoutParams(
                0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        startParams.leftMargin = dp(8);
        buttonRow.addView(startButton, startParams);

        Button stopButton = new Button(this);
        stopButton.setText("停止");
        stopButton.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                stopService();
            }
        });
        LinearLayout.LayoutParams stopParams = new LinearLayout.LayoutParams(
                0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        stopParams.leftMargin = dp(8);
        buttonRow.addView(stopButton, stopParams);

        accessibilityText = new TextView(this);
        accessibilityText.setTextSize(13);
        addView(root, accessibilityText, dp(18));

        statusText = new TextView(this);
        statusText.setTextSize(13);
        statusText.setText("状态：未启动");
        addView(root, statusText, dp(8));

        TextView help = new TextView(this);
        help.setTextSize(12);
        help.setAlpha(0.7f);
        help.setText("使用步骤：\n"
                + "1. 在手机「设置 → 无障碍 → 已下载的服务」里开启「手机控制（远程操作）」。\n"
                + "   小米/红米(澎湃OS/MIUI) 还需在「设置 → 应用设置 → 手机控制 → 省电策略」选「无限制」，\n"
                + "   否则后台被杀后无障碍会失效。\n"
                + "2. 填写电脑端服务地址和配对码（与服务器 CONTROL_DEVICE_KEY 一致）。\n"
                + "3. 点「开始投屏」，在系统弹窗里允许屏幕录制。\n"
                + "4. 在电脑浏览器打开控制台即可看到画面并操作。\n"
                + "注意：无障碍没开启时画面能看，但点击/滑动/按键/文本都不会生效；\n"
                + "App 必须保持前台服务运行，锁屏或从后台划掉会断开。");
        addView(root, help, dp(20));

        return scroll;
    }

    private void addView(LinearLayout parent, View child, int topMarginDp) {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        params.topMargin = topMarginDp;
        parent.addView(child, params);
    }

    // ---------------------------------------------------------------- 配置读写

    private SharedPreferences prefs() {
        return getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    private void restorePrefs() {
        String url = prefs().getString(PREF_SERVER_URL, null);
        String key = prefs().getString(PREF_DEVICE_KEY, null);
        if (url == null || url.isEmpty()) {
            url = BuildConfig.SERVER_URL;
        }
        if (key == null || key.isEmpty()) {
            key = BuildConfig.DEVICE_KEY;
        }
        urlInput.setText(url);
        keyInput.setText(key);
    }

    private void savePrefs() {
        prefs().edit()
                .putString(PREF_SERVER_URL, urlInput.getText().toString().trim())
                .putString(PREF_DEVICE_KEY, keyInput.getText().toString().trim())
                .apply();
    }

    // ---------------------------------------------------------------- 交互

    private void onStartClicked() {
        String url = urlInput.getText().toString().trim();
        if (url.isEmpty()) {
            Toast.makeText(this, "请先填写服务器地址", Toast.LENGTH_SHORT).show();
            return;
        }
        savePrefs();
        if (!isAccessibilityEnabled()) {
            Toast.makeText(this, "无障碍未开启：触摸/按键/文本指令将不可用", Toast.LENGTH_LONG).show();
        }
        statusText.setText("状态：请求屏幕录制授权…");
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, REQ_NOTIFICATIONS);
            return;
        }
        requestProjection();
    }

    private void requestProjection() {
        MediaProjectionManager manager =
                (MediaProjectionManager) getSystemService(Context.MEDIA_PROJECTION_SERVICE);
        if (manager == null) {
            Toast.makeText(this, "本机不支持屏幕录制", Toast.LENGTH_SHORT).show();
            return;
        }
        startActivityForResult(manager.createScreenCaptureIntent(), REQ_PROJECTION);
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == REQ_NOTIFICATIONS) {
            // 通知被拒也允许继续（Android 13+ 仍可启动前台服务）
            requestProjection();
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != REQ_PROJECTION) {
            return;
        }
        if (resultCode != RESULT_OK || data == null) {
            statusText.setText("状态：已取消投屏授权");
            return;
        }
        savePrefs();
        Intent intent = new Intent(this, ScreenService.class);
        intent.putExtra(ScreenService.EXTRA_RESULT_CODE, resultCode);
        intent.putExtra(ScreenService.EXTRA_RESULT_DATA, data);
        intent.putExtra(ScreenService.EXTRA_SERVER_URL, urlInput.getText().toString().trim());
        intent.putExtra(ScreenService.EXTRA_DEVICE_KEY, keyInput.getText().toString().trim());
        try {
            if (Build.VERSION.SDK_INT >= 26) {
                startForegroundService(intent);
            } else {
                startService(intent);
            }
            statusText.setText("状态：正在启动投屏服务…");
        } catch (RuntimeException e) {
            statusText.setText("状态：启动服务失败 - " + e.getMessage());
        }
    }

    private void stopService() {
        try {
            Intent intent = new Intent(this, ScreenService.class).setAction(ScreenService.ACTION_STOP);
            startService(intent);
            statusText.setText("状态：已请求停止");
        } catch (RuntimeException e) {
            statusText.setText("状态：停止失败 - " + e.getMessage());
        }
    }

    private void openAccessibilitySettings() {
        try {
            Intent intent = new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS);
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            startActivity(intent);
        } catch (RuntimeException e) {
            Toast.makeText(this, "无法打开无障碍设置，请手动进入系统设置", Toast.LENGTH_LONG).show();
        }
    }

    // ---------------------------------------------------------------- 无障碍检测

    private boolean isAccessibilityEnabled() {
        ComponentName component = new ComponentName(this, RemoteAccessibilityService.class);
        String target = component.flattenToString();
        String enabled = Settings.Secure.getString(
                getContentResolver(), Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES);
        if (enabled == null || enabled.isEmpty()) {
            return false;
        }
        for (String item : enabled.split(":")) {
            if (item.equalsIgnoreCase(target)) {
                return true;
            }
            // 部分 ROM 会额外带包名/斜杠格式，做一次宽松匹配
            if (item.toLowerCase().contains(getPackageName().toLowerCase())
                    && item.toLowerCase().contains("remoteaccessibilityservice")) {
                return true;
            }
        }
        return RemoteAccessibilityService.isEnabled();
    }

    private void updateAccessibilityText() {
        if (accessibilityText == null) {
            return;
        }
        if (isAccessibilityEnabled()) {
            accessibilityText.setText("无障碍：已开启（点击/滑动/按键/文本可用）");
        } else {
            accessibilityText.setText("无障碍：未开启（点击/滑动/按键/文本不可用，点上方按钮去开启）");
        }
    }

    // ---------------------------------------------------------------- 服务状态广播

    private void registerStatusReceiver() {
        IntentFilter filter = new IntentFilter(ScreenService.ACTION_STATUS);
        try {
            if (Build.VERSION.SDK_INT >= 33) {
                registerReceiver(statusReceiver, filter, Context.RECEIVER_NOT_EXPORTED);
            } else {
                registerReceiver(statusReceiver, filter);
            }
            receiverRegistered = true;
        } catch (RuntimeException ignored) {
            receiverRegistered = false;
        }
    }

    @SuppressWarnings("unused")
    private void openUrl(String url) {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
        } catch (RuntimeException ignored) {
        }
    }
}
